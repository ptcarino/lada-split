# lada-split

> ⚠️ **AI Disclaimer**: This script and this README were written entirely by an AI assistant (Claude by Anthropic). They are provided as-is with no guarantees of correctness, stability, or fitness for any particular purpose. Use at your own risk.

---

A batch processing wrapper around [lada-cli](https://github.com/ladaapp/lada) that splits a video into chunks, runs mosaic restoration on each chunk sequentially, then concatenates the results into a single output file. The entire process is resumable — if interrupted for any reason, re-running the script with the same input will skip already completed chunks and pick up from where it left off.

Designed for community-built LadaApp installations with AMD GPU support, where processing a full video in one shot may cause GPU memory fragmentation or OOM crashes — particularly on AMD GPUs running ROCm on WSL2.

Note that this does not guarantee mitigation of GPU memory fragmentation or OOM crashes; it only reduces the likelihood by processing smaller pieces at a time.

---

## Requirements

- Python 3.12+
- `ffmpeg` and `ffprobe` in PATH (Linux)
- [LadaApp](https://github.com/ladaapp/lada) installed with a working `lada-cli`
- AMD GPU with ROCm support (script is configured for ROCm on WSL2)
- **Optional**: `ffmpeg.exe` (Windows) in PATH for GPU-accelerated downscale/upscale via AMF. The Gyan.dev full build includes AMF support. If not available, the script falls back to Linux ffmpeg with software scaling automatically.

---

## Installation

Make the script executable:

```bash
chmod +x ~/lada-split.py
```

Optionally, add a symlink to make it callable from anywhere:

```bash
sudo ln -s ~/lada-split.py /usr/local/bin/lada-split
```

Edit the config section at the top of the script to match your system:

```python
CHUNK_DURATION = 600            # seconds per chunk (default: 10 minutes)
LADA_FIXED_ARGS = ["--fp16", "--mosaic-detection-model", "v4-accurate"]
MODEL_WEIGHTS_DIR = "/home/<user>/lada/model_weights"
LADA_VENV_BIN = "/home/<user>/lada/.venv/bin/lada-cli"
TEMP_BASE = Path("/path/to/lada_tmp")        # where temp/chunk files are stored
STATE_DIR = Path("/path/to/lada_tmp/.lada_state")  # where job state is saved
SHUTDOWN_COUNTDOWN = 300        # seconds before auto-shutdown (default: 5 minutes)
SHUTDOWN_WINDOW_START = 3       # earliest hour shutdown is allowed (03:00)
SHUTDOWN_WINDOW_END = 7         # latest hour shutdown is allowed (07:00)
```

---

## Usage

### Single file

```bash
# Explicit output path
lada-split --input video.mp4 --output /path/to/output.mp4

# Output directory with default pattern ({orig_file_name}-MR)
lada-split --input video.mp4 --output-dir /path/to/exports

# Output directory with custom pattern
lada-split --input video.mp4 --output-dir /path/to/exports --output-pattern "{orig_file_name}-restored"
```

### Directory input

Process all `.mp4` files in a directory sequentially:

```bash
lada-split --input-dir /path/to/videos --output-dir /path/to/exports

# With custom pattern
lada-split --input-dir /path/to/videos --output-dir /path/to/exports --output-pattern "{orig_file_name}-MR"
```

`--input` and `--input-dir` are mutually exclusive. When using `--input-dir`, processing stops immediately on the first failure.

### Output pattern

The `--output-pattern` flag uses `{orig_file_name}` as a placeholder for the input filename stem. The file extension is always taken from the source file automatically — do not include an extension in the pattern.

Default pattern: `{orig_file_name}-MR`

### Passing extra args to lada-cli

Any additional arguments not recognised by the script are passed directly to `lada-cli`:

```bash
lada-split --input video.mp4 --output /path/to/output.mp4 --max-clip-length 30
```

### Pre-downscale

Downscale input before processing and upscale output back to original resolution after:

```bash
# Default 720p downscale
lada-split --input video.mp4 --output /path/to/output.mp4 --pre-downscale

# Specific resolution
lada-split --input video.mp4 --output /path/to/output.mp4 --pre-downscale 540p
```

### Shutdown after completion

```bash
lada-split --input video.mp4 --output /path/to/output.mp4 --shutdown-after
```

---

## Features

### Chunked processing
The input video is split into fixed-duration chunks using `ffmpeg`. Each chunk is processed by a separate `lada-cli` invocation, ensuring GPU memory is fully released between chunks.

### Batch / directory processing
When `--input-dir` is specified, all `.mp4` files in the directory are processed sequentially, one at a time. Output filenames are generated using `--output-pattern` (default: `{orig_file_name}-MR`). Processing stops immediately if any file fails. Already-completed jobs are skipped automatically on re-run.

### Resume support
Job state is saved to a JSON file keyed by the MD5 hash of the input filename. If the script is interrupted, re-running it with the same `--input` will skip already completed chunks and continue from where it left off.

### Output validation
After each chunk is processed, the script verifies:
- The output file exists
- Its duration matches the source chunk within a 2-second tolerance

If validation fails, the chunk is retried once before being marked as failed.

### Pre-downscale / upscale
When `--pre-downscale` is specified, the input video is downscaled to the target resolution before splitting into chunks. After concatenation, the output is upscaled back to the original input resolution. This reduces VRAM usage during lada-cli processing at the cost of some image sharpness.

If `ffmpeg.exe` (Windows) is available in PATH, both steps use GPU-accelerated AMF scaling via `vpp_amf`: `h264_amf` for the downscale intermediate and `hevc_amf` for the final upscale output. If `ffmpeg.exe` is not available, both steps fall back to Linux ffmpeg with the Lanczos software scaler.

Accepted values: `720p`, `540p`, `480p`, etc. Defaults to `720p` if no value is given. If the input is already at or below the target resolution, the downscale step is skipped automatically.

### Shutdown after completion
When `--shutdown-after` is specified, the script will trigger a Windows shutdown after successful completion — but only if the current time is within the configured window (default: 03:00–07:00). A countdown is shown before shutdown, and pressing any key cancels it. Configurable via `SHUTDOWN_COUNTDOWN`, `SHUTDOWN_WINDOW_START`, and `SHUTDOWN_WINDOW_END` in the config section.

### Progress display
A live-updating progress display shows:
```
────────────────────────────────────────────────────────────────────────
  Overall  [████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░]  30.0%  65,940/219,803 frames  ETA 0:45:12
  Chunk 3/9  [██████████████░░░░░░░░░░░░░░░░]  46.5%  12,561/27,000 frames  13.0 fps  Elapsed 0:16:42
────────────────────────────────────────────────────────────────────────
  Q = clean exit    F = forced exit
```

The display adapts to the terminal width automatically.

### Quit keys
Press during processing (case-insensitive):

| Key | Behaviour |
|-----|-----------|
| `Q` | Clean exit — sends SIGTERM to lada-cli, waits up to 30s, saves state |
| `F` | Forced exit — sends SIGKILL immediately, saves state |

Both print an exit summary on quit:
```
────────────────────────────────────────────────────────────────────────
  Exit summary
  Total elapsed:    0:42:15
  Chunks completed: 1, 3, 4, 5
  Chunks failed:    2
  Chunks remaining: 6, 7, 8, 9
────────────────────────────────────────────────────────────────────────
```

---

## Temp files and state

| Path | Contents |
|------|----------|
| `$TEMP_BASE/lada_<job_id>/` | Split chunks and restored chunks |
| `$STATE_DIR/<job_id>.json` | Job state (completed/failed chunks) |
| `$STATE_DIR/<job_id>.log` | Full lada-cli log output |

To reset a job and start from scratch:
```bash
rm -rf /path/to/lada_tmp/lada_<job_id> /path/to/lada_tmp/.lada_state/<job_id>.json
```

The `job_id` is printed at the start of every run:
```
[2026-03-13 10:11:57] INFO Job ID: <job_id> (video.mp4)
```

---

## Known limitations

- ROCm on WSL2 with RDNA4 GPUs may experience GPU memory fragmentation causing lada-cli to stall on content with dense mosaic scenes. This is a driver/ROCm limitation and not something the script can fully work around. Reducing `CHUNK_DURATION` or `--max-clip-length` can help but may not eliminate the issue entirely.
- Quit keys require an interactive terminal (TTY). They will not work if the script is run in a non-interactive context (e.g. piped or backgrounded without a TTY).
- Hardware video encoding via AMF (`vpp_amf`, `h264_amf`, `hevc_amf`) is used automatically for downscale and upscale steps when `ffmpeg.exe` is available in PATH. This requires a Windows ffmpeg build with AMF support (e.g. Gyan.dev full build) and an AMD GPU with working AMF drivers. If `ffmpeg.exe` is not found, the script falls back to Linux ffmpeg with software scaling transparently. Note that AMF is used only for the downscale/upscale steps — lada-cli chunk processing always runs through ROCm on the Linux side.