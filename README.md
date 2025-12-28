# ClipMaker (ytfrags TUI)

A keyboard-first TUI wizard for downloading **time-range fragments** from a video. 

It uses the native **yt-dlp Python library** (no separate binary required) and **ffmpeg** to provide a fast, robust, and cross-platform experience.

If you’ve ever needed “only the 00:42:10–00:43:37 moment” from a VOD (and then 17 more moments), this is that.

---

## What it does

- **Fragment wizard**: paste URL → choose fragment count → choose output → type start/end per fragment → review → download.
- **Cross-Platform**: Fully supported on **Windows, macOS, and Linux**.
- **Two output modes**
  - **Video**
    - **MKV (default)**: best video ≤1080p + best audio (prioritizes "God Tier" codecs like AV1/VP9).
    - **MP4**: tries MP4-friendly H.264 + AAC; if unavailable, it auto-falls back to **Remuxing**.
      * *Remux mode downloads the best quality source (AV1/VP9) and wraps it into an MP4 container instantly, preserving original quality and small file size.*
  - **Audio-only**: downloads **best available audio**. Prioritizes **Opus** (highest efficiency). If it gets a WebM container, it automatically remuxes it to `.opus`. Fallback to AAC/M4A if Opus is missing.
- **Auth-on-fail** (YouTube etc.)
  - First attempt: **no cookies**
  - If it fails with “sign in / bot wall / captcha / 403 / 429 / cookies” vibes, it automatically retries using browser cookies:
    1) **Zen (Firefox-based)** - *Native support!*
    2) Firefox
    3) Chromium-family browsers (Chrome/Edge/Brave/Vivaldi/Opera/Safari etc.)
- **Logs**
  - “Activity” log is curated (keeps the important lines)
  - “Details” log shows raw-ish output (toggle with **D**)
  - Full output is also saved to `./_logs/ytfrags_YYYY-MM-DD_HHMMSS.log`

---

## Requirements

### Python Environment
- Python 3.10+ (tested on 3.12)
- **yt-dlp** is installed automatically as a Python library (via `requirements.txt`). You do **not** need to install the `yt-dlp` binary on your system path.

### System Tools
- **uv** (must be on PATH). Used for high-speed dependency installation and virtual environment management.
  - **Windows**: `winget install --id=astral-sh.uv -e`
  - **macOS**: `brew install uv`
  - **Linux**: Use your package manager ('sudo pacman -S uv' for Arch)
or `curl -LsSf https://astral.sh/uv/install.sh | sh`
  
*(Note: You may need to restart your terminal after installing uv)*
  
- **ffmpeg** (must be on PATH). Used for high-speed cutting and remuxing.
  > *Portable install? You can point to it directly with `--ffmpeg-path "C:\path\to\ffmpeg.exe"` / `--ffmpeg-path "/path/to/ffmpeg"`.*

### Optional (highly recommended for YouTube)
- **Node.js** on PATH  
  YouTube sometimes triggers an “n challenge” / EJS warning; having a JS runtime helps the internal library unlock formats.
---

## Install

### 0. Make sure uv is installed (read Requirements)

This project uses **uv** for fast, reproducible installs.

### 1. Clone the repo and create a virtual environment
```bash
git clone https://github.com/Izen72/clipmaker-ytdlp-tui.git
cd clipmaker-ytdlp-tui
uv venv
```

### 2. Activate the environment:
**Windows:**
```bash
.venv\Scripts\activate
```
**Linux/macOS:**
```bash
source .venv/bin/activate
```
**Linux with the Fish shell (if Saba is a fish; a *certified* feesh 🐟✨):**
```bash
source .venv/bin/activate.fish
```

### 3. Install dependencies
```bash
uv pip install -r requirements.txt
```

**Install FFmpeg:**

* **Windows**: `winget install ffmpeg` (or download binaries and add to PATH)
* **macOS**: `brew install ffmpeg`
* **Linux**: `sudo pacman -S ffmpeg` (or `dnf`, `apt`, etc.)

---

## Run

```bash
python ytfrags_tui.py

```

Startup defaults:

* `--mp4` → default container = MP4 (video-only)
* `--audio` → default media = audio-only (container ignored)
* `--remux` → default MP4 method = Remux (prioritizes Quality/Size over legacy H.264)
* `--zen-profile-path "/path/to/profile"` → optional Zen cookie profile override
* `--ffmpeg-path "/path/to/custom/ffmpeg"` → override system ffmpeg (default: `ffmpeg`)

Examples:

```bash
# Get MP4s with high efficiency (AV1/VP9 inside MP4)
python ytfrags_tui.py --mp4 --remux

# Audio mode
python ytfrags_tui.py --audio

# Point to a specific Zen Browser profile & custom ffmpeg
python ytfrags_tui.py --zen-profile-path "$HOME/.zen/oplhmacu.Default (release)" --ffmpeg-path "/opt/ffmpeg/bin/ffmpeg"

```

---

## Controls

### Wizard / general

* **Enter**: confirm / next
* **← / →**: change toggles (fragment count, media, container)
* **Tab**: focus
* **PgUp / PgDn**: scroll
* **Ctrl+Q**: quit

### Download screen

* **D**: toggle Details log
* **Ctrl+C**: cancel (sends terminate to the active process)
* **Enter**: exit when done

### Theme (Textual)

* **Ctrl+T**: cycle theme
* **Ctrl+P**: Command Palette (includes theme selection)

> Want it to *not* reset theme on every run? See: **Troubleshooting → Theme resets**.

---

## Time range syntax

Accepted inputs (start and end):

* `SS` (e.g., `75`)
* `MM:SS` (e.g., `10:15`)
* `HH:MM:SS` (e.g., `1:02:03`)
* Optional milliseconds: `HH:MM:SS.ms`
* End may be `inf`
* Negative values (e.g., `-30`) are relative to the end of the video

**Special shortcut**

* If fragment count is **1**, then **Start=0 and End=0** downloads the **FULL** video (no trimming).

---

## Output location

By default, downloads go to:

* **Windows**: `User\Videos\yt-fragments`
* **macOS**: `User/Movies/yt-fragments`
* **Linux**: `$XDG_VIDEOS_DIR/yt-fragments` (or `~/Videos/yt-fragments`)

---

## Troubleshooting

If anything acts up (auth, missing formats, audio container weirdness, theme resetting, etc.):

➡️ **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**

---

## Notes on quality

* **Audio-only mode**:
  * Prioritizes **Opus** (the "AV1 of Audio") for maximum quality/size efficiency.
  * If the source is WebM/Opus, it automatically **remuxes** it to a clean `.opus` file (lossless copy).
  * Falls back to high-bitrate AAC (`.m4a`) if Opus is unavailable.


* **MP4 mode (Remux vs Direct)**:
  * **Direct**: Tries to download native H.264. This is compatible with ancient players but is often lower quality or larger file size.
  * **Remux**: Downloads the best modern stream (AV1 or VP9) and copies it into an MP4 container. Audio is converted to AAC for compatibility.
  * *Result:* You get the **small file size** and **high quality** of AV1, inside an **MP4 file** that plays on all modern devices.
