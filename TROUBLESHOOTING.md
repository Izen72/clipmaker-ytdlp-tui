# Troubleshooting (ClipMaker / ytfrags TUI)

If you’re here, something is either broken, annoying, or YouTube is being… YouTube.

Back to README: **[README.md](README.md)**

---

## 1) “It says *n challenge solving failed* / EJS / Some formats may be missing”

### Symptoms
- yt-dlp prints something like:
  - `n challenge solving failed`
  - “Some formats may be missing”
  - References EJS / JS runtimes being unavailable

### What it means
yt-dlp sometimes needs a working JavaScript runtime to solve YouTube challenges and unlock formats.

### Fix
Install **Node.js** and make sure it’s on PATH.

Quick check:

```bash
node -v
```

If that works, re-run ClipMaker (or your manual yt-dlp command).

If you can’t or don’t want Node installed, you may still be able to download *something*, but you might lose higher-quality formats.

---

## 2) Auth / bot wall / cookies required (403/429, “sign in”, captcha, etc.)

### Symptoms
- yt-dlp errors include terms like:
  - sign in / login
  - “confirm you’re not a bot”
  - captcha
  - HTTP 403 / 429
  - “cookies” / “age-restricted” / “members-only”

### What ClipMaker does automatically
ClipMaker always tries:
1) **No cookies**
2) If that looks like an auth/bot-wall failure → **auto cookie fallback**:
   - **Zen** (Firefox-based)
   - **Firefox**
   - **Chromium-family** browsers (Chromium/Chrome/Brave/Edge/Vivaldi/Opera/Whale/Safari where relevant)

### Manual confidence test (recommended)
If you want to test whether Zen cookies are usable *outside* the app, run a quick probe:

```bash
yt-dlp -v \
  --cookies-from-browser firefox:"$HOME/.zen" \
  --no-playlist \
  --skip-download --print title \
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

If you see something like:
- `Found YouTube account cookies`
- and it prints the title successfully

…then the cookie extraction path is working, and ClipMaker’s fallback has a good chance of working too.

### Why `"$HOME/.zen"` is nicer than `"$HOME/.zen/<profile>.Default (release)"`
That `<profile>` bit (e.g., `oplhmacu`) can be per-install / per-user / per-machine. Using the **root** directory lets yt-dlp locate the right profile without you hardcoding that random prefix.

### If you still get blocked
- Try a different browser profile where you’re definitely logged in.
- Make sure the browser is **closed** when you run cookie extraction (some systems lock DBs aggressively).
- If you’re getting rate-limited (429), wait a while or change IP.

---

## 3) “Audio downloaded as a .webm video with a black screen”

### Why it happens
Some sites (YouTube included) commonly deliver audio streams in a container like **WebM** (often with Opus audio).  
Many players display that as “a video” (because the container is often associated with video), even when it’s audio-only — hence the “black screen”.

### What ClipMaker does
Audio-only mode tries to prefer *audio-ish* containers when they exist (e.g., `.m4a` / AAC), without re-encoding.  
If the site only offers WebM audio, yt-dlp may still deliver WebM — and that’s still valid audio.

### If you need a different extension/container
You can remux *without re-encoding* (fast, no quality loss). Example: WebM/Opus → Ogg/Opus:

```bash
ffmpeg -i input.webm -c copy output.ogg
```

Or WebM/Opus → Opus-in-Ogg explicitly:

```bash
ffmpeg -i input.webm -vn -c:a copy output.opus
```

(Exact best target depends on your editor/player.)

---

## 4) “I picked MP4 but it still produced MKV / or it transcoded”

### Why it happens
Not all sources provide MP4-friendly codecs (H.264 video + AAC audio) in a way yt-dlp can download directly.  
When direct MP4 isn’t viable, ClipMaker automatically falls back to:
- **MKV download** (the “always works” path)
- then **transcode** to MP4 (H.264 + AAC)

### What to expect
- Direct MP4 is the fastest path when available.
- Fallback transcode costs time/CPU, but is reliable and yields MP4 compatibility.

---

## 5) “My time range isn’t accepted” (or downloads the wrong thing)

### Accepted formats
Start/end supports:
- `SS` / `MM:SS` / `HH:MM:SS` (optional `.ms`)
- End can be `inf`
- Negative values are relative to the end (e.g., `-30`)

### Common gotchas
- **Start and end identical** → rejected
- `Start=0` & `End=0` is a special “full video” shortcut **only when fragment count is 1**
- For long VODs, prefer **H:MM:SS** to stay sane

---

## 6) “The theme resets when I reopen the app”

### What’s going on
Picking a theme at runtime (Ctrl+T / Command Palette) changes the theme *for that session*, but ClipMaker won’t remember it unless you set it programmatically.

### Fix (set a default theme in code)
In `YtFragsApp.on_mount`, set `self.theme`:

```python
class YtFragsApp(App):
    def on_mount(self) -> None:
        self.theme = "tokyo-night"
        self.push_screen(WizardScreen())
```

That forces the app to start in that theme every run.

If you want true persistence (remember the last selected theme), you’d store it in a config file and reload it on startup — but “set a default” is the simple win.

---

## 7) “Do the in-app logs look sane?”

### Short answer
Yes — the log design is intentional.

### How it’s structured
- **Activity** shows important, human-scannable events:
  - destinations, warnings/errors, key yt-dlp stages, ffmpeg stages
- **Details** (press **D**) is for raw-ish output when you need to diagnose:
  - format selection, headers, ffmpeg progress lines, etc.
- Everything is always written to `./_logs/ytfrags_*.log`

### Tip
If you’re debugging auth or format weirdness, use **Details** + open the saved log file for the full context.

---

## 8) “It says yt-dlp or ffmpeg not found”

### Fix
- Ensure `yt-dlp` is installed and on PATH:
  ```bash
  yt-dlp --version
  ```
- Ensure `ffmpeg` is installed and on PATH:
  ```bash
  ffmpeg -version
  ```

ClipMaker will refuse to start downloads if either is missing (by design).

---

## Still stuck?

If the failure is site-specific, grab the exact command shown in **Review**, run it in a terminal with `-v`, and attach the relevant `_logs/ytfrags_*.log` snippet when you ask for help.
