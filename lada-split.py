#!/usr/bin/env python3
"""
lada-split.py
Splits a video into chunks, runs lada-cli on each with resume support,
then concatenates the results.

Usage:
    lada-split --input video.mp4 --output /path/to/output.mp4
    lada-split --input video.mp4 --output /path/to/output.mp4 --max-clip-length 120

Keys during processing:
    Q — clean exit (waits for current frame, saves state)
    F — forced exit (kills immediately, saves state)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread

from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text
from rich.console import Group

console = Console(force_terminal=True)

# ─── Config ───────────────────────────────────────────────────────────────────
CHUNK_DURATION = 600            # 10 minutes in seconds
LADA_FIXED_ARGS = ["--fp16", "--mosaic-detection-model", "v4-accurate"]
MODEL_WEIGHTS_DIR = "/home/ptcarino/lada/model_weights"
LADA_VENV_BIN = "/home/ptcarino/lada/.venv/bin/lada-cli"
TEMP_BASE = Path("/mnt/f/lada_tmp")
STATE_DIR = Path("/mnt/f/lada_tmp/.lada_state")

SHUTDOWN_COUNTDOWN = 300        # seconds before auto-shutdown (default: 5 minutes)
SHUTDOWN_WINDOW_START = 3       # earliest hour shutdown is allowed (03:00)
SHUTDOWN_WINDOW_END = 7         # latest hour shutdown is allowed (07:00)

OUTPUT_PATTERN_DEFAULT = "{orig_file_name}-MR"

ROCM_ENV = {
    "PYTORCH_ALLOC_CONF": "expandable_segments:True,garbage_collection_threshold:0.5,max_split_size_mb:128",
    "HSA_FORCE_FINE_GRAIN_PCIE": "1",
    "GPU_MAX_ALLOC_PERCENT": "100",
    "GPU_SINGLE_ALLOC_PERCENT": "100",
    "LADA_MODEL_WEIGHTS_DIR": MODEL_WEIGHTS_DIR,
}

# ─── Quit flags ───────────────────────────────────────────────────────────────
QUIT_CLEAN = False
QUIT_FORCE = False

# ─── Logging ──────────────────────────────────────────────────────────────────
_file_handler = None  # current per-job file handler

def setup_logging():
    """Called once at startup — sets up the console handler via rich."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_level=True,
        show_path=False,
        markup=False,
        rich_tracebacks=False,
    )
    rich_handler.setFormatter(logging.Formatter("%(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(rich_handler)

    fl = logging.getLogger("file_only")
    fl.setLevel(logging.DEBUG)
    fl.propagate = False

def setup_job_logging(log_path: Path):
    """Called once per job — attaches a new file handler and removes the old one."""
    global _file_handler
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    fl = logging.getLogger("file_only")

    # Remove previous file handler if present
    if _file_handler is not None:
        root.removeHandler(_file_handler)
        fl.removeHandler(_file_handler)
        _file_handler.close()

    _file_handler = logging.FileHandler(log_path)
    _file_handler.setFormatter(formatter)
    root.addHandler(_file_handler)
    fl.addHandler(_file_handler)

log = logging.getLogger(__name__)
file_logger = logging.getLogger("file_only")

# ─── Helpers ──────────────────────────────────────────────────────────────────
def input_hash(input_path: Path) -> str:
    return hashlib.md5(str(input_path.resolve()).encode()).hexdigest()[:10]

def load_state(state_file: Path) -> dict:
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {}

def save_state(state_file: Path, state: dict):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def fmt_duration(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:
        return "--:--:--"
    return str(timedelta(seconds=int(seconds)))

def fmt_frames(n: int) -> str:
    return f"{n:,}"

def get_video_frames(path: Path) -> int:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-count_packets", "-show_entries", "stream=nb_read_packets",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0

def get_video_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0

# ─── Windows ffmpeg (AMF) ─────────────────────────────────────────────────────
def detect_windows_ffmpeg() -> str | None:
    """Return path to ffmpeg.exe if available, else None."""
    result = subprocess.run(["which", "ffmpeg.exe"], capture_output=True, text=True)
    path = result.stdout.strip()
    return path if path else None

def to_windows_path(path: Path) -> str:
    """Convert a WSL path to a Windows path using wslpath."""
    result = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True)
    return result.stdout.strip()

FFMPEG_EXE = detect_windows_ffmpeg()

# ─── Shutdown ─────────────────────────────────────────────────────────────────
def maybe_shutdown(countdown: int = SHUTDOWN_COUNTDOWN):
    """Prompt user before shutting down Windows. Cancels if any key is pressed."""
    now = datetime.now()
    if not (SHUTDOWN_WINDOW_START <= now.hour < SHUTDOWN_WINDOW_END):
        log.info(
            f"Shutdown skipped — current time {now.strftime('%H:%M')} is outside "
            f"the allowed window ({SHUTDOWN_WINDOW_START:02d}:00–{SHUTDOWN_WINDOW_END:02d}:00)."
        )
        return

    print("\r\n" + "─" * 72)
    print(f"  Shutdown scheduled. Press any key to cancel...\r")
    print("─" * 72)

    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    old_settings = None
    if fd is not None:
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)

    try:
        for remaining in range(countdown, 0, -1):
            mins, secs = divmod(remaining, 60)
            sys.stdout.write(f"\r  Shutting down in {mins}:{secs:02d}...   ")
            sys.stdout.flush()
            if fd is not None and select.select([sys.stdin], [], [], 1)[0]:
                sys.stdin.read(1)
                print("\r\n  Shutdown cancelled.\r")
                return
    finally:
        if fd is not None and old_settings is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    print("\r\n  Shutting down now...\r")
    subprocess.run(["shutdown.exe", "/s", "/t", "0"])

# ─── Keypress listener ────────────────────────────────────────────────────────
def keypress_listener():
    global QUIT_CLEAN, QUIT_FORCE
    if not sys.stdin.isatty():
        return
    while True:
        try:
            line = sys.stdin.readline().strip().lower()
        except (EOFError, OSError):
            return
        if line == 'q':
            QUIT_CLEAN = True
            return
        elif line == 'f':
            QUIT_FORCE = True
            return

# ─── Exit summary ─────────────────────────────────────────────────────────────
def print_summary(chunks: list, completed: set, failed: set, start_time: float):
    elapsed = time.time() - start_time
    remaining = [str(i+1) for i, c in enumerate(chunks)
                 if str(c) not in completed and str(c) not in failed]
    completed_nums = [str(i+1) for i, c in enumerate(chunks) if str(c) in completed]
    failed_nums = [str(i+1) for i, c in enumerate(chunks) if str(c) in failed]

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Total elapsed:",    fmt_duration(elapsed))
    table.add_row("Chunks completed:", ", ".join(completed_nums) if completed_nums else "none")
    table.add_row("Chunks failed:",    ", ".join(failed_nums) if failed_nums else "none")
    table.add_row("Chunks remaining:", ", ".join(remaining) if remaining else "none")
    console.print(Panel(table, title="Exit summary", border_style="dim"))

# ─── Progress display ─────────────────────────────────────────────────────────
def _make_progress() -> Progress:
    """Create a Progress instance with shared column layout."""
    return Progress(
        TextColumn("  [bold]{task.description:<20}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TextColumn("[cyan]{task.completed:,}[/cyan]/[cyan]{task.total:,}[/cyan] frames"),
        TextColumn("[green]{task.fields[fps]:.1f} fps[/green]"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=True,
    )

def ffmpeg_with_progress(cmd: list, total_frames: int, label: str):
    """Run an ffmpeg command and show a single-bar Live progress display."""
    cmd = cmd + ["-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    progress = _make_progress()
    task = progress.add_task(label, total=total_frames, fps=0.0)
    frames_done = 0
    start_time = time.time()

    with Live(progress, console=console, refresh_per_second=10):
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("frame="):
                try:
                    frames_done = int(line.split("=")[1])
                except ValueError:
                    pass
                elapsed = time.time() - start_time
                fps = frames_done / elapsed if elapsed > 0 and frames_done > 0 else 0.0
                progress.update(task, completed=frames_done, fps=fps)

    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read()
        log.error(f"ffmpeg failed:\n{err}")
        sys.exit(1)

class ProgressTracker:
    """Two-bar progress display for chunk processing using Live.
    Call start() before the chunk loop, stop() after."""

    def __init__(self, total_chunks: int, total_frames: int, completed_frames: int = 0):
        self.total_chunks = total_chunks
        self.total_frames = total_frames
        self.completed_frames = completed_frames
        self.current_chunk = 0
        self.current_chunk_frames = 0
        self.start_time = time.time()
        self.chunk_start_time = time.time()
        self._progress: Progress | None = None
        self._live: Live | None = None
        self._overall_task = None
        self._chunk_task = None
        self._phase_text = Text("  Phase: Initializing", style="bold cyan")

    def set_phase(self, phase: str):
        self._phase_text = Text(f"  Phase: {phase}", style="bold cyan")
        if self._live is not None:
            self._live.update(Group(self._phase_text, self._progress))

    def start(self):
        """Start the two-bar Live display. Call once before the chunk loop begins."""
        self._progress = _make_progress()
        self._overall_task = self._progress.add_task(
            "Overall",
            total=self.total_frames,
            completed=self.completed_frames,
            fps=0.0,
        )
        self._chunk_task = self._progress.add_task(
            "Chunk -/-",
            total=1,
            completed=0,
            fps=0.0,
        )
        self._live = Live(
            Group(self._phase_text, self._progress),
            console=console,
            refresh_per_second=10,
        )
        self._live.start()
        self._live.console.print("  [dim]Q + Enter = clean exit    F + Enter = forced exit[/dim]")

    def start_chunk(self, chunk_num: int, chunk_frames: int):
        self.current_chunk = chunk_num
        self.current_chunk_frames = chunk_frames
        self.chunk_start_time = time.time()
        if self._progress is not None:
            self._progress.reset(
                self._chunk_task,
                total=chunk_frames,
                description=f"Chunk {chunk_num}/{self.total_chunks}",
            )

    def complete_chunk(self, chunk_frames: int):
        self.completed_frames += chunk_frames

    def print_initial(self):
        # No-op — Live display is started explicitly via start()
        pass

    def render(self, chunk_frames_done: int, chunk_total: int):
        if self._progress is None or self._live is None:
            return
        now = time.time()
        elapsed_total = now - self.start_time
        overall_done = self.completed_frames + chunk_frames_done
        overall_fps = overall_done / elapsed_total if elapsed_total > 0 and overall_done > 0 else 0.0
        chunk_elapsed = now - self.chunk_start_time
        chunk_fps = chunk_frames_done / chunk_elapsed if chunk_elapsed > 0 and chunk_frames_done > 0 else 0.0
        self._progress.update(self._overall_task, completed=overall_done, fps=overall_fps)
        self._progress.update(self._chunk_task, completed=chunk_frames_done, fps=chunk_fps)

    def stop(self):
        if self._live is not None:
            self._live.stop()
            self._live = None
            self._progress = None

# ─── Output validation ────────────────────────────────────────────────────────
DURATION_TOLERANCE = 2.0

def validate_output(output: Path, source_chunk: Path) -> tuple[bool, str]:
    if not output.exists():
        return False, "output file does not exist"
    source_duration = get_video_duration(source_chunk)
    output_duration = get_video_duration(output)
    if source_duration <= 0:
        return False, "could not read source chunk duration"
    if output_duration <= 0:
        return False, "could not read output duration"
    diff = abs(source_duration - output_duration)
    if diff > DURATION_TOLERANCE:
        return False, (
            f"duration mismatch: source={source_duration:.2f}s "
            f"output={output_duration:.2f}s diff={diff:.2f}s"
        )
    return True, "ok"

# ─── Split ────────────────────────────────────────────────────────────────────
def split_video(input_path: Path, work_dir: Path) -> list[Path]:
    log.info(f"Splitting into {CHUNK_DURATION}s chunks...")
    chunk_pattern = work_dir / "chunk_%04d.mp4"
    cmd = [
        "ffmpeg", "-i", str(input_path),
        "-c", "copy", "-map", "0",
        "-segment_time", str(CHUNK_DURATION),
        "-f", "segment", "-reset_timestamps", "1",
        str(chunk_pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"ffmpeg split failed:\n{result.stderr}")
        sys.exit(1)
    chunks = sorted(work_dir.glob("chunk_*.mp4"))
    log.info(f"Created {len(chunks)} chunk(s).")
    return chunks

# ─── Downscale / Upscale ──────────────────────────────────────────────────────
def parse_resolution(res_str: str) -> int:
    """Parse a resolution string like '720p' into an integer height."""
    res_str = res_str.strip().lower().rstrip('p')
    try:
        return int(res_str)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid resolution: '{res_str}'. Use formats like 720p, 540p, 480p.")

def get_video_resolution(path: Path) -> tuple[int, int]:
    """Return (width, height) of a video."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        w, h = result.stdout.strip().split(",")
        return int(w), int(h)
    except Exception:
        return 0, 0

def downscale_video(input_path: Path, output_path: Path, target_height: int):
    log.info(f"Downscaling to {target_height}p -> {output_path.name}...")
    total_frames = get_video_frames(input_path)
    if FFMPEG_EXE:
        log.info("Using Windows ffmpeg with AMF (h264_amf) for downscale.")
        orig_w, orig_h = get_video_resolution(input_path)
        target_width = (orig_w * target_height // orig_h) & ~1  # keep aspect ratio, ensure even
        cmd = [
            FFMPEG_EXE,
            "-i", to_windows_path(input_path),
            "-vf", f"vpp_amf=w={target_width}:h={target_height}",
            "-c:v", "h264_amf",
            "-c:a", "copy",
            "-y", to_windows_path(output_path),
        ]
    else:
        log.info("ffmpeg.exe not found — falling back to Linux ffmpeg (software scale) for downscale.")
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-vf", f"scale=-2:{target_height}:flags=lanczos",
            "-c:a", "copy",
            "-y", str(output_path),
        ]
    ffmpeg_with_progress(cmd, total_frames, "Downscale")
    log.info("Downscale complete.")

def upscale_video(input_path: Path, output_path: Path, target_width: int, target_height: int):
    log.info(f"Upscaling to {target_width}x{target_height} -> {output_path.name}...")
    total_frames = get_video_frames(input_path)
    if FFMPEG_EXE:
        log.info("Using Windows ffmpeg with AMF (hevc_amf) for upscale.")
        cmd = [
            FFMPEG_EXE,
            "-i", to_windows_path(input_path),
            "-vf", f"vpp_amf=w={target_width}:h={target_height}",
            "-c:v", "hevc_amf",
            "-quality", "quality",
            "-c:a", "copy",
            "-y", to_windows_path(output_path),
        ]
    else:
        log.info("ffmpeg.exe not found — falling back to Linux ffmpeg (software scale) for upscale.")
        cmd = [
            "ffmpeg", "-i", str(input_path),
            "-vf", f"scale={target_width}:{target_height}:flags=lanczos",
            "-c:a", "copy",
            "-y", str(output_path),
        ]
    ffmpeg_with_progress(cmd, total_frames, "Upscale ")
    log.info("Upscale complete.")


def run_lada(chunk: Path, output: Path, work_dir: Path, extra_args: list,
             tracker: ProgressTracker, chunk_idx: int, chunk_frames: int) -> tuple[bool, list]:
    global QUIT_CLEAN, QUIT_FORCE

    lada_cmd = [
        LADA_VENV_BIN,
        "--input", str(chunk),
        "--output", str(output),
        "--temporary-directory", str(work_dir),
    ] + LADA_FIXED_ARGS + extra_args

    env = {**os.environ, **ROCM_ENV}
    tracker.start_chunk(chunk_idx, chunk_frames)
    tracker.print_initial()

    try:
        proc = subprocess.Popen(
            lada_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        frame_re = re.compile(r"Processed:\s*[\d:]+\s*\((\d+)f\)")
        error_lines = []

        for line in proc.stdout:
            # Check quit flags
            if QUIT_FORCE:
                proc.kill()
                proc.wait()
                return False, error_lines
            if QUIT_CLEAN:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=30)
                return False, error_lines

            line = line.strip()
            if not line:
                continue

            m = frame_re.search(line)
            if m:
                frames_done = int(m.group(1))
                tracker.render(frames_done, chunk_frames)
            else:
                file_logger.debug(line)
                if any(x in line.lower() for x in ["error", "exception", "traceback", "crashed", "out of memory"]):
                    error_lines.append(line)

        proc.wait()

        if error_lines:
            for err in error_lines[-5:]:
                log.error(err)

        return proc.returncode == 0, error_lines

    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        log.info("Interrupted.")
        return False, []
    except Exception as e:
        log.error(f"Exception running lada-cli: {e}")
        return False, []

# ─── Concatenate ──────────────────────────────────────────────────────────────
def concatenate(restored_chunks: list, output: Path, work_dir: Path):
    log.info(f"Concatenating {len(restored_chunks)} chunk(s) -> {output}")
    concat_file = work_dir / "concat.txt"
    with open(concat_file, "w") as f:
        for chunk in restored_chunks:
            f.write(f"file '{chunk}'\n")
    cmd = [
        "ffmpeg", "-f", "concat", "-safe", "0",
        "-i", str(concat_file), "-c", "copy", str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"ffmpeg concat failed:\n{result.stderr}")
        sys.exit(1)
    log.info(f"Output saved to: {output}")

# ─── Output path resolution ───────────────────────────────────────────────────
def resolve_output_path(input_path: Path, output: str | None, output_dir: str | None, output_pattern: str | None) -> Path:
    """Resolve the output path from --output or --output-dir + --output-pattern."""
    if output:
        return Path(output)
    pattern = output_pattern or OUTPUT_PATTERN_DEFAULT
    stem = pattern.replace("{orig_file_name}", input_path.stem)
    filename = stem + input_path.suffix
    return Path(output_dir) / filename

# ─── Per-file processing ──────────────────────────────────────────────────────
def process_file(input_path: Path, output_path: Path, args, extra_args: list) -> bool:
    """Process a single input file. Returns True on success, False on failure."""
    global QUIT_CLEAN, QUIT_FORCE

    if not input_path.exists():
        log.error(f"Input file not found: {input_path}")
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)

    job_id = input_hash(input_path)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{job_id}.json"
    log_file = STATE_DIR / f"{job_id}.log"

    setup_job_logging(log_file)
    log.info(f"Job ID: {job_id} ({input_path.name})")
    if FFMPEG_EXE:
        log.info(f"GPU scaling: Windows ffmpeg AMF detected ({FFMPEG_EXE})")
    else:
        log.info("GPU scaling: ffmpeg.exe not found — using Linux ffmpeg (software scale)")
    log.info("Press Q + Enter for clean exit, F + Enter for forced exit.")

    state = load_state(state_file)
    resuming = bool(state)

    # Check if already completed
    if resuming and state.get("done"):
        log.info(f"Skipping already completed job for: {input_path.name}")
        return True

    if resuming:
        log.info(f"Resuming previous job for: {input_path.name}")
        work_dir = Path(state["work_dir"])
        chunks = [Path(c) for c in state["chunks"]]
        chunk_frames = state["chunk_frames"]
        completed = set(state.get("completed", []))
        failed = set(state.get("failed", []))
        original_resolution = tuple(state["original_resolution"]) if state.get("original_resolution") else None

        # Validate downscaled file if this job used --pre-downscale
        if original_resolution:
            downscaled_path = work_dir / "input_downscaled.mp4"
            input_duration = get_video_duration(input_path)
            downscaled_duration = get_video_duration(downscaled_path) if downscaled_path.exists() else 0.0
            if abs(input_duration - downscaled_duration) > DURATION_TOLERANCE:
                log.warning("Downscaled file is missing or incomplete. Re-running downscale...")
                target_height = state.get("downscale_target_height", 720)
                console.print("  [bold cyan]Phase: Downscaling[/bold cyan]")
                downscale_video(input_path, downscaled_path, target_height)
                log.info("Re-downscale complete. Resuming chunk processing.")
    else:
        log.info(f"Starting new job for: {input_path.name}")
        work_dir = TEMP_BASE / f"lada_{job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)

        # Downscale if requested
        source_for_split = input_path
        original_resolution = None
        target_height = None
        if args.pre_downscale:
            target_height = parse_resolution(args.pre_downscale)
            orig_w, orig_h = get_video_resolution(input_path)
            if orig_h <= target_height:
                log.warning(f"Input resolution ({orig_h}p) is already <= target ({target_height}p). Skipping downscale.")
            else:
                original_resolution = (orig_w, orig_h)
                downscaled_path = work_dir / "input_downscaled.mp4"
                input_duration = get_video_duration(input_path)
                downscaled_duration = get_video_duration(downscaled_path) if downscaled_path.exists() else 0.0
                if abs(input_duration - downscaled_duration) <= DURATION_TOLERANCE:
                    log.info("Existing downscaled file is valid. Skipping downscale.")
                else:
                    console.print("  [bold cyan]Phase: Downscaling[/bold cyan]")
                    downscale_video(input_path, downscaled_path, target_height)
                source_for_split = downscaled_path

        chunks = split_video(source_for_split, work_dir)
        log.info("Counting frames per chunk (this may take a moment)...")
        chunk_frames = {str(c): get_video_frames(c) for c in chunks}
        completed = set()
        failed = set()
        state = {
            "input": str(input_path),
            "output": str(output_path),
            "work_dir": str(work_dir),
            "chunks": [str(c) for c in chunks],
            "chunk_frames": chunk_frames,
            "completed": [],
            "failed": [],
            "original_resolution": list(original_resolution) if original_resolution else None,
            "downscale_target_height": target_height if original_resolution else None,
            "done": False,
        }
        save_state(state_file, state)

    total_frames = sum(chunk_frames.values())
    total_chunks = len(chunks)
    already_done = sum(chunk_frames.get(c, 0) for c in completed)
    tracker = ProgressTracker(total_chunks, total_frames, already_done)

    log.info(f"Chunks: {total_chunks} | Total frames: {fmt_frames(total_frames)}")
    log.info(f"Completed: {len(completed)} | Remaining: {total_chunks - len(completed) - len(failed)}")

    # Start keypress listener thread
    listener = Thread(target=keypress_listener, daemon=True)
    listener.start()

    def attempt(chunk: Path, restored: Path, idx: int, frames: int, attempt_num: int) -> bool:
        exit_ok, error_lines = run_lada(chunk, restored, work_dir, extra_args, tracker, idx, frames)
        if QUIT_CLEAN or QUIT_FORCE:
            return False
        if not exit_ok:
            log.error(f"Chunk {idx}/{total_chunks} attempt {attempt_num}: lada-cli exited with error.")
            return False
        valid, reason = validate_output(restored, chunk)
        if not valid:
            log.error(f"Chunk {idx}/{total_chunks} attempt {attempt_num}: validation failed — {reason}")
            restored.unlink(missing_ok=True)
            return False
        return True

    # Process chunks
    tracker.start()
    tracker.set_phase(f"Processing chunks (0/{total_chunks})")
    for idx, chunk in enumerate(chunks, start=1):
        if QUIT_CLEAN or QUIT_FORCE:
            break

        chunk_key = str(chunk)

        if chunk_key in completed:
            log.info(f"Skipping already completed chunk {idx}/{total_chunks}: {chunk.name}")
            continue

        restored = work_dir / f"restored_{chunk.name}"
        log.info(f"Processing chunk {idx}/{total_chunks}: {chunk.name}")
        frames = chunk_frames.get(chunk_key, 0)
        tracker.set_phase(f"Processing chunks ({idx}/{total_chunks})")

        if attempt(chunk, restored, idx, frames, 1):
            log.info(f"Chunk {idx}/{total_chunks} completed.")
            tracker.complete_chunk(frames)
            completed.add(chunk_key)
            failed.discard(chunk_key)
        elif not QUIT_CLEAN and not QUIT_FORCE:
            log.warning(f"Chunk {idx}/{total_chunks} failed. Retrying...")
            restored.unlink(missing_ok=True)
            tracker.start_chunk(idx, frames)
            if attempt(chunk, restored, idx, frames, 2):
                log.info(f"Chunk {idx}/{total_chunks} succeeded on retry.")
                tracker.complete_chunk(frames)
                completed.add(chunk_key)
                failed.discard(chunk_key)
            else:
                if not QUIT_CLEAN and not QUIT_FORCE:
                    log.error(f"Chunk {idx}/{total_chunks} failed on retry. Will skip.")
                    failed.add(chunk_key)

        state["completed"] = list(completed)
        state["failed"] = list(failed)
        save_state(state_file, state)

    tracker.stop()

    # Handle quit
    if QUIT_CLEAN or QUIT_FORCE:
        quit_type = "Clean" if QUIT_CLEAN else "Forced"
        log.info(f"{quit_type} exit requested.")
        print_summary(chunks, completed, failed, tracker.start_time)
        sys.exit(0)

    if not completed:
        log.error("No chunks completed successfully. Aborting.")
        return False

    if failed:
        log.warning(f"{len(failed)} chunk(s) failed and will be missing from output.")

    # Concatenate
    restored_chunks = [
        work_dir / f"restored_{chunk.name}"
        for chunk in chunks if str(chunk) in completed
    ]

    if original_resolution:
        if args.skip_upscale:
            console.print("  [bold cyan]Phase: Concatenating[/bold cyan]")
            concatenate(restored_chunks, output_path, work_dir)
            log.info("Upscale skipped — output is at downscaled resolution.")
        else:
            console.print("  [bold cyan]Phase: Concatenating[/bold cyan]")
            pre_upscale_path = work_dir / "output_pre_upscale.mp4"
            concatenate(restored_chunks, pre_upscale_path, work_dir)
            if args.output_res:
                out_w, out_h = [int(x) for x in args.output_res.split("x")]
            else:
                out_w, out_h = original_resolution[0], original_resolution[1]
            console.print("  [bold cyan]Phase: Upscaling[/bold cyan]")
            upscale_video(pre_upscale_path, output_path, out_w, out_h)
    else:
        console.print("  [bold cyan]Phase: Concatenating[/bold cyan]")
        concatenate(restored_chunks, output_path, work_dir)

    # Cleanup
    log.info("Cleaning up temporary files...")
    shutil.rmtree(work_dir)
    state["done"] = True
    save_state(state_file, state)
    state_file.unlink(missing_ok=True)

    log.info("Done.")

    if args.delete_input and not failed:
        try:
            input_path.unlink()
            log.info(f"Deleted original input file: {input_path}")
        except Exception as e:
            log.warning(f"Could not delete input file: {e}")

    return True

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    global QUIT_CLEAN, QUIT_FORCE

    setup_logging()

    parser = argparse.ArgumentParser(
        description="Split, restore with lada-cli, and concatenate video.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    # Input — mutually exclusive
    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument("-i", "--input", help="Input video file")
    input_group.add_argument("--input-dir", help="Directory of .mp4 files to process sequentially")

    # Output
    parser.add_argument("-o", "--output",
                        help="Output video file path. Required with --input unless --output-dir is used.")
    parser.add_argument("--output-dir",
                        help="Output directory. Used with --output-pattern (or default pattern).")
    parser.add_argument("-p", "--output-pattern", default=OUTPUT_PATTERN_DEFAULT,
                        metavar="PATTERN",
                        help=f"Output filename pattern using {{orig_file_name}} as placeholder. "
                             f"Extension is taken from the source file. "
                             f"Default: {OUTPUT_PATTERN_DEFAULT}")

    parser.add_argument("--pre-downscale", metavar="RESOLUTION", nargs="?", const="720p",
                        type=str,
                        help="Downscale input to the given resolution before processing (e.g. 720p, 540p, 480p). "
                             "Defaults to 720p if no value given. Output will be upscaled back to original resolution.")
    parser.add_argument("--output-res", metavar="WxH",
                        help="Override the upscale output resolution (e.g. 1920x1080). "
                             "Only valid when --pre-downscale is used.")
    parser.add_argument("--skip-upscale", action="store_true",
                        help="Skip the upscale step after processing. The concatenated downscaled file "
                             "is written directly to the output path. Only valid when --pre-downscale is used.")
    parser.add_argument("--shutdown-after", action="store_true",
                        help=f"Shut down Windows after successful completion "
                             f"(only between {SHUTDOWN_WINDOW_START:02d}:00–{SHUTDOWN_WINDOW_END:02d}:00, "
                             f"with a {SHUTDOWN_COUNTDOWN//60}-minute countdown)")
    parser.add_argument("--delete-input", action="store_true",
                        help="Delete the original input file after successful completion. "
                             "Only applies when all chunks completed without failure.")
    parser.add_argument("-r", "--remove-job", metavar="JOB_ID",
                        help="Remove all temporary files and state for the given job ID. "
                             "Standalone operation — no other flags required.")

    args, extra_args = parser.parse_known_args()

    # ── Handle --remove-job standalone operation ───────────────────────────────
    if args.remove_job:
        job_id = args.remove_job
        work_dir = TEMP_BASE / f"lada_{job_id}"
        state_file = STATE_DIR / f"{job_id}.json"
        log_file = STATE_DIR / f"{job_id}.log"
        found_any = False

        if work_dir.exists():
            console.print(f"  Removing work directory: {work_dir}")
            shutil.rmtree(work_dir)
            found_any = True
        else:
            console.print(f"  Work directory not found (already clean): {work_dir}")

        if state_file.exists():
            console.print(f"  Removing state file: {state_file}")
            state_file.unlink()
            found_any = True
        else:
            console.print(f"  State file not found (already clean): {state_file}")

        if log_file.exists():
            console.print(f"  Removing log file: {log_file}")
            log_file.unlink()
            found_any = True
        else:
            console.print(f"  Log file not found (already clean): {log_file}")

        if found_any:
            console.print(f"\n  [green]Job {job_id} cleaned up.[/green]")
        else:
            console.print(f"\n  [yellow]No files found for job {job_id}.[/yellow]")
        sys.exit(0)

    # ── Validate input flags are present for normal operation ──────────────────
    if not args.input and not args.input_dir:
        parser.error("one of the arguments --input/--input-dir is required.")

    # ── Validate argument combinations ────────────────────────────────────────
    if args.input:
        if not args.output and not args.output_dir:
            parser.error("--input requires either --output or --output-dir.")
        if args.output and args.output_dir:
            parser.error("--output and --output-dir cannot be used together.")
        if not Path(args.input).exists():
            parser.error(f"Input file not found: {args.input}")
    if args.input_dir:
        if not args.output_dir:
            parser.error("--input-dir requires --output-dir.")
        if args.output:
            parser.error("--output cannot be used with --input-dir. Use --output-dir and --output-pattern instead.")
    if args.output_pattern:
        if "--" in args.output_pattern:
            parser.error("--output-pattern appears to contain a flag. Did you forget a space between arguments?")
        if "/" in args.output_pattern or "\\" in args.output_pattern:
            parser.error("--output-pattern must be a filename stem, not a path. Use --output-dir for the directory.")
    if args.skip_upscale and not args.pre_downscale:
        parser.error("--skip-upscale requires --pre-downscale.")
    if args.output_res:
        if not args.pre_downscale:
            parser.error("--output-res requires --pre-downscale.")
        if args.skip_upscale:
            parser.error("--output-res and --skip-upscale cannot be used together.")
        if not re.fullmatch(r"\d+x\d+", args.output_res):
            parser.error(f"--output-res must be in WxH format (e.g. 1920x1080), got: {args.output_res}")
        try:
            out_w, out_h = [int(x) for x in args.output_res.split("x")]
            if out_w <= 0 or out_h <= 0:
                raise ValueError
        except ValueError:
            parser.error(f"--output-res dimensions must be positive integers, got: {args.output_res}")

    # ── Collect input files ───────────────────────────────────────────────────
    if args.input:
        input_files = [Path(args.input).resolve()]
    else:
        input_dir = Path(args.input_dir).resolve()
        if not input_dir.is_dir():
            print(f"Error: Input directory not found: {input_dir}")
            sys.exit(1)
        input_files = sorted(input_dir.glob("*.mp4"))
        if not input_files:
            print(f"Error: No .mp4 files found in: {input_dir}")
            sys.exit(1)
        log.info(f"Found {len(input_files)} .mp4 file(s) in {input_dir}")

    # ── Process files ─────────────────────────────────────────────────────────
    for idx, input_path in enumerate(input_files, start=1):
        if len(input_files) > 1:
            print(f"\n{'═' * 72}")
            print(f"  File {idx}/{len(input_files)}: {input_path.name}")
            print(f"{'═' * 72}")

        output_path = resolve_output_path(
            input_path,
            args.output if args.input else None,
            args.output_dir,
            args.output_pattern,
        )

        success = process_file(input_path, output_path, args, extra_args)

        if not success:
            log.error(f"Failed processing: {input_path.name}. Stopping.")
            sys.exit(1)

        # Reset quit flags between files
        QUIT_CLEAN = False
        QUIT_FORCE = False

    if args.shutdown_after:
        maybe_shutdown()

if __name__ == "__main__":
    main()