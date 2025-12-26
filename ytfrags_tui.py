# --- BEGIN ytfrags_tui.py ---
#!/usr/bin/env python3
"""
ytfrags_tui.py — an interactive TUI for downloading time-range fragments from a video via yt-dlp.

Wizard flow (keyboard-first):
  1) Paste URL -> press Enter
  2) Choose fragment count with ← / → -> press Enter
  3) Output settings (toggles):
       - Media: Video (default) / Audio
       - Preset (video only): Quality (default) / Compact
       - Container (video only): MKV (default) / MP4
     Notes:
       - CLI flags only set the DEFAULT selection when the app starts:
           --mp4     -> defaults container to MP4 (video-only)
           --audio   -> defaults media to Audio (audio-only; container/preset are ignored)
           --compact -> defaults preset to Compact (video-only)
       - If you pick Audio in the TUI, Preset and MKV/MP4 are not applicable and are skipped/hidden.
  4) For each fragment: fill Start + End (same line) -> press Enter on End to confirm that fragment
     Tip: Start=0 and End=0 (when fragment count is 1) downloads the full video (no trimming).
  5) Review -> press Enter to start download

Output behavior:
  - Video + Preset=Quality + MKV: best video <=1080p + best audio available
  - Video + Preset=Quality + MP4: tries direct MP4-friendly download (H.264 + AAC)
      * If unavailable, auto-fallback: download MKV then transcode to MP4 (H.264/AAC)

  - Video + Preset=Compact: prioritizes SMALL SIZE while keeping reasonable quality:
      * Caps video at <=720p (and prefers <=30fps when possible)
      * Prefers modern efficient codecs (AV1/VP9 when available)
      * Prefers Opus audio with a “not huge” bitrate when possible
      * Uses concurrent fragment downloading (-N) to keep progress reasonably fast
    Container rules still apply:
      - Compact + MKV: “small and efficient” path
      - Compact + MP4: still tries MP4-friendly selection; if it has to fall back and transcode,
                       it will behave exactly like the existing MP4 fallback pipeline.

  - Audio: best audio available (no conversion; container/codec as provided by source)

Auth behavior (YouTube can require cookies sometimes):
  - First attempt is always "no cookies"
  - If the run fails and looks like an auth/bot wall, we try browser cookies (auth-on-fail):
      1) Zen (Firefox-based) via --zen-profile-path (or default "Default (release)")
      2) Firefox
      3) Chromium/Chrome/Brave/Edge/Vivaldi/Opera/Whale/Safari (in popularity-ish order)
  - If all cookie methods fail, we show the user instructions to authenticate via Firefox manually.

Scrolling (inside the app):
  - PgUp / PgDn to scroll
  - Home / End to jump top/bottom
  - Mouse wheel should work in most terminals too

Python deps:
  textual
  yt-dlp

System deps:
  ffmpeg (must be on PATH)

Run:
  python ytfrags_tui.py
  python ytfrags_tui.py --mp4
  python ytfrags_tui.py --audio
  python ytfrags_tui.py --compact
  python ytfrags_tui.py --zen-profile-path "/home/you/.zen/xxxx.Default (release)"
"""

from __future__ import annotations
import json
import argparse
import asyncio
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Deque, List, Optional, Tuple

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Header, Input, Label, RichLog, Static


# ---------------------------
# Optional ASCII art (yours!)
# ---------------------------
ASCII_ART_HEADER = r"""
 ██████╗██╗     ██╗██████╗ ███╗   ███╗ █████╗ ██╗  ██╗███████╗██████╗
██╔════╝██║     ██║██╔══██╗████╗ ████║██╔══██╗██║ ██╔╝██╔════╝██╔══██╗
██║     ██║     ██║██████╔╝██╔████╔██║███████║█████╔╝ █████╗  ██████╔╝
██║     ██║     ██║██╔═══╝ ██║╚██╔╝██║██╔══██║██╔═██╗ ██╔══╝  ██╔══██╗
╚██████╗███████╗██║██║     ██║ ╚═╝ ██║██║  ██║██║  ██╗███████╗██║  ██║
 ╚═════╝╚══════╝╚═╝╚═╝     ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝

                           by Izen · v1.0
"""


def colored_ascii_block(s: str) -> Text:
    """
    Per-character coloring for the ASCII header:
      - block chars (█ etc.) => bright white
      - box/shadow chars (╚═╝ etc.) => red
      - note line(s) at the bottom (e.g., "by Izen · v1.1") => gray
    """
    BLOCK_CHARS = set("█▓▒░▀▄■▌▐▖▗▘▙▟▛▜▝▞▚▁▂▃▄▅▆▇")
    SHADOW_CHARS = set("╚╝╔╗═║╩╦╠╣╬╭╮╯╰┌┐└┘─│┼┴┬├┤")

    def is_note_line(line: str) -> bool:
        t = line.strip()
        if not t:
            return False
        tl = t.lower()
        return (
            tl.startswith("by ")
            or " by " in f" {tl} "
            or "· v" in tl
            or " v" in tl
        )

    # Preserve exact newlines (including trailing blank line if present)
    lines = s.split("\n")

    # Find the first "note" line; everything from that line to the end is gray
    note_start: Optional[int] = None
    for i in range(len(lines)):
        if is_note_line(lines[i]):
            note_start = i
            break

    out = Text()
    for i, line in enumerate(lines):
        if note_start is not None and i >= note_start:
            out.append(line, style="grey62")
        else:
            for ch in line:
                if ch in BLOCK_CHARS:
                    out.append(ch, style="bold bright_white")
                elif ch in SHADOW_CHARS:
                    out.append(ch, style="bold red")
                else:
                    out.append(ch)
        if i != len(lines) - 1:
            out.append("\n")
    return out


MAX_FRAGMENTS = 50

# MP4 fallback transcode settings (H.264 + AAC)
MP4_X264_PRESET = "medium"
MP4_X264_CRF = "18"
MP4_AAC_BITRATE = "320k"

# Compact preset knobs (download-only; no re-encode unless you already hit MP4 fallback transcode path)
COMPACT_MAX_HEIGHT = 720
COMPACT_MAX_FPS = 30
COMPACT_MAX_AUDIO_ABR = 160  # kbps-ish constraint when metadata exists
COMPACT_CONCURRENT_FRAGMENTS = "8"

# Supported cookie browsers (yt-dlp list)
YTDLP_COOKIE_BROWSERS = ["firefox", "chromium", "chrome", "brave", "edge", "vivaldi", "opera", "whale", "safari"]
# Popularity-ish order after Zen + Firefox (Linux leaning)
COOKIE_BROWSER_ORDER = ["firefox", "chromium", "chrome", "brave", "edge", "vivaldi", "opera", "whale", "safari"]


def _find_ytdlp_binary() -> Optional[str]:
    """Use ONLY yt-dlp."""
    return shutil.which("yt-dlp")


def _get_videos_dir() -> Path:
    """
    Best-effort "Videos" directory:
    1) $XDG_VIDEOS_DIR if set
    2) xdg-user-dir VIDEOS (if available)
    3) ~/Videos
    """
    env = os.environ.get("XDG_VIDEOS_DIR")
    if env:
        return Path(env).expanduser()

    if shutil.which("xdg-user-dir"):
        try:
            out = subprocess.check_output(["xdg-user-dir", "VIDEOS"], text=True).strip()
            if out:
                return Path(out).expanduser()
        except Exception:
            pass

    return Path.home() / "Videos"

# ---------------------------
# Theme-select helpers
# ---------------------------

def _config_dir() -> Path:
    """Per-user config dir (XDG-ish)."""
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return root / "ytfrags"


def _theme_config_path() -> Path:
    return _config_dir() / "settings.json"


def _load_saved_theme() -> Optional[str]:
    p = _theme_config_path()
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        theme = data.get("theme")
        return str(theme) if theme else None
    except Exception:
        return None


def _save_theme(theme: str) -> None:
    try:
        cfg = _config_dir()
        cfg.mkdir(parents=True, exist_ok=True)
        p = _theme_config_path()
        p.write_text(json.dumps({"theme": theme}, indent=2), encoding="utf-8")
    except Exception:
        pass

# ---------------------------
# Auth-on-fail helpers
# ---------------------------

_AUTH_FAIL_RE = re.compile(
    r"(sign in|login|confirm you('|’)re not a bot|captcha|429|forbidden|http error 403|cookies|age-restricted|members[- ]only)",
    re.IGNORECASE,
)


def _looks_like_auth_problem(text: str) -> bool:
    if not text:
        return False
    return bool(_AUTH_FAIL_RE.search(text))


def _insert_after_bin(cmd: List[str], extra: List[str]) -> List[str]:
    """Return cmd with extra args inserted right after argv[0]."""
    if not cmd:
        return cmd
    return [cmd[0], *extra, *cmd[1:]]


def _safe_stat_mtime(p: Path) -> float:
    try:
        return float(p.stat().st_mtime)
    except Exception:
        return 0.0


def _copy_sqlite_for_read(db_path: Path) -> Optional[Path]:
    """Copy sqlite DB to a temp file to avoid locks; return temp path."""
    try:
        if not db_path.exists():
            return None
        tmp = Path(tempfile.mkstemp(prefix="ytfrags_db_", suffix=".sqlite")[1])
        shutil.copy2(db_path, tmp)
        return tmp
    except Exception:
        return None


def _sqlite_table_has_any_row(db_path: Path, query: str, params: Tuple = ()) -> bool:
    tmp = _copy_sqlite_for_read(db_path)
    if tmp is None:
        return False
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            row = cur.fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _firefox_profiles_dir() -> Path:
    return Path.home() / ".mozilla" / "firefox"


def _zen_profiles_root() -> Path:
    return Path.home() / ".zen"


def _default_zen_profile_dir() -> Optional[Path]:
    """
    Default (without --zen-profile-path):
      Prefer ~/.zen/oplhmacu.Default (release) if present (your out-of-box profile),
      else pick the newest *.Default (release),
      else pick the newest *Default*.
    """
    root = _zen_profiles_root()
    if not root.exists():
        return None

    preferred = root / "oplhmacu.Default (release)"
    if preferred.exists():
        return preferred

    candidates = sorted(root.glob("*.Default (release)"), key=_safe_stat_mtime, reverse=True)
    if candidates:
        return candidates[0]

    candidates2 = sorted(root.glob("*Default*"), key=_safe_stat_mtime, reverse=True)
    if candidates2:
        return candidates2[0]

    return None


def _resolve_zen_cookie_db(zen_profile_path: Optional[str]) -> Optional[Path]:
    """
    Accepts:
      - None: use default zen profile dir resolution
      - path to dir: use dir/cookies.sqlite
      - path to file: use that file (if it's cookies.sqlite)
    """
    if zen_profile_path:
        p = Path(zen_profile_path).expanduser()
        if p.is_dir():
            db = p / "cookies.sqlite"
            return db if db.exists() else None
        if p.is_file():
            return p if p.exists() else None
        return None

    d = _default_zen_profile_dir()
    if not d:
        return None
    db = d / "cookies.sqlite"
    return db if db.exists() else None


def _list_firefox_cookie_dbs() -> List[Path]:
    root = _firefox_profiles_dir()
    if not root.exists():
        return []
    return list(root.glob("*/cookies.sqlite"))


def _firefox_db_has_youtubeish_cookies(db: Path) -> bool:
    # Firefox cookies table: moz_cookies(host, ...)
    q = "SELECT 1 FROM moz_cookies WHERE host LIKE ? OR host LIKE ? OR host LIKE ? LIMIT 1"
    return _sqlite_table_has_any_row(db, q, ("%youtube.%", "%google.%", "%accounts.google.%"))


def _chromium_cookie_db_for(browser: str) -> Optional[Path]:
    h = Path.home()
    mapping = {
        "chromium": h / ".config" / "chromium" / "Default" / "Cookies",
        "chrome": h / ".config" / "google-chrome" / "Default" / "Cookies",
        "brave": h / ".config" / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies",
        "edge": h / ".config" / "microsoft-edge" / "Default" / "Cookies",
        "vivaldi": h / ".config" / "vivaldi" / "Default" / "Cookies",
        "opera": h / ".config" / "opera" / "Default" / "Cookies",
        "whale": h / ".config" / "naver-whale" / "Default" / "Cookies",
        # Safari on Linux: basically not a thing; keep None.
        "safari": None,
        "firefox": None,
    }
    p = mapping.get(browser)
    if p is None:
        return None
    return p if p.exists() else None


def _chromium_db_has_youtubeish_cookies(db: Path) -> bool:
    # Chromium cookies table: cookies(host_key, ...)
    q = "SELECT 1 FROM cookies WHERE host_key LIKE ? OR host_key LIKE ? OR host_key LIKE ? LIMIT 1"
    return _sqlite_table_has_any_row(db, q, ("%youtube.%", "%google.%", "%accounts.google.%"))


@dataclass
class CookieCandidate:
    kind: str  # "zen" | "browser"
    label: str  # user-friendly
    browser: Optional[str] = None  # for kind="browser"
    zen_db: Optional[Path] = None  # for kind="zen"
    score: int = 0
    reason: str = ""


@dataclass
class AuthMethod:
    label: str
    args: List[str]  # args to inject after yt-dlp binary
    temp_files: List[Path]


def _rank_cookie_candidates(zen_profile_path: Optional[str]) -> List[CookieCandidate]:
    out: List[CookieCandidate] = []

    # Zen first (forced)
    zen_db = _resolve_zen_cookie_db(zen_profile_path)
    zen_score = 0
    zen_reason = "Zen preferred"
    if zen_db and zen_db.exists():
        zen_score += 100
        if _firefox_db_has_youtubeish_cookies(zen_db):
            zen_score += 50
        zen_score += int(min(30, max(0, (datetime.now().timestamp() - _safe_stat_mtime(zen_db)) // -86400)))  # tiny bonus
        zen_reason = f"Zen cookies.sqlite found ({'has' if _firefox_db_has_youtubeish_cookies(zen_db) else 'no'} YT/Google cookies hit)"
    else:
        zen_reason = "Zen cookies.sqlite not found (will skip if unavailable)"

    out.append(
        CookieCandidate(kind="zen", label="Zen", zen_db=zen_db, score=10_000 + zen_score, reason=zen_reason)
    )

    # Firefox
    ff_dbs = _list_firefox_cookie_dbs()
    ff_best = None
    ff_score = 0
    ff_reason = "Firefox profile cookies.sqlite not found"
    if ff_dbs:
        # prefer default-release-ish, else newest mtime
        preferred = [p for p in ff_dbs if "default-release" in p.parent.name]
        pool = preferred if preferred else ff_dbs
        ff_best = sorted(pool, key=_safe_stat_mtime, reverse=True)[0]
        ff_score += 100
        if _firefox_db_has_youtubeish_cookies(ff_best):
            ff_score += 50
        ff_reason = f"Firefox cookies.sqlite found ({'has' if _firefox_db_has_youtubeish_cookies(ff_best) else 'no'} YT/Google cookies hit)"
    out.append(
        CookieCandidate(kind="browser", label="Firefox", browser="firefox", score=5_000 + ff_score, reason=ff_reason)
    )

    # Chromium-family & others (supported by yt-dlp)
    popularity_bonus = {
        "chromium": 60,
        "chrome": 55,
        "brave": 50,
        "edge": 40,
        "vivaldi": 25,
        "opera": 20,
        "whale": 10,
        "safari": 1,
    }

    for b in [x for x in COOKIE_BROWSER_ORDER if x != "firefox"]:
        base = 2_000 + popularity_bonus.get(b, 0)
        db = _chromium_cookie_db_for(b)
        score = base
        reason = "Cookie DB not found"
        if db and db.exists():
            score += 80
            if _chromium_db_has_youtubeish_cookies(db):
                score += 40
            reason = f"Cookies DB found ({'has' if _chromium_db_has_youtubeish_cookies(db) else 'no'} YT/Google cookies hit)"
        out.append(CookieCandidate(kind="browser", label=b.capitalize(), browser=b, score=score, reason=reason))

    # sort descending (Zen stays first anyway due to huge base)
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def _export_firefox_sqlite_to_netscape(db_path: Path, out_path: Path) -> bool:
    """
    Export a subset of Firefox-style cookies.sqlite (Zen/Firefox) into Netscape cookies format.
    We intentionally export only youtube/google domains to reduce leakage.
    """
    tmp = _copy_sqlite_for_read(db_path)
    if tmp is None:
        return False
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            cur = conn.cursor()

            # Validate table exists
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='moz_cookies'")
            if cur.fetchone() is None:
                return False

            # Export only youtube/google-ish cookies
            cur.execute(
                """
                SELECT host, path, isSecure, expiry, name, value
                FROM moz_cookies
                WHERE host LIKE ? OR host LIKE ? OR host LIKE ? OR host LIKE ? OR host LIKE ?
                """,
                ("%youtube.%", "%google.%", "%accounts.google.%", "%googlevideo.%", "%ytimg.%"),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            # Still write a valid file (some auth flows might rely on other domains; but no rows means no help)
            out_path.write_text("# Netscape HTTP Cookie File\n# (empty)\n", encoding="utf-8")
            return True

        with out_path.open("w", encoding="utf-8", errors="replace") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# This file was generated by ytfrags_tui.py\n\n")
            for host, path, is_secure, expiry, name, value in rows:
                if host is None or name is None:
                    continue
                host_s = str(host).strip()
                path_s = str(path or "/").strip() or "/"
                secure_s = "TRUE" if int(is_secure or 0) else "FALSE"
                try:
                    expiry_i = int(expiry or 0)
                except Exception:
                    expiry_i = 0
                name_s = str(name)
                val_s = "" if value is None else str(value)

                # Netscape format is tab-separated; sanitize tabs/newlines.
                host_s = host_s.replace("\t", " ").replace("\n", " ").replace("\r", " ")
                path_s = path_s.replace("\t", " ").replace("\n", " ").replace("\r", " ")
                name_s = name_s.replace("\t", " ").replace("\n", " ").replace("\r", " ")
                val_s = val_s.replace("\t", " ").replace("\n", " ").replace("\r", " ")

                # include_subdomains flag expects leading dot convention
                include_subdomains = "TRUE" if host_s.startswith(".") else "FALSE"
                f.write(f"{host_s}\t{include_subdomains}\t{path_s}\t{secure_s}\t{expiry_i}\t{name_s}\t{val_s}\n")

        try:
            os.chmod(out_path, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


_RANGE_RE = re.compile(
    r"""
    ^\s*\*?\s*
    (?P<start>
        -?
        (?:
            \d+:\d{2}:\d{2}(?:\.\d+)?   # HH:MM:SS(.ms)
            |
            \d+:\d{2}(?:\.\d+)?         # MM:SS(.ms)
            |
            \d+(?:\.\d+)?               # SS(.ms)
        )
    )
    \s*-\s*
    (?P<end>
        (?:
            inf
            |
            -?
            (?:
                \d+:\d{2}:\d{2}(?:\.\d+)?
                |
                \d+:\d{2}(?:\.\d+)?
                |
                \d+(?:\.\d+)?
            )
        )
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_range(range_text: str) -> Tuple[bool, str, str]:
    """
    Validate and normalize a time-range string.

    Accepts:
      - "MM:SS-MM:SS"
      - "HH:MM:SS-HH:MM:SS"
      - "SS-SS"
      - end may be "inf"
      - optional leading "*" is allowed but not required.

    Returns: (ok, normalized_for_ytdlp, error_message)
    """
    text = range_text.strip()
    if not text:
        return False, "", "Empty time range"

    m = _RANGE_RE.match(text)
    if not m:
        return False, "", "Bad format. Use START-END, e.g. 00:01:23-00:02:34 or 10:15-inf"

    start = m.group("start")
    end = m.group("end").lower()

    if start == end:
        return False, "", "Start and end are identical"

    return True, f"*{start}-{end}", ""


def is_zero_time(s: str) -> bool:
    """Accepts 0, 0.0, 00:00, 00:00:00, with optional .ms. Treats -0 as zero too."""
    t = s.strip()
    if not t:
        return False
    try:
        parts = t.split(":")
        if len(parts) == 1:
            val = float(parts[0])
        elif len(parts) == 2:
            val = float(parts[0]) * 60.0 + float(parts[1])
        elif len(parts) == 3:
            val = float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
        else:
            return False
    except Exception:
        return False
    return abs(val) < 1e-9


def _format_selector_1080_best() -> str:
    # Best video <=1080p + best audio; fallback to best <=1080p then best overall.
    return "bv*[height<=1080]+ba/best[height<=1080]/best"


def _format_selector_mp4_h264_1080() -> str:
    # Try hard to get AVC (H.264) + AAC within 1080p, inside MP4 container.
    # Fallback stays inside MP4 to avoid remux failures.
    return (
        "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/"
        "best[height<=1080][ext=mp4]/"
        "best[ext=mp4]"
    )


def _format_selector_compact_720() -> str:
    """
    Compact selector: cap to <=720p, prefer <=30fps, prefer efficient video codecs (AV1/VP9),
    and prefer Opus audio with a not-huge bitrate when possible.

    This is still download-only: it only chooses *which native formats* yt-dlp grabs.
    """
    h = COMPACT_MAX_HEIGHT
    fps = COMPACT_MAX_FPS
    abr = COMPACT_MAX_AUDIO_ABR
    return (
        # Prefer AV1/VP9 + Opus, <=30fps if possible
        f"bv*[height<={h}][fps<={fps}][vcodec^=av01]+ba[acodec^=opus][abr<={abr}]/"
        f"bv*[height<={h}][fps<={fps}][vcodec^=vp09]+ba[acodec^=opus][abr<={abr}]/"
        # If abr metadata doesn't match, keep codec preference
        f"bv*[height<={h}][fps<={fps}][vcodec^=av01]+ba[acodec^=opus]/"
        f"bv*[height<={h}][fps<={fps}][vcodec^=vp09]+ba[acodec^=opus]/"
        # If fps constraint is too strict, relax fps
        f"bv*[height<={h}][vcodec^=av01]+ba[acodec^=opus][abr<={abr}]/"
        f"bv*[height<={h}][vcodec^=vp09]+ba[acodec^=opus][abr<={abr}]/"
        f"bv*[height<={h}][vcodec^=av01]+ba[acodec^=opus]/"
        f"bv*[height<={h}][vcodec^=vp09]+ba[acodec^=opus]/"
        # Last-resort compact-ish
        f"bv*[height<={h}]+ba[acodec^=opus]/"
        f"bv*[height<={h}]+ba/"
        f"best[height<={h}]/best"
    )


def _format_selector_mp4_h264_720() -> str:
    # MP4-friendly compact selector (H.264 + AAC) capped at <=720p.
    return (
        "bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/"
        "best[height<=720][ext=mp4]/"
        "best[ext=mp4]"
    )


def _format_selector_best_audio() -> str:
    """
    Best-audio selector that tries hard to avoid "audio in a video container" outcomes.

    Goal:
      - Prefer *audio-ish* containers when available (m4a/mp3/ogg/opus/flac/wav),
        so your audio mode doesn't commonly produce a .webm that players show as
        “a black video”.

    Notes:
      - This does NOT re-encode anything. It only changes which *native* audio format we pick.
      - On YouTube, this will usually pick AAC in .m4a (very editor-friendly),
        instead of Opus-in-WebM.
      - If a site only offers WebM audio, yt-dlp may still fall back to it.
        (If you want “never webm, remux to .ogg/.opus” we can add a ffmpeg remux step later.)
    """
    return (
        # Strong preference: AAC in an audio container
        "ba[ext=m4a]/"
        "ba[ext=mp4][acodec^=mp4a]/"
        "ba[acodec^=mp4a]/"
        # Other clean audio containers
        "ba[ext=mp3]/"
        "ba[ext=flac]/"
        "ba[ext=wav]/"
        "ba[ext=ogg]/"
        "ba[ext=opus]/"
        # Last resort
        "ba/bestaudio"
    )


def build_ytdlp_command(
    *,
    url: str,
    ranges: List[str],
    download_root: Path,
    downloader_bin: str,
    profile: str,  # "mkv" | "mp4_direct" | "mkv_intermediate" | "audio"
    whole_video: bool = False,
    preset: str = "quality",  # "quality" | "compact" (video only)
) -> List[str]:
    """
    Build a yt-dlp command.

    If whole_video=True:
      - no --download-sections are used
      - output template does NOT reference section_* fields

    Otherwise:
      - downloads multiple ranges from the same URL with --download-sections
    """
    is_audio = profile == "audio"

    if whole_video:
        if is_audio:
            out_tmpl = "%(title)s [%(id)s]/%(title)s [%(id)s] - audio.%(ext)s"
        else:
            out_tmpl = "%(title)s [%(id)s]/%(title)s [%(id)s].%(ext)s"
    else:
        if is_audio:
            out_tmpl = (
                "%(title)s [%(id)s]/"
                "%(title)s [%(id)s] - audio frag %(section_number)02d "
                "(%(section_start>%H-%M-%S)s-%(section_end>%H-%M-%S)s).%(ext)s"
            )
        else:
            out_tmpl = (
                "%(title)s [%(id)s]/"
                "%(title)s [%(id)s] - frag %(section_number)02d "
                "(%(section_start>%H-%M-%S)s-%(section_end>%H-%M-%S)s).%(ext)s"
            )

    cmd: List[str] = [
        downloader_bin,
        "--newline",
        "--no-playlist",
        "-P",
        str(download_root),
        "-o",
        out_tmpl,
    ]

    # Compact preset: keep the download progress reasonably fast.
    # (Video only; audio mode unchanged.)
    if (not is_audio) and preset == "compact":
        cmd.extend(["-N", COMPACT_CONCURRENT_FRAGMENTS])

    if profile in ("mkv", "mkv_intermediate"):
        cmd.extend(["-t", "mkv", "-f", _format_selector_compact_720() if preset == "compact" else _format_selector_1080_best()])
    elif profile == "mp4_direct":
        cmd.extend(["-t", "mp4", "-f", _format_selector_mp4_h264_720() if preset == "compact" else _format_selector_mp4_h264_1080()])
    elif profile == "audio":
        cmd.extend(["-f", _format_selector_best_audio()])
    else:
        cmd.extend(["-t", "mkv", "-f", _format_selector_1080_best()])

    if not whole_video:
        for r in ranges:
            cmd.extend(["--download-sections", r])

    cmd.append(url)
    return cmd


@dataclass
class AppState:
    url: str = ""
    count: int = 1
    starts: List[str] = None
    ends: List[str] = None
    ranges_norm: List[str] = None
    whole_video: bool = False  # Start=0 and End=0 shortcut (only when count=1)

    # v1.1 output toggles
    media_mode: str = "video"  # "video" | "audio"
    preset_mode: str = "quality"  # "quality" | "compact" (video only)
    container_mode: str = "mkv"  # "mkv" | "mp4" (video only)

    def __post_init__(self) -> None:
        if self.starts is None:
            self.starts = []
        if self.ends is None:
            self.ends = []
        if self.ranges_norm is None:
            self.ranges_norm = []


class CountSelector(Widget):
    """Working fragment count selector: ←/→ adjusts, Enter confirms."""

    can_focus = True
    value: reactive[int] = reactive(1)
    confirmed: reactive[bool] = reactive(False)

    class Confirmed(Message):
        bubble = True

        def __init__(self, value: int) -> None:
            super().__init__()
            self.value = value

    def __init__(
        self,
        *,
        value: int = 1,
        min_value: int = 1,
        max_value: int = MAX_FRAGMENTS,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.min_value = min_value
        self.max_value = max_value
        self.value = int(value)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="count_row"):
            yield Static("Fragments:", classes="count_label")
            yield Static(str(self.value), id="count_value", classes="count_value")
            yield Static("  (←/→ change, Enter confirm)", classes="count_hint")

    def _clamp(self, v: int) -> int:
        return max(self.min_value, min(self.max_value, v))

    def adjust(self, delta: int) -> None:
        if self.confirmed:
            return
        self.value = self._clamp(self.value + delta)

    def confirm(self) -> None:
        if self.confirmed:
            return
        self.confirmed = True
        self.post_message(self.Confirmed(self.value))

    def watch_value(self, value: int) -> None:
        if self.is_mounted:
            self.query_one("#count_value", Static).update(str(value))

    def watch_confirmed(self, confirmed: bool) -> None:
        if confirmed:
            self.add_class("confirmed")

    def on_key(self, event: Key) -> None:
        if self.confirmed:
            return

        if event.key == "left":
            self.adjust(-1)
            event.stop()
        elif event.key == "right":
            self.adjust(+1)
            event.stop()
        elif event.key == "enter":
            self.confirm()
            event.stop()


class TwoChoiceSelector(Widget):
    """
    Two-option toggle selector: ←/→ changes, Enter confirms (non-locking).

    IMPORTANT: We render literal brackets like "[Video] / Audio".
    Static defaults to Rich markup, which would eat "[Video]" as a style tag.
    Therefore we set markup=False on the value Static.
    """

    can_focus = True
    value: reactive[str] = reactive("")

    class Changed(Message):
        bubble = True

        def __init__(self, key: str, value: str) -> None:
            super().__init__()
            self.key = key
            self.value = value

    class Confirmed(Message):
        bubble = True

        def __init__(self, key: str, value: str) -> None:
            super().__init__()
            self.key = key
            self.value = value

    def __init__(
        self,
        *,
        key: str,
        label: str,
        options: List[Tuple[str, str]],  # [(value, label)]
        value: str,
        hint: str = "(←/→ change, Enter confirm)",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.key = key
        self.label = label
        self.options = options[:]
        self.hint = hint
        self.value = value if value else (self.options[0][0] if self.options else "")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toggle_row"):
            yield Static(self.label, classes="toggle_label")
            yield Static(self._render_value(), id="toggle_value", classes="toggle_value", markup=False)
            yield Static(self.hint, classes="toggle_hint")

    def _render_value(self) -> str:
        if not self.options:
            return ""

        NBSP = "\u00a0"

        max_len = max(len(lab) for _, lab in self.options)

        def render_opt(v: str, lab: str) -> str:
            lab_fixed = lab.rjust(max_len)
            if v == self.value:
                return f"[{lab_fixed}]"
            return f"{NBSP}{lab_fixed}{NBSP}"

        (v0, lab0), (v1, lab1) = self.options[0], self.options[1]

        left = render_opt(v0, lab0)
        right = render_opt(v1, lab1)

        # --- FIX: keep slash stationary (constant separator) ---
        # Produces:
        #   [Video] /  Audio   <->   Video  / [Audio]
        sep = f"{NBSP}/{NBSP}"

        return left + sep + right
        # --- END FIX ---

    def _set_value(self, v: str) -> None:
        if v == self.value:
            return
        self.value = v
        self.post_message(self.Changed(self.key, self.value))

    def _cycle(self, delta: int) -> None:
        if not self.options:
            return
        values = [v for v, _ in self.options]
        if self.value not in values:
            self._set_value(values[0])
            return
        idx = values.index(self.value)
        idx = (idx + delta) % len(values)
        self._set_value(values[idx])

    def watch_value(self, value: str) -> None:
        if self.is_mounted:
            self.query_one("#toggle_value", Static).update(self._render_value())

    def on_key(self, event: Key) -> None:
        if event.key == "left":
            self._cycle(-1)
            event.stop()
        elif event.key == "right":
            self._cycle(+1)
            event.stop()
        elif event.key == "enter":
            self.post_message(self.Confirmed(self.key, self.value))
            event.stop()


class EnterPrompt(Widget):
    """Focusable prompt that fires when Enter is pressed."""

    can_focus = True
    text: reactive[str] = reactive("Press Enter to continue")

    class Triggered(Message):
        bubble = True

    def __init__(self, text: str, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.text = text

    def compose(self) -> ComposeResult:
        yield Static(self.text, id="prompt_text", classes="enter_prompt")

    def watch_text(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#prompt_text", Static).update(text)

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Triggered())
            event.stop()


class RangeRow(Widget):
    """Two Inputs (Start/End) on the same line for a given fragment."""

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index

    def compose(self) -> ComposeResult:
        yield Label(f"Select range for Fragment {self.index}", classes="section_title")
        with Horizontal(classes="range_row"):
            yield Input(
                placeholder="Start  (e.g. 00:00:30 or -30)",
                id=f"range_start_{self.index}",
                classes="range_input",
            )
            yield Static("→", classes="range_arrow")
            yield Input(
                placeholder="End  (e.g. 00:01:10 or inf / -10)",
                id=f"range_end_{self.index}",
                classes="range_input",
            )
        yield Static(
            "Examples: 00:00:30 → 00:01:10   |   10:15 → inf   |   -30 → -10\n"
            "Tip: Start=0 and End=0 downloads the full video (when fragment count is 1).",
            classes="hint",
        )


class WizardScreen(Screen):
    """Single-screen wizard that grows downward and auto-scrolls to new content."""

    phase: reactive[str] = reactive("url")  # url -> count -> output -> ranges -> review
    current_fragment: reactive[int] = reactive(1)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="panel"):
            with VerticalScroll(id="scroll"):
                yield Static(
                    "Controls: Enter = confirm/next  |  ←/→ (Left/Right) = toggles  |  PgUp/PgDn = scroll  |  Tab = focus  |  Ctrl+Q = quit",
                    id="controls",
                )
                yield Static(colored_ascii_block(ASCII_ART_HEADER), id="ascii")

                yield Label("Video URL", classes="section_title")
                yield Input(
                    placeholder="Paste the YouTube URL here, then press Enter",
                    id="url_input",
                )

                yield Vertical(id="flow")

            yield Static("", id="status", classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#url_input", Input))
        self._set_status("Paste a URL and press Enter.")

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _scroll_end(self) -> None:
        """Instant jump to bottom (no animation), with fallbacks for older Textual."""
        scroll = self.query_one("#scroll", VerticalScroll)
        try:
            scroll.scroll_end(animate=False)  # type: ignore[arg-type]
            return
        except Exception:
            pass
        try:
            scroll.scroll_end()
        except Exception:
            pass

    def _scroll_by(self, delta_y: int) -> None:
        scroll = self.query_one("#scroll", VerticalScroll)
        try:
            scroll.scroll_relative(y=delta_y)  # type: ignore[call-arg]
        except Exception:
            try:
                scroll.scroll_relative(0, delta_y)  # type: ignore[misc]
            except Exception:
                pass

    def on_key(self, event: Key) -> None:
        if event.key == "pageup":
            amount = max(3, self.query_one("#scroll", VerticalScroll).size.height - 3)
            self._scroll_by(-amount)
            event.stop()
            return
        if event.key == "pagedown":
            amount = max(3, self.query_one("#scroll", VerticalScroll).size.height - 3)
            self._scroll_by(+amount)
            event.stop()
            return

        if self.phase == "count":
            try:
                selector = self.query_one("#count_selector", CountSelector)
            except Exception:
                return

            if event.key == "left":
                selector.adjust(-1)
                event.stop()
                return
            if event.key == "right":
                selector.adjust(+1)
                event.stop()
                return
            if event.key == "enter":
                selector.confirm()
                event.stop()
                return

    async def _flow_mount(self, *widgets: Widget) -> None:
        flow = self.query_one("#flow", Vertical)
        await flow.mount(*widgets)
        self.call_after_refresh(self._scroll_end)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        wid = event.input.id or ""

        if wid == "url_input" and self.phase == "url":
            url = event.value.strip()
            if not url:
                event.input.add_class("invalid")
                self._set_status("URL can't be empty. Paste one and press Enter.")
                return

            event.input.remove_class("invalid")
            event.input.disabled = True
            self.app.state.url = url

            self._set_status("URL confirmed. Choose fragment count with ←/→, then press Enter.")
            await self._show_count_step()
            return

        if self.phase == "ranges" and wid.startswith("range_start_"):
            try:
                idx = int(wid.split("_")[-1])
            except Exception:
                return
            if idx != self.current_fragment:
                return
            self.set_focus(self.query_one(f"#range_end_{idx}", Input))
            self._set_status(f"Fragment {idx}: now fill End and press Enter to confirm.")
            return

        if self.phase == "ranges" and wid.startswith("range_end_"):
            try:
                idx = int(wid.split("_")[-1])
            except Exception:
                return
            if idx != self.current_fragment:
                return

            start_inp = self.query_one(f"#range_start_{idx}", Input)
            end_inp = self.query_one(f"#range_end_{idx}", Input)

            start = start_inp.value.strip()
            end = end_inp.value.strip()

            if not start:
                start_inp.add_class("invalid")
                self._set_status(f"Fragment {idx}: Start can't be empty.")
                self.set_focus(start_inp)
                return
            if not end:
                end_inp.add_class("invalid")
                self._set_status(f"Fragment {idx}: End can't be empty (use 'inf' for end-of-video).")
                self.set_focus(end_inp)
                return

            if is_zero_time(start) and is_zero_time(end):
                if self.app.state.count == 1 and idx == 1:
                    start_inp.remove_class("invalid")
                    end_inp.remove_class("invalid")
                    start_inp.disabled = True
                    end_inp.disabled = True

                    self.app.state.whole_video = True
                    self.app.state.starts.append(start)
                    self.app.state.ends.append(end)
                    self.app.state.ranges_norm = []

                    await self._show_review()
                    return
                else:
                    self._set_status(
                        "Tip: Start=0 and End=0 downloads the FULL video, but only when fragment count is 1."
                    )
                    self.set_focus(start_inp)
                    return

            raw = f"{start}-{end}"
            ok, norm, err = normalize_range(raw)
            if not ok:
                start_inp.add_class("invalid")
                end_inp.add_class("invalid")
                self._set_status(f"Fragment {idx}: {err}")
                return

            start_inp.remove_class("invalid")
            end_inp.remove_class("invalid")

            start_inp.disabled = True
            end_inp.disabled = True

            self.app.state.starts.append(start)
            self.app.state.ends.append(end)
            self.app.state.ranges_norm.append(norm)

            if self.current_fragment < self.app.state.count:
                self.current_fragment += 1
                await self._show_range_input(self.current_fragment)
            else:
                await self._show_review()
            return

    async def _show_count_step(self) -> None:
        if self.phase != "url":
            return

        self.phase = "count"
        await self._flow_mount(
            Label("How many fragments?", classes="section_title"),
            Static("Use ←/→ to change the number (min 1), then press Enter to confirm.", classes="hint"),
            CountSelector(
                value=max(1, self.app.state.count),
                min_value=1,
                max_value=MAX_FRAGMENTS,
                id="count_selector",
            ),
        )
        self.call_after_refresh(lambda: self.set_focus(self.query_one("#count_selector", CountSelector)))

    @on(CountSelector.Confirmed)
    async def _on_count_confirmed(self, message: CountSelector.Confirmed) -> None:
        if self.phase != "count":
            return

        count = int(message.value)
        self.app.state.count = max(1, min(MAX_FRAGMENTS, count))

        self.app.state.starts = []
        self.app.state.ends = []
        self.app.state.ranges_norm = []
        self.app.state.whole_video = False
        self.current_fragment = 1

        self.app.state.media_mode = self.app.default_media_mode
        self.app.state.preset_mode = self.app.default_preset_mode
        self.app.state.container_mode = self.app.default_container_mode

        self.phase = "output"
        await self._show_output_step()

    async def _show_output_step(self) -> None:
        note_lines: List[Widget] = []
        if self.app.startup_note:
            note_lines.append(Static(f"Note: {self.app.startup_note}", classes="hint"))

        await self._flow_mount(
            Label("Output settings", classes="section_title"),
            Static("Pick what to download. Audio ignores Preset and MKV/MP4 (not applicable).", classes="hint"),
            *note_lines,
            TwoChoiceSelector(
                key="media",
                label="Media:",
                options=[("video", "Video"), ("audio", "Audio")],
                value=self.app.state.media_mode,
                id="media_selector",
            ),
            TwoChoiceSelector(
                key="preset",
                label="Preset:",
                options=[("quality", "Quality"), ("compact", "Compact")],
                value=self.app.state.preset_mode,
                hint="(video only)  (←/→ change, Enter confirm)",
                id="preset_selector",
            ),
            TwoChoiceSelector(
                key="container",
                label="Container:",
                options=[("mkv", "MKV"), ("mp4", "MP4")],
                value=self.app.state.container_mode,
                hint="(video only)  (←/→ change, Enter confirm)",
                id="container_selector",
            ),
        )

        self._update_output_visibility()
        self.call_after_refresh(lambda: self.set_focus(self.query_one("#media_selector", TwoChoiceSelector)))

        if self.app.state.media_mode == "audio":
            self._set_status("Output: AUDIO ONLY. Press Enter to continue to time ranges.")
        else:
            self._set_status("Output: VIDEO. Press Enter on Media to continue to preset selection.")

    def _update_output_visibility(self) -> None:
        try:
            preset = self.query_one("#preset_selector", TwoChoiceSelector)
            cont = self.query_one("#container_selector", TwoChoiceSelector)
        except Exception:
            return

        want_visible = self.app.state.media_mode == "video"

        was_preset_visible = bool(getattr(preset, "display", True))
        was_cont_visible = bool(getattr(cont, "display", True))

        preset.display = want_visible
        cont.display = want_visible

        if want_visible and (not was_preset_visible or not was_cont_visible):
            self.call_after_refresh(self._scroll_end)

    @on(TwoChoiceSelector.Changed)
    def _on_toggle_changed(self, message: TwoChoiceSelector.Changed) -> None:
        if self.phase != "output":
            return

        if message.key == "media":
            self.app.state.media_mode = message.value
            self._update_output_visibility()

            if message.value == "audio":
                self.call_after_refresh(lambda: self.set_focus(self.query_one("#media_selector", TwoChoiceSelector)))
                if self.app.state.container_mode == "mp4":
                    self._set_status("Audio-only selected → Preset + MKV/MP4 ignored (even if you started with --mp4). Press Enter.")
                else:
                    self._set_status("Audio-only selected → Preset + MKV/MP4 ignored. Press Enter to continue.")
            else:
                self._set_status("Video selected. Press Enter on Media to choose preset.")
            return

        if message.key == "preset":
            self.app.state.preset_mode = message.value
            # Optional nudge (doesn't change behavior): Compact is best paired with MKV.
            if self.app.state.preset_mode == "compact" and self.app.state.container_mode == "mp4":
                self._set_status("Preset: COMPACT. Note: biggest size wins are usually with MKV (MP4 may be less efficient).")
            return

        if message.key == "container":
            self.app.state.container_mode = message.value
            if self.app.state.preset_mode == "compact" and self.app.state.container_mode == "mp4":
                self._set_status("Container: MP4. Note: Compact preset usually shines more with MKV.")
            return

    @on(TwoChoiceSelector.Confirmed)
    async def _on_toggle_confirmed(self, message: TwoChoiceSelector.Confirmed) -> None:
        if self.phase != "output":
            return

        if message.key == "media":
            self.app.state.media_mode = message.value
            self._update_output_visibility()

            if self.app.state.media_mode == "audio":
                await self._begin_ranges_phase()
                return

            self._set_status("Select preset (Quality/Compact) with ←/→, then press Enter.")
            self.call_after_refresh(lambda: self.set_focus(self.query_one("#preset_selector", TwoChoiceSelector)))
            return

        if message.key == "preset":
            self.app.state.preset_mode = message.value
            self._set_status("Select container (MKV/MP4) with ←/→, then press Enter.")
            self.call_after_refresh(lambda: self.set_focus(self.query_one("#container_selector", TwoChoiceSelector)))
            return

        if message.key == "container":
            self.app.state.container_mode = message.value
            await self._begin_ranges_phase()
            return

    async def _begin_ranges_phase(self) -> None:
        self.phase = "ranges"
        self._set_status("Fill Start + End for Fragment 1 (press Enter on End).")

        await self._flow_mount(
            Label("Time syntax", classes="section_title"),
            Static(
                "Accepted: SS, MM:SS, HH:MM:SS (optional .ms). End may be 'inf'.\n"
                "Negative values are relative to end of video.\n"
                "Examples: 00:00:30 → 00:01:10 | 10:15 → inf | -30 → -10\n"
                "Tip: Start=0 and End=0 downloads the full video (when fragment count is 1).",
                classes="hint_block",
            ),
        )

        await self._show_range_input(1)

    async def _show_range_input(self, index: int) -> None:
        await self._flow_mount(RangeRow(index))
        self._set_status(f"Fragment {index}: fill Start, Enter; then End, Enter to confirm.")
        self.call_after_refresh(lambda: self.set_focus(self.query_one(f"#range_start_{index}", Input)))

    async def _show_review(self) -> None:
        self.phase = "review"

        url = self.app.state.url
        root = self.app.download_root
        ranges_norm = self.app.state.ranges_norm
        whole_video = self.app.state.whole_video

        media = self.app.state.media_mode
        preset = self.app.state.preset_mode
        container = self.app.state.container_mode

        downloader_bin = _find_ytdlp_binary() or "yt-dlp"

        if media == "audio":
            cmd_preview = build_ytdlp_command(
                url=url,
                ranges=ranges_norm,
                download_root=root,
                downloader_bin=downloader_bin,
                profile="audio",
                whole_video=whole_video,
                preset="quality",
            )
            output_note = "Output: AUDIO ONLY (best audio available). Preset and MKV/MP4 not applicable."
        else:
            if container == "mkv":
                cmd_preview = build_ytdlp_command(
                    url=url,
                    ranges=ranges_norm,
                    download_root=root,
                    downloader_bin=downloader_bin,
                    profile="mkv",
                    whole_video=whole_video,
                    preset=preset,
                )
                if preset == "compact":
                    output_note = f"Output: VIDEO MKV (COMPACT ≤{COMPACT_MAX_HEIGHT}p, prefers efficient codecs; uses -N {COMPACT_CONCURRENT_FRAGMENTS})"
                else:
                    output_note = "Output: VIDEO MKV (best ≤1080p video + best audio)"
            else:
                cmd_preview = build_ytdlp_command(
                    url=url,
                    ranges=ranges_norm,
                    download_root=root,
                    downloader_bin=downloader_bin,
                    profile="mp4_direct",
                    whole_video=whole_video,
                    preset=preset,
                )
                if preset == "compact":
                    output_note = "Output: VIDEO MP4 (COMPACT ≤720p, H.264 + AAC). If unavailable, auto-fallback to MKV + transcode."
                else:
                    output_note = "Output: VIDEO MP4 (H.264 + AAC). If unavailable, auto-fallback to MKV + transcode."

        if whole_video:
            summary = "Fragment 1: FULL VIDEO (no trimming)"
            frag_line = "Fragments: full video"
        else:
            lines = []
            for i, (s, e) in enumerate(zip(self.app.state.starts, self.app.state.ends), start=1):
                lines.append(f"Fragment {i}: {s} → {e}")
            summary = "\n".join(lines)
            frag_line = f"Fragments: {len(ranges_norm)}"

        cmd_line = shlex.join(cmd_preview)

        await self._flow_mount(
            Label("Review", classes="section_title"),
            Static(output_note, classes="review_line"),
            Static(f"URL: {url}", classes="review_line"),
            Static(frag_line, classes="review_line"),
            Static(summary, classes="review_block"),
            Static(f"Download folder: {root}", classes="review_line"),
            Static("Command (first attempt):", classes="review_line"),
            Static(f"    {cmd_line}", classes="review_cmd"),
            EnterPrompt("Press Enter to start downloading (Ctrl+Q to quit).", id="start_prompt"),
        )

        self.call_after_refresh(lambda: self.set_focus(self.query_one("#start_prompt", EnterPrompt)))
        self._set_status("Ready. Press Enter to start download.")

    @on(EnterPrompt.Triggered)
    async def _on_start_triggered(self, message: EnterPrompt.Triggered) -> None:
        if self.phase != "review":
            return

        if not _find_ytdlp_binary():
            self._set_status("Error: yt-dlp not found in PATH.")
            return

        if not shutil.which("ffmpeg"):
            self._set_status("Error: ffmpeg not found in PATH (required).")
            return

        self.app.push_screen(
            RunScreen(
                url=self.app.state.url,
                ranges_norm=self.app.state.ranges_norm,
                download_root=self.app.download_root,
                total_fragments=self.app.state.count,
                media_mode=self.app.state.media_mode,
                preset_mode=self.app.state.preset_mode,
                container_mode=self.app.state.container_mode,
                whole_video=self.app.state.whole_video,
                zen_profile_path=self.app.zen_profile_path,
            )
        )


def _parse_time_to_seconds_generic(t: str) -> Optional[float]:
    t = t.strip()
    if not t:
        return None
    try:
        parts = t.split(":")
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return float(parts[0]) * 60.0 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
    except Exception:
        return None
    return None


def _parse_time_to_seconds_for_ranges(t: str) -> Optional[float]:
    t = t.strip()
    if not t or t.lower() == "inf" or t.startswith("-"):
        return None
    return _parse_time_to_seconds_generic(t)


def _format_hhmmss(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--:--"
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    return f"{h:02d}:{m:02d}:{ss:02d}"


def _format_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or seconds > 999 * 3600:
        return "--:--"
    s = int(seconds)
    m = s // 60
    ss = s % 60
    return f"{m:02d}:{ss:02d}"


def _is_url_spam(line: str) -> bool:
    if "googlevideo.com/videoplayback" in line:
        return True
    if "http" in line and len(line) > 240:
        return True
    return False


def _should_keep_event(line: str) -> bool:
    u = line.upper()
    if "ERROR" in u or "WARNING" in u:
        return True
    if "Extracting URL" in line:
        return True
    if "Downloading" in line and "time ranges" in line:
        return True
    if "Merging formats" in line or "Deleting original" in line:
        return True
    if "Destination:" in line:
        return True
    if line.startswith("[youtube]") or line.startswith("[info]") or line.startswith("[download]"):
        if line.startswith("[download]") and "%" in line:
            return False
        return True
    if line.startswith("Output #0") or line.startswith("Stream mapping"):
        return True
    return False


_FFMPEG_RE = re.compile(
    r"time=(?P<time>\S+).*?bitrate=\s*(?P<bitrate>\S+).*?speed=\s*(?P<speed>\S+)(?:.*?elapsed=(?P<elapsed>\S+))?",
    re.IGNORECASE,
)

_YTDLP_PCT_RE = re.compile(
    r"^\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%\s+of\s+(?P<total>~?\s*\S+)",
    re.IGNORECASE,
)


@dataclass
class LiveProgress:
    fragment_idx: int = 1
    percent: Optional[float] = None
    cur_time_s: Optional[float] = None
    seg_len_s: Optional[float] = None
    size: str = ""
    speed_x: str = ""
    bitrate: str = ""
    eta_s: Optional[float] = None


def _segment_lengths_from_ranges(ranges_norm: List[str]) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for r in ranges_norm:
        m = _RANGE_RE.match(r.strip())
        if not m:
            out.append(None)
            continue
        s0 = _parse_time_to_seconds_for_ranges(m.group("start"))
        s1 = _parse_time_to_seconds_for_ranges(m.group("end").lower())
        if s0 is None or s1 is None or s1 <= s0:
            out.append(None)
            continue
        out.append(s1 - s0)
    return out


class RunScreen(Screen):
    """Download runner with filtered events + optional raw details + live progress + log-to-file."""

    show_details: reactive[bool] = reactive(False)
    spinner_frame: reactive[int] = reactive(0)

    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(
        self,
        *,
        url: str,
        ranges_norm: List[str],
        download_root: Path,
        total_fragments: int,
        media_mode: str,       # "video" | "audio"
        preset_mode: str,      # "quality" | "compact" (video only)
        container_mode: str,   # "mkv" | "mp4" (video only)
        whole_video: bool,
        zen_profile_path: Optional[str],
    ) -> None:
        super().__init__()
        self.url = url
        self.ranges_norm = ranges_norm[:]
        self.download_root = download_root
        self.total_fragments = max(1, int(total_fragments))
        self.media_mode = media_mode
        self.preset_mode = preset_mode
        self.container_mode = container_mode
        self.whole_video = bool(whole_video)
        self.zen_profile_path = zen_profile_path

        self.downloader_bin = _find_ytdlp_binary() or "yt-dlp"
        self.seg_lengths = _segment_lengths_from_ranges(self.ranges_norm)

        self.proc: Optional[asyncio.subprocess.Process] = None
        self.done: bool = False
        self.cancel_requested: bool = False

        self.progress = LiveProgress(
            fragment_idx=1, seg_len_s=self.seg_lengths[0] if self.seg_lengths else None
        )

        self._ticker = None
        self._buffer = ""

        self.log_path: Optional[Path] = None
        self._log_fp = None

        self.dest_paths: List[Path] = []
        self.stage: str = "Preparing"

        self._ytdlp_last_pct: Optional[float] = None
        self._ytdlp_smooth_pct: Optional[float] = None

        # auth-on-fail state
        self._recent_lines: Deque[str] = deque(maxlen=250)
        self._last_run_authy: bool = False
        self._auth_method: Optional[AuthMethod] = None
        self._auth_temp_files: List[Path] = []

        # internal switches
        self._ignore_destinations: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="panel"):
            yield Static(colored_ascii_block(ASCII_ART_HEADER), id="ascii_small")
            yield Static("Ctrl+C cancel  |  D toggle details  |  Enter exits when done", classes="hint")
            yield Static("", id="status_line", classes="status_line")
            with VerticalScroll(id="run_scroll"):
                yield Label("Activity", classes="section_title")
                yield RichLog(id="events", highlight=True, wrap=True)
                yield Label("Details (press D)", id="details_label", classes="section_title")
                yield RichLog(id="details", highlight=True, wrap=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.download_root.mkdir(parents=True, exist_ok=True)

        logs_dir = Path(__file__).resolve().parent / "_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.log_path = logs_dir / f"ytfrags_{ts}.log"
        self._log_fp = self.log_path.open("w", encoding="utf-8", errors="replace")

        self.query_one("#details_label", Label).display = False
        self.query_one("#details", RichLog).display = False

        events = self.query_one("#events", RichLog)
        events.write("Starting…")
        events.write(f"Log file: {self.log_path}")
        if self.media_mode == "audio":
            events.write("Mode: AUDIO ONLY")
        else:
            if self.preset_mode == "compact":
                events.write(f"Mode: VIDEO ({self.container_mode.upper()}, COMPACT)")
            else:
                events.write(f"Mode: VIDEO ({self.container_mode.upper()})")
        if self.whole_video:
            events.write("Selection: FULL VIDEO (no trimming)")
        events.write("")

        self._ticker = self.set_interval(0.1, self._tick)
        asyncio.create_task(self._run_pipeline())
        self._render_status_line()

    def _tick(self) -> None:
        if self.done:
            return
        self.spinner_frame = (self.spinner_frame + 1) % len(self.SPINNER)
        self._render_status_line()

    def _render_status_line(self) -> None:
        spin = "✓" if self.done else self.SPINNER[self.spinner_frame % len(self.SPINNER)]

        frag = f"{self.progress.fragment_idx}/{self.total_fragments}" if self.total_fragments > 1 else "1/1"

        per_frag_pct = self.progress.percent
        if per_frag_pct is None:
            overall_pct = None
        else:
            overall_pct = ((self.progress.fragment_idx - 1) + per_frag_pct / 100.0) / self.total_fragments * 100.0

        bar_width = 24
        if overall_pct is None:
            bar = "[" + ("░" * bar_width) + "]"
            pct_txt = "--.-%"
        else:
            filled = int(round((overall_pct / 100.0) * bar_width))
            filled = max(0, min(bar_width, filled))
            bar = "[" + ("█" * filled) + ("░" * (bar_width - filled)) + "]"
            pct_txt = f"{overall_pct:5.1f}%"

        stats = []
        if self.progress.cur_time_s is not None:
            cur = _format_hhmmss(self.progress.cur_time_s)
            total = _format_hhmmss(self.progress.seg_len_s) if self.progress.seg_len_s is not None else "--:--:--"
            stats.append(f"{cur}/{total}")
        if self.progress.size:
            stats.append(self.progress.size)
        if self.progress.speed_x:
            stats.append(self.progress.speed_x)
        if self.progress.eta_s is not None and not self.done:
            stats.append(f"ETA {_format_eta(self.progress.eta_s)}")

        extra = "  ".join(stats)
        line = f"{spin} {self.stage}  (frag {frag})  {bar}  {pct_txt}"
        if extra:
            line += f"  {extra}"
        if self.cancel_requested and not self.done:
            line += "  (canceling…)"
        self.query_one("#status_line", Static).update(line)

    def _write_log_file(self, text: str) -> None:
        if not self._log_fp:
            return
        try:
            self._log_fp.write(text)
            self._log_fp.flush()
        except Exception:
            pass

    def _append_details(self, line: str) -> None:
        details = self.query_one("#details", RichLog)
        if _is_url_spam(line):
            return
        if len(line) > 800:
            details.write(line[:800] + " …(truncated)")
        else:
            details.write(line)

    def _append_event(self, line: str) -> None:
        events = self.query_one("#events", RichLog)
        if len(line) > 360:
            events.write(line[:360] + " …(truncated)")
        else:
            events.write(line)

    def _reset_for_new_run(self) -> None:
        self.dest_paths = []
        self._buffer = ""
        self._recent_lines.clear()
        self._last_run_authy = False

        self.progress.fragment_idx = 1
        self.progress.seg_len_s = self.seg_lengths[0] if self.seg_lengths else None
        self.progress.percent = None
        self.progress.cur_time_s = None
        self.progress.size = ""
        self.progress.speed_x = ""
        self.progress.bitrate = ""
        self.progress.eta_s = None

        self._ytdlp_last_pct = None
        self._ytdlp_smooth_pct = None

    def _extract_destination_path(self, line: str) -> Optional[Path]:
        idx = line.find("Destination:")
        if idx == -1:
            return None
        path_str = line[idx + len("Destination:") :].strip()
        if not path_str:
            return None
        if (path_str.startswith("'") and path_str.endswith("'")) or (path_str.startswith('"') and path_str.endswith('"')):
            path_str = path_str[1:-1]
        try:
            p = Path(path_str)
            if not p.is_absolute():
                p = (self.download_root / p).resolve()
            return p
        except Exception:
            return None

    def _on_new_destination(self, dest: Optional[Path]) -> None:
        if dest is not None:
            self.dest_paths.append(dest)

        self.progress.fragment_idx = min(len(self.dest_paths), self.total_fragments)

        if self.whole_video:
            return

        idx0 = self.progress.fragment_idx - 1
        if 0 <= idx0 < len(self.seg_lengths):
            self.progress.seg_len_s = self.seg_lengths[idx0]
        else:
            self.progress.seg_len_s = None
        self.progress.percent = 0.0
        self.progress.cur_time_s = 0.0
        self.progress.eta_s = None

    def _parse_ffmpeg_stats(self, line: str) -> bool:
        m = _FFMPEG_RE.search(line)
        if not m:
            return False
        t = (m.group("time") or "").strip()
        cur_s = _parse_time_to_seconds_generic(t)
        self.progress.cur_time_s = cur_s

        self.progress.bitrate = (m.group("bitrate") or "").strip()
        self.progress.speed_x = (m.group("speed") or "").strip()

        elapsed_raw = (m.group("elapsed") or "").strip()
        elapsed_s = _parse_time_to_seconds_generic(elapsed_raw) if elapsed_raw else None

        size_m = re.search(r"size=\s*(\S+)", line)
        if size_m:
            self.progress.size = size_m.group(1)

        if cur_s is not None and self.progress.seg_len_s is not None and self.progress.seg_len_s > 0:
            ratio = max(0.0, min(1.0, cur_s / self.progress.seg_len_s))
            self.progress.percent = ratio * 100.0

            remaining = self.progress.seg_len_s * (1.0 - ratio)
            if elapsed_s is not None and ratio > 0.01:
                self.progress.eta_s = elapsed_s * (1.0 - ratio) / ratio
            else:
                sx = self.progress.speed_x.lower().rstrip("x")
                try:
                    sp = float(sx)
                except Exception:
                    sp = None
                if sp and sp > 0:
                    self.progress.eta_s = remaining / sp
                else:
                    self.progress.eta_s = None
        else:
            self.progress.percent = None
            self.progress.eta_s = None
        return True

    def _parse_ytdlp_percent(self, line: str) -> bool:
        m = _YTDLP_PCT_RE.match(line.strip())
        if not m:
            return False
        try:
            pct = float(m.group("pct"))
        except Exception:
            return False

        pct = max(0.0, min(100.0, pct))

        if self.whole_video:
            if self._ytdlp_last_pct is None or self._ytdlp_smooth_pct is None:
                self._ytdlp_last_pct = pct
                self._ytdlp_smooth_pct = pct
            else:
                if pct < 2.0 and self._ytdlp_last_pct > 95.0:
                    self._ytdlp_last_pct = pct
                    self._ytdlp_smooth_pct = pct
                else:
                    pct = max(pct, self._ytdlp_last_pct)
                    self._ytdlp_last_pct = pct
                    alpha = 0.20
                    self._ytdlp_smooth_pct = self._ytdlp_smooth_pct + alpha * (pct - self._ytdlp_smooth_pct)

            self.progress.percent = self._ytdlp_smooth_pct
            return True

        self.progress.percent = pct
        return True

    def _handle_line(self, line: str, is_cr_update: bool) -> None:
        self._write_log_file(line + "\n")
        self._recent_lines.append(line)

        if self.show_details:
            if not is_cr_update or self._parse_ffmpeg_stats(line):
                self._append_details(line)

        if (not self._ignore_destinations) and "Destination:" in line:
            dest = self._extract_destination_path(line)
            self._on_new_destination(dest)

        if self._parse_ffmpeg_stats(line) or self._parse_ytdlp_percent(line):
            self._render_status_line()
            return

        if _should_keep_event(line) and not _is_url_spam(line):
            self._append_event(line)

    def _consume_buffer(self) -> None:
        while True:
            n_idx = self._buffer.find("\n")
            r_idx = self._buffer.find("\r")
            if n_idx == -1 and r_idx == -1:
                break
            if n_idx == -1:
                sep_idx, sep = r_idx, "\r"
            elif r_idx == -1:
                sep_idx, sep = n_idx, "\n"
            else:
                sep_idx, sep = (r_idx, "\r") if r_idx < n_idx else (n_idx, "\n")
            chunk = self._buffer[:sep_idx]
            self._buffer = self._buffer[sep_idx + 1 :]
            line = chunk.strip()
            if not line:
                continue
            self._handle_line(line, is_cr_update=(sep == "\r"))

    async def _run_subprocess_with_live_parse(self, cmd: List[str]) -> int:
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert self.proc.stdout is not None
        self._buffer = ""
        while True:
            data = await self.proc.stdout.read(2048)
            if not data:
                break
            text = data.decode(errors="replace")
            self._buffer += text
            self._consume_buffer()
        self._consume_buffer()
        rc = await self.proc.wait()
        return int(rc)

    async def _run_quick_capture(self, cmd: List[str]) -> Tuple[int, str]:
        """
        Run a short yt-dlp probe (e.g., --skip-download) and capture output.
        This avoids polluting the TUI with probe noise, but still logs to file.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            out_chunks: List[bytes] = []
            while True:
                data = await proc.stdout.read(4096)
                if not data:
                    break
                out_chunks.append(data)
            rc = await proc.wait()
            out = b"".join(out_chunks).decode(errors="replace")
            self._write_log_file("\n# --- PROBE OUTPUT BEGIN ---\n")
            self._write_log_file(out)
            if not out.endswith("\n"):
                self._write_log_file("\n")
            self._write_log_file("# --- PROBE OUTPUT END ---\n\n")
            return int(rc), out
        except Exception as e:
            self._write_log_file(f"\n# Probe failed to execute: {e}\n")
            return 1, str(e)

    def _ffmpeg_transcode_cmd(self, inp: Path, out: Path) -> List[str]:
        return [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-i",
            str(inp),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            MP4_X264_PRESET,
            "-crf",
            MP4_X264_CRF,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            MP4_AAC_BITRATE,
            "-movflags",
            "+faststart",
            str(out),
        ]

    def _cleanup_auth_temp_files(self) -> None:
        for p in list(self._auth_temp_files):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        self._auth_temp_files = []

    def _last_run_text(self) -> str:
        return "\n".join(list(self._recent_lines)[-180:])

    def _detect_auth_failure_from_last_run(self, rc: int) -> bool:
        text = self._last_run_text()
        if rc == 0:
            return False
        return _looks_like_auth_problem(text)

    async def _build_auth_method_for_candidate(self, cand: CookieCandidate) -> Optional[AuthMethod]:
        if cand.kind == "zen":
            if not cand.zen_db or not cand.zen_db.exists():
                return None
            # Export Zen cookies.sqlite to a temp Netscape file
            tmp_cookie = Path(tempfile.mkstemp(prefix="ytfrags_zen_", suffix=".cookies.txt")[1])
            ok = _export_firefox_sqlite_to_netscape(cand.zen_db, tmp_cookie)
            if not ok:
                try:
                    tmp_cookie.unlink(missing_ok=True)
                except Exception:
                    pass
                return None
            self._auth_temp_files.append(tmp_cookie)
            return AuthMethod(label="Zen", args=["--cookies", str(tmp_cookie)], temp_files=[tmp_cookie])

        if cand.kind == "browser" and cand.browser:
            if cand.browser not in YTDLP_COOKIE_BROWSERS:
                return None
            return AuthMethod(
                label=cand.label,
                args=["--cookies-from-browser", cand.browser],
                temp_files=[],
            )

        return None

    async def _select_auth_method(self, base_cmd: List[str]) -> Optional[AuthMethod]:
        """
        Rank candidates, then probe in order using --skip-download to avoid repeated real downloads.

        NEW BEHAVIOR:
        0) Try Zen root (~/.zen) *first* via yt-dlp's native Firefox cookie extractor:
            --cookies-from-browser firefox:~/.zen
            This avoids hardcoding the per-install profile folder name (e.g. "oplhmacu.Default (release)").

        1) If that fails, fall back to the existing ranked candidates (Zen-export, Firefox, Chromium-family, etc.)
        """
        # Probe command: same base, just add skip-download and print title to keep it short.
        probe_extras = ["--skip-download", "--print", "title"]

        def log_line(msg: str) -> None:
            self._append_event(msg)
            self._write_log_file(msg + "\n")

        # ------------------------------------------------------------
        # Step 0: Try Zen ROOT first (or user-specified zen dir if given)
        # ------------------------------------------------------------
        zen_dir_to_try: Optional[Path] = None
        zen_label = "Zen (root)"

        if self.zen_profile_path:
            p = Path(self.zen_profile_path).expanduser()
            # If user gave a directory, treat it as "try this directory directly with cookies-from-browser"
            # This supports them passing ~/.zen or a profile directory.
            if p.is_dir():
                zen_dir_to_try = p
                zen_label = "Zen (from --zen-profile-path)"
        else:
            root = _zen_profiles_root()
            if root.exists():
                zen_dir_to_try = root
                zen_label = "Zen (root)"

        if zen_dir_to_try is not None and zen_dir_to_try.exists() and zen_dir_to_try.is_dir():
            log_line("Auth required → trying Zen cookies via yt-dlp (root dir) …")
            zen_method = AuthMethod(
                label=zen_label,
                args=["--cookies-from-browser", f"firefox:{str(zen_dir_to_try)}"],
                temp_files=[],
            )

            probe_cmd = _insert_after_bin(base_cmd, [*zen_method.args, *probe_extras])
            self._write_log_file(f"# PROBE using {zen_method.label}: {shlex.join(probe_cmd)}\n")
            rc, out = await self._run_quick_capture(probe_cmd)

            # Success criteria: rc==0 and output doesn't look auth/bot-wall-ish
            if rc == 0 and not _looks_like_auth_problem(out):
                log_line(f"Auth OK via: {zen_method.label}")
                return zen_method
            else:
                # Not fatal; we just continue into the normal ranking system.
                log_line(f"Zen root probe failed (rc={rc}) → continuing with other cookie methods.")

        # ------------------------------------------------------------
        # Step 1: Existing ranked candidates (unchanged behavior)
        # ------------------------------------------------------------
        candidates = _rank_cookie_candidates(self.zen_profile_path)

        log_line("Auth required → attempting browser cookies (auto)…")
        self._write_log_file("# Auth selection candidates:\n")
        for c in candidates:
            self._write_log_file(f"#   {c.label:10s} score={c.score}  reason={c.reason}\n")
        self._write_log_file("\n")

        for cand in candidates:
            if self.cancel_requested:
                return None

            method = await self._build_auth_method_for_candidate(cand)
            if method is None:
                continue

            probe_cmd = _insert_after_bin(base_cmd, [*method.args, *probe_extras])

            self._write_log_file(f"# PROBE using {method.label}: {shlex.join(probe_cmd)}\n")
            rc, out = await self._run_quick_capture(probe_cmd)

            if rc == 0 and not _looks_like_auth_problem(out):
                log_line(f"Auth OK via: {method.label}")
                return method

        return None

    async def _run_ytdlp_with_auth_on_fail(self, cmd: List[str], *, stage: str) -> int:
        """
        Run yt-dlp. If it fails with auth-like symptoms, pick an auth method and retry once.
        If an auth method is already chosen, apply it immediately.
        """
        def log(msg: str) -> None:
            self._append_event(msg)
            self._write_log_file(msg + "\n")

        # If we already picked auth, apply it immediately.
        run_cmd = cmd
        if self._auth_method is not None:
            run_cmd = _insert_after_bin(cmd, self._auth_method.args)

        # First attempt
        self._reset_for_new_run()
        self.stage = stage
        log(f"$ {shlex.join(run_cmd)}")
        rc = await self._run_subprocess_with_live_parse(run_cmd)

        if self.cancel_requested:
            return rc

        authy = self._detect_auth_failure_from_last_run(rc)
        self._last_run_authy = authy

        if rc == 0:
            return rc

        # If not auth-looking, do not retry with cookies.
        if not authy:
            return rc

        # If we already had auth and still failed, don't loop.
        if self._auth_method is not None:
            return rc

        # Select auth method via probing, then retry once.
        self._auth_method = await self._select_auth_method(cmd)
        if self._auth_method is None:
            # User-facing instruction (only when auth truly seems required)
            self._append_event("")
            self._append_event("Authentication seems required, but I couldn't find usable browser cookies.")
            self._append_event("Fix: Log into Firefox, then run this manually:")
            self._append_event(f'    yt-dlp -v --cookies-from-browser firefox --no-playlist "{self.url}"')
            return rc

        # Retry with chosen auth method
        run_cmd2 = _insert_after_bin(cmd, self._auth_method.args)
        self._reset_for_new_run()
        self.stage = f"{stage} (auth: {self._auth_method.label})"
        log(f"$ {shlex.join(run_cmd2)}")
        rc2 = await self._run_subprocess_with_live_parse(run_cmd2)
        self._last_run_authy = self._detect_auth_failure_from_last_run(rc2)
        return rc2

    async def _run_pipeline(self) -> None:
        def log(msg: str) -> None:
            self._append_event(msg)
            self._write_log_file(msg + "\n")

        try:
            if self.media_mode == "audio":
                cmd_audio = build_ytdlp_command(
                    url=self.url,
                    ranges=self.ranges_norm,
                    download_root=self.download_root,
                    downloader_bin=self.downloader_bin,
                    profile="audio",
                    whole_video=self.whole_video,
                    preset="quality",
                )

                rc = await self._run_ytdlp_with_auth_on_fail(cmd_audio, stage="Downloading (audio)")
                self._finalize(rc)
                return

            # Video
            if self.container_mode == "mkv":
                cmd_primary = build_ytdlp_command(
                    url=self.url,
                    ranges=self.ranges_norm,
                    download_root=self.download_root,
                    downloader_bin=self.downloader_bin,
                    profile="mkv",
                    whole_video=self.whole_video,
                    preset=self.preset_mode,
                )

                rc = await self._run_ytdlp_with_auth_on_fail(cmd_primary, stage="Downloading")
                self._finalize(rc)
                return

            # Video MP4
            cmd_primary = build_ytdlp_command(
                url=self.url,
                ranges=self.ranges_norm,
                download_root=self.download_root,
                downloader_bin=self.downloader_bin,
                profile="mp4_direct",
                whole_video=self.whole_video,
                preset=self.preset_mode,
            )

            cmd_fallback = build_ytdlp_command(
                url=self.url,
                ranges=self.ranges_norm,
                download_root=self.download_root,
                downloader_bin=self.downloader_bin,
                profile="mkv_intermediate",
                whole_video=self.whole_video,
                preset=self.preset_mode,
            )

            rc = await self._run_ytdlp_with_auth_on_fail(cmd_primary, stage="Downloading")

            if self.cancel_requested:
                self._finalize(rc)
                return

            produced_mp4 = any(p.suffix.lower() == ".mp4" for p in self.dest_paths)
            if rc == 0 and produced_mp4:
                self._finalize(rc)
                return

            # If it failed and it still looks like auth, don't bother with format fallback.
            if rc != 0 and self._last_run_authy:
                self._finalize(rc)
                return

            log("")
            log("MP4 direct path unavailable (or produced non-mp4). Falling back: MKV download + H.264/AAC transcode.")

            rc2 = await self._run_ytdlp_with_auth_on_fail(cmd_fallback, stage="Downloading (fallback)")
            if rc2 != 0 or self.cancel_requested:
                self._finalize(rc2)
                return

            self.stage = "Transcoding"
            inputs = [p for p in self.dest_paths if p.exists() and p.suffix.lower() != ".mp4"]
            if not inputs:
                log("No files found to transcode (unexpected).")
                self._finalize(0)
                return

            for i, inp in enumerate(inputs, start=1):
                if self.cancel_requested:
                    break
                out = inp.with_suffix(".mp4")

                self.progress.fragment_idx = min(i, self.total_fragments)
                idx0 = self.progress.fragment_idx - 1
                self.progress.seg_len_s = self.seg_lengths[idx0] if 0 <= idx0 < len(self.seg_lengths) else None
                self.progress.percent = 0.0
                self.progress.cur_time_s = 0.0
                self.progress.size = ""
                self.progress.speed_x = ""
                self.progress.eta_s = None

                log("")
                log(f"Transcoding {i}/{len(inputs)} → {out.name}")
                ff_cmd = self._ffmpeg_transcode_cmd(inp, out)
                log(f"$ {shlex.join(ff_cmd)}")

                # ffmpeg run (no auth logic)
                self._reset_for_new_run()
                self.stage = "Transcoding"
                rc_t = await self._run_subprocess_with_live_parse(ff_cmd)
                if rc_t != 0:
                    log(f"Transcode failed for: {inp}")
                    self._finalize(rc_t)
                    return

                try:
                    inp.unlink(missing_ok=True)
                except Exception:
                    pass

            self._finalize(0)
        finally:
            self._cleanup_auth_temp_files()

    def _finalize(self, rc: int) -> None:
        self.done = True
        try:
            if self._ticker:
                self._ticker.stop()
        except Exception:
            pass
        try:
            if self._log_fp:
                self._log_fp.close()
        except Exception:
            pass

        if rc == 0:
            self.progress.percent = 100.0
            if self.whole_video:
                self._ytdlp_last_pct = 100.0
                self._ytdlp_smooth_pct = 100.0

        self.stage = "Done"
        self._render_status_line()

        events = self.query_one("#events", RichLog)
        events.write("")
        events.write(f"Done. Exit code: {rc}")
        events.write(f"Output root: {self.download_root}")
        if self.log_path:
            events.write(f"Log saved: {self.log_path}")
        events.write("Press Enter to exit.")

    def watch_show_details(self, show: bool) -> None:
        self.query_one("#details_label", Label).display = show
        self.query_one("#details", RichLog).display = show

    def on_key(self, event: Key) -> None:
        if event.key.lower() == "d":
            self.show_details = not self.show_details
            event.stop()
            return

        if event.key == "ctrl+c":
            if self.proc and self.proc.returncode is None:
                self.cancel_requested = True
                self._render_status_line()
                try:
                    self.proc.terminate()
                    self.query_one("#events", RichLog).write("[Cancel requested]")
                except ProcessLookupError:
                    pass
            event.stop()
            return

        if event.key == "enter" and self.done:
            self.app.exit()
            event.stop()
            return


class YtFragsApp(App):
    CSS = """
    Screen { background: $surface; }

    #panel { width: 100%; height: 100%; border: round $primary; padding: 1 2; }
    #scroll { height: 1fr; border: round $panel; padding: 1 1; }
    #flow { height: auto; }
    RangeRow { height: auto; }
    EnterPrompt { height: auto; }
    CountSelector { height: auto; }
    TwoChoiceSelector { height: auto; }

    #controls { color: $text-muted; margin-bottom: 1; width: 100%; content-align: center middle; }
    #ascii, #ascii_small { color: $text-muted; content-align: center middle; height: auto; }

    .section_title { margin-top: 1; text-style: bold; }
    .hint { color: $text-muted; margin-top: 1; margin-bottom: 1; height: auto; }
    .hint_block { color: $text-muted; margin-top: 1; margin-bottom: 1; height: auto; }
    .status { color: $text; margin-top: 1; height: auto; }

    .status_line { margin-top: 1; margin-bottom: 1; height: auto; border: round $panel; padding: 0 1; }
    #run_scroll { height: 1fr; border: round $panel; padding: 1 1; }

    Input.invalid { border: tall $error; }

    .count_row { height: auto; margin-top: 1; margin-bottom: 1; align: left middle; }
    .count_label { width: 11; content-align: left middle; }
    .count_value { width: 6; content-align: center middle; border: round $panel; margin-right: 1; }
    CountSelector:focus .count_value { border: round $primary; }
    CountSelector.confirmed .count_value { border: round $success; }

    .toggle_row { height: auto; margin-top: 1; margin-bottom: 1; align: left middle; }
    .toggle_label { width: 11; content-align: left middle; }
    .toggle_value { width: 25; padding: 0 1; content-align: center middle; border: round $panel; margin-right: 1; }
    TwoChoiceSelector:focus .toggle_value { border: round $primary; }
    .toggle_hint { color: $text-muted; }

    .range_row { height: auto; margin-top: 1; align: left middle; }
    .range_input { width: 1fr; }
    .range_arrow { width: 3; content-align: center middle; color: $text-muted; }

    .enter_prompt { border: round $panel; padding: 1 1; margin-top: 1; text-style: bold; }
    EnterPrompt:focus .enter_prompt { border: round $primary; }

    .review_line { height: auto; margin-top: 1; }
    .review_block { height: auto; margin-top: 1; }
    .review_cmd { height: auto; margin-top: 1; color: $text-muted; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        default_media_mode: str = "video",
        default_preset_mode: str = "quality",
        default_container_mode: str = "mkv",
        startup_note: str = "",
        zen_profile_path: Optional[str] = None,
        default_theme: str = "tokyo-night",
    ) -> None:
        super().__init__()
        self.state = AppState(media_mode=default_media_mode, preset_mode=default_preset_mode, container_mode=default_container_mode)
        self.download_root = _get_videos_dir() / "yt-fragments"

        self.default_media_mode = default_media_mode
        self.default_preset_mode = default_preset_mode
        self.default_container_mode = default_container_mode
        self.startup_note = startup_note.strip()

        self.zen_profile_path = zen_profile_path

        # Theme: load last used theme; otherwise use your chosen default.
        self._preferred_theme = _load_saved_theme() or default_theme

    def on_mount(self) -> None:
        # Set theme programmatically (same mechanism as the palette uses). :contentReference[oaicite:1]{index=1}
        try:
            self.theme = self._preferred_theme
        except Exception:
            pass

        self.push_screen(WizardScreen())

    async def shutdown(self) -> None:
        # Save theme when the app exits (works even on Ctrl+C / normal exit in Textual apps). :contentReference[oaicite:2]{index=2}
        try:
            t = getattr(self, "theme", None)
            if t:
                _save_theme(str(t))
        finally:
            await super().shutdown()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ytfrags TUI: download time-range fragments from a video.")
    p.add_argument("--mp4", action="store_true", help="Default container = MP4 (video only).")
    p.add_argument("--audio", action="store_true", help="Default media = audio only (best audio).")
    p.add_argument("--compact", action="store_true", help="Default preset = Compact (video only).")
    p.add_argument(
        "--zen-profile-path",
        type=str,
        default=None,
        help='Path to Zen profile DIR (containing cookies.sqlite) or to cookies.sqlite directly. '
             'If omitted, uses the default "Default (release)" profile.',
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    default_media = "audio" if args.audio else "video"
    default_preset = "compact" if args.compact else "quality"
    default_container = "mp4" if args.mp4 else "mkv"

    note = ""
    if args.audio and args.mp4:
        note = "--mp4 ignored because --audio selects audio-only (container not applicable)."
    if args.audio and args.compact:
        note = (note + " " if note else "") + "--compact ignored because --audio selects audio-only (preset not applicable)."

    YtFragsApp(
        default_media_mode=default_media,
        default_preset_mode=default_preset,
        default_container_mode=default_container,
        startup_note=note,
        zen_profile_path=args.zen_profile_path,
    ).run()
# --- END ytfrags_tui.py ---
