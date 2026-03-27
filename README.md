# lada-split

> ⚠️ **AI Disclaimer**: This script and this README were written entirely by an AI assistant (Claude by Anthropic). They are provided as-is with no guarantees of correctness, stability, or fitness for any particular purpose. Use at your own risk.

---

A Windows-native batch processing wrapper around [lada-cli.exe](https://codeberg.org/ladaapp/lada) that optionally splits a video into chunks, runs mosaic restoration on each chunk sequentially, then concatenates the results into a single output file. The entire process is resumable — if interrupted for any reason, re-running the script with the same input will skip already completed chunks and pick up from where it left off.

Use `--no-chunk` to skip splitting entirely and process the whole video in a single lada-cli call — useful when your GPU can handle the full file without running out of VRAM.

Designed for the pre-built Windows lada distribution with AMD GPU support via native Windows ROCm. Requires no WSL2, no Linux environment, and no virtual environment activation.

---

## Requirements

- Python 3.12+ (on Windows PATH)
- `ffmpeg` and `ffprobe` on Windows PATH (Gyan.dev full build recommended — includes AMF support)
- [LadaApp pre-built Windows release](https://codeberg.org/ladaapp/lada) with `lada-cli.exe`
- AMD GPU with Windows ROCm support
- `rich` Python library (`pip install rich`)

---

## Installation

1. Place `lada-split.py` anywhere convenient (e.g. `F:\lada\lada-split.py`).
2. Edit the config section at the top of the script to match your system:

```python
CHUNK_DURATION = 600            # seconds per chunk (default: 10 minutes)
LADA_CLI       = Path(r"F:\lada\lada-cli.exe")
LADA_FIXED_ARGS = [
    "--mosaic-detection-model",   "v4-accurate",
    "--mosaic-restoration-model", "basicvsrpp-v1.2",
    "--encoding-preset",          "hevc-amd-gpu-hq",
]
TEMP_BASE  = Path(r"F:\lada_tmp")        # where temp/chunk files are stored
STATE_DIR  = Path(r"F:\lada_tmp\.lada_state")  # where job state is saved
SHUTDOWN_COUNTDOWN    = 300     # seconds before auto-shutdown (default: 5 minutes)
SHUTDOWN_WINDOW_START = 3       # earliest hour shutdown is allowed (03:00)
SHUTDOWN_WINDOW_END   = 7       # latest hour shutdown is allowed (07:00)
```

---

## Usage

### Single file

```powershell
# Explicit output path
python lada-split.py --input video.mp4 --output E:\exports\output.mp4

# Output directory with default pattern ({orig_file_name}-MR)
python lada-split.py --input video.mp4 --output-dir E:\exports

# Output directory with custom pattern
python lada-split.py --input video.mp4 --output-dir E:\exports --output-pattern "{orig_file_name}-restored"
```

### No-chunk mode (process whole file at once)

```powershell
python lada-split.py --input video.mp4 --output E:\exports\output.mp4 --no-chunk
```

Skips splitting entirely — lada-cli processes the full file in one invocation. Useful when the GPU has enough VRAM to handle the video without chunking. All other flags (`--pre-downscale`, `--skip-upscale`, `--output-res`, `--delete-input`, etc.) remain compatible.

### Directory input

Process all `.mp4` files in a directory sequentially:

```powershell
python lada-split.py --input-dir E:\videos --output-dir E:\exports

# With custom pattern
python lada-split.py --input-dir E:\videos --output-dir E:\exports --output-pattern "{orig_file_name}-MR"
```

`--input` and `--input-dir` are mutually exclusive. When using `--input-dir`, processing stops immediately on the first failure.

### Output pattern

The `--output-pattern` flag uses `{orig_file_name}` as a placeholder for the input filename stem. The file extension is always taken from the source file automatically — do not include an extension in the pattern.

Default pattern: `{orig_file_name}-MR`

### Passing extra args to lada-cli

Use `--args` to pass additional flags to lada-cli as a single quoted string:

```powershell
python lada-split.py --input video.mp4 --output E:\exports\output.mp4 --args "--max-clip-length 60"
python lada-split.py --input video.mp4 --output E:\exports\output.mp4 --args "--detect-face-mosaics --max-clip-length 60"
```

### Pre-downscale

Downscale input before processing and upscale output back to original resolution after:

```powershell
# Default 720p downscale
python lada-split.py --input video.mp4 --output E:\exports\output.mp4 --pre-downscale

# Specific resolution
python lada-split.py --input video.mp4 --output E:\exports\output.mp4 --pre-downscale 540p

# Custom upscale output resolution
python lada-split.py --input video.mp4 --output E:\exports\output.mp4 --pre-downscale --output-res 1920x1080

# Skip upscale — output stays at downscaled resolution
python lada-split.py --input video.mp4 --output E:\exports\output.mp4 --pre-downscale --skip-upscale
```

### Remove a job

Remove all temporary files and state for a specific job ID:

```powershell
python lada-split.py --remove-job <job_id>
```

This is a standalone operation — no other flags are needed. It removes the work directory, state file, and log file for the given job ID, and prints each item as it is deleted. The original input file is not touched.

The `job_id` is printed at the start of every run.

### Delete input after completion

```powershell
python lada-split.py --input video.mp4 --output E:\exports\output.mp4 --delete-input
```

Only deletes the original input file if processing completed successfully. In chunked mode, only deletes if no chunks failed.

---

## Flags reference

| Flag | Shortcut | Argument | Description |
|------|----------|----------|-------------|
| `--input` | `-i` | `FILE` | Input video file. Mutually exclusive with `--input-dir`. |
| `--input-dir` | — | `DIR` | Process all `.mp4` files in a directory sequentially. Mutually exclusive with `--input`. |
| `--output` | `-o` | `FILE` | Output video file path. Required with `--input` unless `--output-dir` is used. |
| `--output-dir` | — | `DIR` | Output directory. Uses `--output-pattern` for filenames. |
| `--output-pattern` | `-p` | `PATTERN` | Output filename pattern using `{orig_file_name}` placeholder. Extension taken from source. Default: `{orig_file_name}-MR`. |
| `--no-chunk` | — | — | Skip splitting — process the whole video in one lada-cli call. Compatible with `--pre-downscale`, `--skip-upscale`, `--output-res`. |
| `--pre-downscale` | — | `[RESOLUTION]` | Downscale input before processing. Defaults to `720p` if no value given. |
| `--output-res` | — | `WxH` | Override upscale output resolution (e.g. `1920x1080`). Only valid with `--pre-downscale`. |
| `--skip-upscale` | — | — | Skip upscale step after processing. Output stays at downscaled resolution. Only valid with `--pre-downscale`. Mutually exclusive with `--output-res`. |
| `--args` | — | `"LADA_ARGS"` | Additional arguments to pass to lada-cli, as a single quoted string. |
| `--delete-input` | — | — | Delete original input file after successful completion. |
| `--shutdown-after` | — | — | Shut down Windows after successful completion, within configured time window. |
| `--remove-job` | `-r` | `JOB_ID` | Remove all temp files and state for a job. Standalone operation. |

---

## Features

### Chunked processing
The input video is split into fixed-duration chunks using `ffmpeg`. Each chunk is processed by a separate `lada-cli` invocation, ensuring GPU memory is fully released between chunks. Use `CHUNK_DURATION` in the config to control chunk length.

### No-chunk mode
When `--no-chunk` is specified, the video is not split — lada-cli processes the entire file in one call. State is still saved so a failed job can be retried. Output validation and the full downscale/upscale pipeline all work the same way.

### Batch / directory processing
When `--input-dir` is specified, all `.mp4` files in the directory are processed sequentially, one at a time. Output filenames are generated using `--output-pattern` (default: `{orig_file_name}-MR`). Processing stops immediately if any file fails. Already-completed jobs are skipped automatically on re-run.

### Resume support
Job state is saved to a JSON file keyed by the MD5 hash of the input file path. If the script is interrupted, re-running it with the same `--input` will resume from where it left off. In chunked mode, already-completed chunks are skipped. In no-chunk mode, the full lada-cli call is retried.

### Output validation
After processing, the script verifies the output file exists and its duration matches the source within a 2-second tolerance. In chunked mode, a failed chunk is retried once before being marked as failed.

### Pre-downscale / upscale
When `--pre-downscale` is specified, the input video is downscaled to the target resolution before processing. After completion, the output is upscaled back to the original input resolution by default, or to a custom resolution if `--output-res WxH` is specified.

Both downscale and upscale use GPU-accelerated AMF via ffmpeg (`vpp_amf` + `h264_amf` for downscale, `vpp_amf` + `hevc_amf` for upscale). Aspect ratio is preserved. If the input is already at or below the target resolution, the downscale step is skipped automatically.

Use `--skip-upscale` to skip the upscale step entirely — the processed file is written directly to the output path at the downscaled resolution. Useful when running the output through an external AI upscaler.

### Progress display
A live-updating progress display powered by `rich` shows the current pipeline phase and processing progress:

**Chunked mode:**
```
  Phase: Processing chunks (3/9)
  Overall              ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   30.0%  65,940/219,803 frames  20.1 fps  0:16:42  ETA 0:45:12
  Chunk 3/9            ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   46.5%  12,561/27,000 frames   20.1 fps  0:16:42  ETA 0:12:30
  Q + Enter = clean exit    F + Enter = forced exit
```

**No-chunk mode:**
```
  Phase: Processing (no-chunk)
  Processing           ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   82.0%  294,401/357,847 frames  28.3 fps  3:24:59  ETA 0:37:20
  Q + Enter = clean exit    F + Enter = forced exit
```

### Quit keys
Press during processing (followed by Enter):

| Key | Behaviour |
|-----|-----------|
| `Q` + Enter | Clean exit — terminates lada-cli, waits up to 30s, saves state |
| `F` + Enter | Forced exit — kills lada-cli immediately, saves state |

Both print an exit summary on quit. State is preserved so the job can be resumed.

### Shutdown after completion
When `--shutdown-after` is specified, the script will trigger a Windows shutdown after successful completion — but only if the current time is within the configured window (default: 03:00–07:00). A countdown is shown before shutdown, and pressing Enter cancels it.

### Delete input after completion
When `--delete-input` is specified, the original input file is deleted after successful completion. In chunked mode, only applies when all chunks completed without failure.

### Remove a job
`--remove-job <job_id>` is a standalone operation that removes all temporary files and state for a given job — work directory, state file, and log file.

---

## Temp files and state

| Path | Contents |
|------|----------|
| `F:\lada_tmp\lada_<job_id>\` | Split chunks, restored chunks, downscaled input |
| `F:\lada_tmp\.lada_state\<job_id>.json` | Job state |
| `F:\lada_tmp\.lada_state\<job_id>.log` | Full lada-cli log |

To reset a job and start from scratch:
```powershell
python lada-split.py --remove-job <job_id>
```

The `job_id` is printed at the start of every run:
```
INFO  Job ID: a3f9c12b04 (video.mp4)
```

---

## Known limitations

- `--fp16` is intentionally not used. On the RX 9070 XT with current Windows ROCm builds, FP32 produces correct restoration results. Using `--fp16` causes the mosaic restoration to silently fail (output appears unprocessed).
- The `[WARNING] failed to run offload-arch: binary not found.` message printed by lada-cli.exe at startup is harmless and can be ignored.
- Output validation uses duration comparison (±2s tolerance). This may occasionally produce false negatives on unusual source files.
- Quit keys require an interactive terminal. They will not work if the script is piped or backgrounded without a TTY.