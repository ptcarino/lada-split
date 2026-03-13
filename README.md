# lada-split

> ⚠️ **AI Disclaimer**: This script and this README were written entirely by an AI assistant (Claude by Anthropic). They are provided as-is with no guarantees of correctness, stability, or fitness for any particular purpose. Use at your own risk.

---

A batch processing wrapper around [lada-cli](https://github.com/ladaapp/lada) that splits a video into chunks, runs mosaic restoration on each chunk sequentially, then concatenates the results into a single output file. The entire process is resumable — if interrupted for any reason, re-running the script with the same input will skip already completed chunks and pick up from where it left off.

Designed for community-built LadaApp installations with AMD GPU support, where processing a full video in one shot may cause GPU memory fragmentation or OOM crashes — particularly on AMD GPUs running ROCm on WSL2.

Note that this does not guarantee mitigation of GPU memory fragmentation or OOM crashes; it only reduces the likelihood by processing smaller pieces at a time.

---

## Requirements

- Python 3.12+
- `ffmpeg` and `ffprobe` in PATH
- [LadaApp](https://github.com/ladaapp/lada) installed with a working `lada-cli`
- AMD GPU with ROCm support (script is configured for ROCm on WSL2)

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
```

---

## Usage

```bash
lada-split --input video.mp4 --output /path/to/output.mp4
```

Any additional arguments not recognised by the script are passed directly to `lada-cli`:

```bash
lada-split --input video.mp4 --output /path/to/output.mp4 --max-clip-length 30
```

---

## Features

### Chunked processing
The input video is split into fixed-duration chunks using `ffmpeg`. Each chunk is processed by a separate `lada-cli` invocation, ensuring GPU memory is fully released between chunks.

### Resume support
Job state is saved to a JSON file keyed by the MD5 hash of the input filename. If the script is interrupted, re-running it with the same `--input` will skip already completed chunks and continue from where it left off.

### Output validation
After each chunk is processed, the script verifies:
- The output file exists
- Its duration matches the source chunk within a 2-second tolerance

If validation fails, the chunk is retried once before being marked as failed.

### Progress display
A live-updating progress display shows:
```
────────────────────────────────────────────────────────────────────────
  Overall  [████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░]  30.0%  65,940/219,803 frames  ETA 0:45:12
  Chunk 3/9  [██████████████░░░░░░░░░░░░░░░░]  46.5%  12,561/27,000 frames  13.0 fps  Elapsed 0:16:42
────────────────────────────────────────────────────────────────────────
  Q = clean exit    F = forced exit
```

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
- Hardware video encoding (VA-API, AMF) availability in WSL2 is dependent on GPU driver support. To check if your WSL instance has GPU passthrough enabled, run:
  ```bash
  ls /dev/dri/
  ```
  If you see devices like `card0` and `renderD128`, GPU passthrough is active and hardware encoding may be available. If the directory is empty or missing, the GPU is not accessible from WSL and lada-cli will fall back to CPU encoding.