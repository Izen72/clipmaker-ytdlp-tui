# Troubleshooting (ClipMaker / ytfrags TUI)

If you’re here, something is either broken, annoying, or YouTube is being… YouTube.

Back to README: **[README.md](README.md)**

---

## 1) “It says *n challenge solving failed* / EJS / Some formats may be missing”

### Symptoms
- Logs show something like:
  - `n challenge solving failed`
  - “Some formats may be missing”
  - References EJS / JS runtimes being unavailable

### What it means
The internal `yt-dlp` library sometimes needs a working JavaScript runtime to solve YouTube challenges and unlock specific high-quality formats.

### Fix
Install **Node.js** and make sure it’s on your system PATH.

Quick check in a terminal:
```bash
node -v

```

If that works, re-run ClipMaker.

*If you can’t or don’t want Node installed, you may still be able to download something, but you might lose access to certain resolutions.*

---

## 2) Auth / bot wall / cookies required (403/429, “sign in”, captcha, etc.)

### Symptoms

* Errors include terms like:
* sign in / login
* “confirm you’re not a bot”
* captcha
* HTTP 403 / 429
* “cookies” / “age-restricted” / “members-only”



### What ClipMaker does automatically

ClipMaker attempts to solve this in stages:

1. **No cookies** (try anonymously first).
2. If blocked, **Auto-Cookie Fallback**:
* **Zen Browser** (Native support!)
* **Firefox**
* **Chromium-family** (Chrome/Edge/Brave/Vivaldi/Opera/Safari etc.)



### Manual confidence test

If you want to verify your browser cookies work *outside* the app, run a probe using the installed library.

*(Note: We use `uv run` to ensure we use the version installed in your project folder)*

```bash
# Example probing Firefox
uv run yt-dlp -v --cookies-from-browser firefox --print title "[https://www.youtube.com/watch?v=VIDEO_ID](https://www.youtube.com/watch?v=VIDEO_ID)"

```

### If you still get blocked

* Try a different browser profile where you are definitely logged in.
* **Close the browser** before running the tool (browsers lock their cookie databases, though ClipMaker tries to copy them to bypass this).
* If you are rate-limited (429), wait a while or change your IP (VPN/Mobile Data).

---

## 3) “Audio downloaded as a .webm video with a black screen”

### Why it happens

YouTube streams high-quality Opus audio inside a **WebM** container. Some players treat this as a "Video" (displaying a black screen).

### The Fix

**ClipMaker (v2.1+) now automatically fixes this.**

* It detects the `.webm` audio file and instantly converts it to a proper **`.opus`** file (without re-encoding/quality loss).
* If you still see a `.webm` file, the conversion step likely failed (missing ffmpeg?).

**Manual fix:**
You can fix leftovers manually. (If you use a custom ffmpeg path, replace `ffmpeg` with your path).

```bash
ffmpeg -i input.webm -vn -c:a copy output.opus

```

---

## 4) “I picked MP4 but it says 'Remuxing' instead of downloading directly?”

### Why it happens

Modern streaming sites prioritize efficient codecs (AV1, VP9) which usually come in MKV/WebM containers. They often *do not* provide a high-quality H.264/MP4 stream directly.

### Old behavior vs New behavior

* **Old way (Transcoding):** Downloaded MKV -> Slowly re-encoded video to H.264. High CPU usage, quality loss.
* **New way (Remuxing):** Downloads the **best** source (AV1/VP9) -> Copies the video stream into an MP4 container.
* **Speed:** Instant.
* **Quality:** Lossless (original source quality).
* **Compatibility:** Audio is converted to AAC to ensure the MP4 plays on Apple devices/QuickTime.



*If you strictly need legacy H.264 (for very old hardware), select **Method: Direct**. If that fails, ClipMaker will still fall back to Remuxing to ensure you at least get a file.*

---

## 5) “My time range isn’t accepted”

### Accepted formats

* `SS` / `MM:SS` / `HH:MM:SS` (optional `.ms`)
* End can be `inf` (end of video)
* Negative values (e.g. `-30`) count back from the end.

### Common gotchas

* **Identical Start/End** → Rejected.
* **Start=0 / End=0** → Only allowed if Fragment Count is **1** (downloads full video).

---

## 6) “Where is my theme saved?”

ClipMaker automatically saves your last used theme (selected via `Ctrl+T` or `Ctrl+P`) to a configuration file.

**Locations:**

* **Windows**: `%APPDATA%\ytfrags\settings.json`
* **macOS**: `~/Library/Application Support/ytfrags/settings.json`
* **Linux**: `~/.config/ytfrags/settings.json`

If settings aren't persisting, check permissions on that folder.

---

## 7) “It says ffmpeg not found”

### The Fix

**FFmpeg is mandatory** for cutting and remuxing.

**Option A: Install nicely (Recommended)**

* **Windows**: `winget install ffmpeg` (or add to PATH).
* **macOS**: `brew install ffmpeg`
* **Linux**: `sudo apt install ffmpeg`

**Option B: Point to a specific binary**
If you have a portable version or don't want to edit your system PATH, use the argument:

```bash
python ytfrags_tui.py --ffmpeg-path "C:\Path\To\ffmpeg.exe"

```

*(Note: You do NOT need to install `yt-dlp` manually; ClipMaker uses its own internal Python library version.)*

---

## Still stuck?

If the failure is site-specific, press **D** (Details) during the download to see the raw logs, or check the full log file saved in:
`./_logs/ytfrags_YYYY-MM-DD_HHMMSS.log`
