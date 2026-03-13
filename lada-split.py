#!/usr/bin/env python3
"""
lada-split.py
Splits a video into chunks, runs lada-cli on each with resume support,
then concatenates the results.

Usage:
    lada-split --input video.mp4 --output /mnt/e/lada_exports/output.mp4
    lada-split --input video.mp4 --output /mnt/e/lada_exports/output.mp4 --max-clip-length 120

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
from datetime import timedelta
from pathlib import Path
from threading import Thread

# ─── Config ───────────────────────────────────────────────────────────────────
CHUNK_DURATION = 600            # 10 minutes in seconds
LADA_FIXED_ARGS = ["--fp16", "--mosaic-detection-model", "v4-accurate"]
MODEL_WEIGHTS_DIR = "/home/ptcarino/lada/model_weights"
LADA_VENV_BIN = "/home/ptcarino/lada/.venv/bin/lada-cli"
TEMP_BASE = Path("/mnt/f/lada_tmp")
STATE_DIR = Path("/mnt/f/lada_tmp/.lada_state")

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
def setup_logging(log_path: Path):
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )
    fl = logging.getLogger("file_only")
    fl.setLevel(logging.DEBUG)
    fl.propagate = False
    fl.addHandler(logging.FileHandler(log_path))

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

# ─── Keypress listener ────────────────────────────────────────────────────────
def keypress_listener():
    global QUIT_CLEAN, QUIT_FORCE
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1).lower()
                if ch == 'q':
                    QUIT_CLEAN = True
                    return
                elif ch == 'f':
                    QUIT_FORCE = True
                    return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ─── Exit summary ─────────────────────────────────────────────────────────────
def print_summary(chunks: list, completed: set, failed: set, start_time: float):
    elapsed = time.time() - start_time
    total = len(chunks)
    remaining = [str(i+1) for i, c in enumerate(chunks)
                 if str(c) not in completed and str(c) not in failed]
    completed_nums = [str(i+1) for i, c in enumerate(chunks) if str(c) in completed]
    failed_nums = [str(i+1) for i, c in enumerate(chunks) if str(c) in failed]

    print()
    print("─" * 72)
    print("  Exit summary")
    print(f"  Total elapsed:    {fmt_duration(elapsed)}")
    print(f"  Chunks completed: {', '.join(completed_nums) if completed_nums else 'none'}")
    print(f"  Chunks failed:    {', '.join(failed_nums) if failed_nums else 'none'}")
    print(f"  Chunks remaining: {', '.join(remaining) if remaining else 'none'}")
    print("─" * 72)

# ─── Progress display ─────────────────────────────────────────────────────────
DISPLAY_LINES = 5

class ProgressTracker:
    def __init__(self, total_chunks: int, total_frames: int):
        self.total_chunks = total_chunks
        self.total_frames = total_frames
        self.completed_frames = 0
        self.current_chunk = 0
        self.current_chunk_frames = 0
        self.start_time = time.time()
        self.chunk_start_time = time.time()
        self._initialized = False

    def start_chunk(self, chunk_num: int, chunk_frames: int):
        self.current_chunk = chunk_num
        self.current_chunk_frames = chunk_frames
        self.chunk_start_time = time.time()

    def complete_chunk(self, chunk_frames: int):
        self.completed_frames += chunk_frames

    def render(self, chunk_frames_done: int, chunk_total: int):
        now = time.time()
        elapsed_total = now - self.start_time
        overall_done = self.completed_frames + chunk_frames_done

        if overall_done > 0:
            rate = overall_done / elapsed_total
            remaining_frames = self.total_frames - overall_done
            eta = remaining_frames / rate if rate > 0 else 0
        else:
            eta = 0

        overall_pct = overall_done / self.total_frames if self.total_frames > 0 else 0
        overall_bar = self._bar(overall_pct, width=40)

        chunk_pct = chunk_frames_done / chunk_total if chunk_total > 0 else 0
        chunk_bar = self._bar(chunk_pct, width=30)

        chunk_elapsed = now - self.chunk_start_time
        chunk_fps = chunk_frames_done / chunk_elapsed if chunk_elapsed > 0 and chunk_frames_done > 0 else 0

        lines = [
            "─" * 72,
            f"  Overall  [{overall_bar}] {overall_pct*100:5.1f}%"
            f"  {fmt_frames(overall_done)}/{fmt_frames(self.total_frames)} frames"
            f"  ETA {fmt_duration(eta)}",
            f"  Chunk {self.current_chunk}/{self.total_chunks}"
            f"  [{chunk_bar}] {chunk_pct*100:5.1f}%"
            f"  {fmt_frames(chunk_frames_done)}/{fmt_frames(chunk_total)} frames"
            f"  {chunk_fps:.1f} fps"
            f"  Elapsed {fmt_duration(chunk_elapsed)}",
            "─" * 72,
            "  Q = clean exit    F = forced exit",
        ]

        if self._initialized:
            sys.stdout.write(f"\033[{DISPLAY_LINES}A")
        else:
            self._initialized = True

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

    def _bar(self, pct: float, width: int = 40) -> str:
        filled = int(width * pct)
        return "█" * filled + "░" * (width - filled)

    def print_initial(self):
        sys.stdout.write("\n" * DISPLAY_LINES)
        sys.stdout.flush()

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

# ─── Run lada-cli ─────────────────────────────────────────────────────────────
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
            print()
            for err in error_lines[-5:]:
                log.error(err)

        return proc.returncode == 0, error_lines

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

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    global QUIT_CLEAN, QUIT_FORCE

    parser = argparse.ArgumentParser(
        description="Split, restore with lada-cli, and concatenate video.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="Input video file")
    parser.add_argument("--output", required=True, help="Output video file")

    args, extra_args = parser.parse_known_args()

    input_path = Path(args.input).resolve()
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    job_id = input_hash(input_path)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{job_id}.json"
    log_file = STATE_DIR / f"{job_id}.log"

    setup_logging(log_file)
    log.info(f"Job ID: {job_id} ({input_path.name})")
    log.info("Press Q for clean exit, F for forced exit.")

    state = load_state(state_file)
    resuming = bool(state)

    if resuming:
        log.info(f"Resuming previous job for: {input_path.name}")
        work_dir = Path(state["work_dir"])
        chunks = [Path(c) for c in state["chunks"]]
        chunk_frames = state["chunk_frames"]
        completed = set(state.get("completed", []))
        failed = set(state.get("failed", []))
    else:
        log.info(f"Starting new job for: {input_path.name}")
        work_dir = TEMP_BASE / f"lada_{job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        chunks = split_video(input_path, work_dir)
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
        }
        save_state(state_file, state)

    total_frames = sum(chunk_frames.values())
    total_chunks = len(chunks)
    tracker = ProgressTracker(total_chunks, total_frames)
    tracker.start_time = time.time()

    for c in completed:
        tracker.completed_frames += chunk_frames.get(c, 0)

    log.info(f"Chunks: {total_chunks} | Total frames: {fmt_frames(total_frames)}")
    log.info(f"Completed: {len(completed)} | Remaining: {total_chunks - len(completed) - len(failed)}")

    # Start keypress listener thread
    listener = Thread(target=keypress_listener, daemon=True)
    listener.start()

    # Process chunks
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

        def attempt(attempt_num: int) -> bool:
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

        if attempt(1):
            log.info(f"Chunk {idx}/{total_chunks} completed.")
            tracker.complete_chunk(frames)
            completed.add(chunk_key)
            failed.discard(chunk_key)
        elif not QUIT_CLEAN and not QUIT_FORCE:
            log.warning(f"Chunk {idx}/{total_chunks} failed. Retrying...")
            restored.unlink(missing_ok=True)
            tracker.start_chunk(idx, frames)
            tracker.print_initial()
            if attempt(2):
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

    # Handle quit
    if QUIT_CLEAN or QUIT_FORCE:
        quit_type = "Clean" if QUIT_CLEAN else "Forced"
        log.info(f"{quit_type} exit requested.")
        print_summary(chunks, completed, failed, tracker.start_time)
        sys.exit(0)

    if not completed:
        log.error("No chunks completed successfully. Aborting.")
        sys.exit(1)

    if failed:
        log.warning(f"{len(failed)} chunk(s) failed and will be missing from output.")

    # Concatenate
    restored_chunks = [
        work_dir / f"restored_{chunk.name}"
        for chunk in chunks if str(chunk) in completed
    ]
    concatenate(restored_chunks, output_path, work_dir)

    # Cleanup
    log.info("Cleaning up temporary files...")
    shutil.rmtree(work_dir)
    state_file.unlink(missing_ok=True)

    log.info("Done.")

if __name__ == "__main__":
    main()