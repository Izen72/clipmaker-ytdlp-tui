# ClipMaker (ytfrags TUI)

A keyboard-first TUI wizard for downloading **time-range fragments** from a video with **yt-dlp** (and optional ffmpeg fallback).

If you’ve ever needed “only the 00:42:10–00:43:37 moment” from a VOD (and then 17 more moments), this is that.

---

## What it does

- **Fragment wizard**: paste URL → choose fragment count → choose output → type start/end per fragment → review → download.
- **Two output modes**
  - **Video**
    - **MKV (default)**: best video ≤1080p + best audio
    - **MP4**: tries MP4-friendly H.264 + AAC; if unavailable it auto-falls back to **MKV download → transcode to MP4**
  - **Audio-only**: downloads **best available audio** (no re-encode). It also tries to prefer *audio-ish* containers (commonly `.m4a`) when the source offers them.
- **Auth-on-fail** (YouTube etc.)
  - First attempt: **no cookies**
  - If it fails with “sign in / bot wall / captcha / 403 / 429 / cookies” vibes, it automatically retries using browser cookies:
    1) **Zen (Firefox-based)**
    2) Firefox
    3) Chromium-family browsers (Chromium/Chrome/Brave/Edge/Vivaldi/Opera/Whale/Safari where applicable)
- **Logs**
  - “Activity” log is curated (keeps the important lines)
  - “Details” log shows raw-ish output (toggle with **D**)
  - Full output is also saved to `./_logs/ytfrags_YYYY-MM-DD_HHMMSS.log`

---

## Requirements

### Python
- Python 3.10+ (tested on 3.12)

### Python packages
All Python dependencies are listed in **`requirements.txt`** (generated from a known-good environment).

Key ones:
- `textual`
- `yt-dlp`
- `uvloop`
### System tools
- **ffmpeg** (must be on PATH)

### Optional (highly recommended for YouTube)
- **Node.js** on PATH  
  YouTube sometimes triggers an “n challenge” / EJS warning in yt-dlp; having a JS runtime helps unlock formats.

---

## Install

This project uses **uv** for fast, reproducible installs.

```bash
git clone <REPO_URL>
cd <REPO_DIR>

uv venv
# (optional) activate the venv in your current shell:
#   source .venv/bin/activate

uv pip install --upgrade pip wheel setuptools uvloop
uv pip install -r requirements.txt
```

Install `ffmpeg` using your OS package manager (e.g., `apt`, `dnf`, `pacman`, `brew`, etc.).

If YouTube gives you EJS / “n challenge” warnings, install Node.js via your OS package manager too (see **Troubleshooting**).

---

## Run
```bash
python ytfrags_tui.py
```

Startup defaults:
- `--mp4` → default container = MP4 (video-only)
- `--audio` → default media = audio-only (container ignored)
- `--zen-profile-path "/path/to/profile-or-cookies.sqlite"` → optional Zen cookie profile override

Examples:

```bash
python ytfrags_tui.py --mp4
python ytfrags_tui.py --audio
python ytfrags_tui.py --zen-profile-path "$HOME/.zen/oplhmacu.Default (release)"
```

---

## Controls

### Wizard / general
- **Enter**: confirm / next
- **← / →**: change toggles (fragment count, media, container)
- **Tab**: focus
- **PgUp / PgDn**: scroll
- **Ctrl+Q**: quit

### Download screen
- **D**: toggle Details log
- **Ctrl+C**: cancel (sends terminate to the active process)
- **Enter**: exit when done

### Theme (Textual)
- **Ctrl+T**: cycle theme
- **Ctrl+P**: Command Palette (includes theme selection)

> Want it to *not* reset theme on every run? See: **Troubleshooting → Theme resets**.

---

## Time range syntax

Accepted inputs (start and end):
- `SS` (e.g., `75`)
- `MM:SS` (e.g., `10:15`)
- `HH:MM:SS` (e.g., `1:02:03`)
- Optional milliseconds: `HH:MM:SS.ms`
- End may be `inf`
- Negative values (e.g., `-30`) are relative to the end of the video

**Special shortcut**
- If fragment count is **1**, then **Start=0 and End=0** downloads the **FULL** video (no trimming).

---

## Output location

By default, downloads go to:

- `$XDG_VIDEOS_DIR/yt-fragments` if `XDG_VIDEOS_DIR` is set  
- otherwise `xdg-user-dir VIDEOS/yt-fragments` (if available)  
- otherwise `~/Videos/yt-fragments`

---

## Troubleshooting

If anything acts up (auth, missing formats, audio container weirdness, theme resetting, etc.):

➡️ **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**

---

## Notes on quality

- **Audio-only mode** aims for *best audio available* (no conversion).  
  “Best” here means the best audio format yt-dlp can fetch according to the selector (often high bitrate AAC in `.m4a`, otherwise Opus-in-WebM, etc.).
- **MP4 mode** is “best effort” because not every source offers MP4-friendly codecs. If the site only offers VP9/AV1 + Opus, direct MP4 may be impossible without transcoding — which ClipMaker does automatically.

---

## License

Your project, your rules. 🙂  
(If you want, add a license file here and reference it.)
