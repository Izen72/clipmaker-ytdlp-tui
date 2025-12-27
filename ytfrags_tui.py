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
       - Method (MP4 only): Direct (default) / Remux
     Notes:
       - CLI flags only set the DEFAULT selection when the app starts:
           --mp4       -> defaults container to MP4 (video-only)
           --audio     -> defaults media to Audio (audio-only)
           --compact   -> defaults preset to Compact (video-only)
           --remux -> defaults MP4 method to Remux (video-only)
       - If you pick Audio in the TUI, Preset/Container/Method are hidden.
       - If you pick MKV, Method is hidden (not applicable).
  4) For each fragment: fill Start + End -> press Enter
  5) Review -> press Enter to start download

Output behavior:
  - Video + Preset=Quality + MKV:
      * "God Tier" Efficiency: Prioritizes AV1 > VP9 > H.264 (<=1080p) + best Opus audio.
      * Uses concurrent fragments (-N 8) for speed.

  - Video + Preset=Quality + MP4:
      * Method=Direct: Tries native H.264 download (fastest). If unavailable, falls back to Remux.
      * Method=Remux: Forces download of best source (AV1/VP9) then converts to MP4 (preserving the efficient codec).
                          (Best quality, but slower due to conversion).

  - Video + Preset=Compact:
      * Caps video at <=720p, prefers small/efficient codecs.
      * Uses concurrent fragments (-N 8).

  - Audio: Best audio available (Opus/AAC/etc).

Auth behavior:
  - Tries no cookies first.
  - On 403/Login error, tries Zen -> Firefox -> Chrome/etc cookies automatically.
"""

from __future__ import annotations
import json
import argparse
import asyncio
import os
import platform
import queue
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Deque, List, Optional, Tuple, Any, Dict, Callable

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

# Native Library Import
import yt_dlp
from yt_dlp.utils import DownloadError


# ---------------------------#
#         ASCII art          #
# ---------------------------#
ASCII_ART_HEADER = r"""
 ██████╗██╗     ██╗██████╗ ███╗   ███╗ █████╗ ██╗  ██╗███████╗██████╗
██╔════╝██║     ██║██╔══██╗████╗ ████║██╔══██╗██║ ██╔╝██╔════╝██╔══██╗
██║     ██║     ██║██████╔╝██╔████╔██║███████║█████╔╝ █████╗  ██████╔╝
██║     ██║     ██║██╔═══╝ ██║╚██╔╝██║██╔══██║██╔═██╗ ██╔══╝  ██╔══██╗
╚██████╗███████╗██║██║     ██║ ╚═╝ ██║██║  ██║██║  ██╗███████╗██║  ██║
 ╚═════╝╚══════╝╚═╝╚═╝     ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝

                          [ R E M U X E D ]

                           v2.0 · by: Izen

"""


def colored_ascii_block(s: str) -> Text:
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

    lines = s.split("\n")
    note_start: Optional[int] = None
    for i in range(len(lines)):
        if is_note_line(lines[i]):
            note_start = i
            break

    out = Text()
    for i, line in enumerate(lines):
        if note_start is not None and i >= note_start:
            out.append(line, style="grey62")
        elif "R E M U X E D" in line:  # <--- Added check here
            out.append(line, style="bold bright_white")
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

# MP4 fallback remux settings (Best Video Codec + AAC)
MP4_AAC_BITRATE = "320k"

# Compact preset knobs
COMPACT_MAX_HEIGHT = 720
COMPACT_MAX_FPS = 30
COMPACT_MAX_AUDIO_ABR = 160

# Speed Boost: Concurrent fragments for ALL video downloads
VIDEO_CONCURRENT_FRAGMENTS = 8

# Supported cookie browsers (yt-dlp list)
YTDLP_COOKIE_BROWSERS = ["firefox", "chromium", "chrome", "brave", "edge", "vivaldi", "opera", "whale", "safari"]
COOKIE_BROWSER_ORDER = ["firefox", "chromium", "chrome", "brave", "edge", "vivaldi", "opera", "whale", "safari"]


def _find_ytdlp_binary() -> Optional[str]:
    # Kept for display/check, though main logic now uses library
    return shutil.which("yt-dlp")


def _get_videos_dir() -> Path:
    s = platform.system()

    # Windows: proper API call to find the real folder (CSIDL_MYVIDEO)
    if s == "Windows":
        try:
            import ctypes.wintypes
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            # CSIDL_MYVIDEO = 0x000e
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x000e, None, 0, buf)
            val = buf.value
            if val:
                return Path(val)
        except Exception:
            pass
        return Path.home() / "Videos"

    # macOS: They call it "Movies"
    if s == "Darwin":
        return Path.home() / "Movies"

    # Linux/BSD: Keep your existing XDG logic
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
    s = platform.system()
    if s == "Windows":
        # Roaming is better for settings than Local
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif s == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        # Standard XDG layout for Linux/BSD
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))

    return base.expanduser() / "ytfrags"


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


def _safe_stat_mtime(p: Path) -> float:
    try:
        return float(p.stat().st_mtime)
    except Exception:
        return 0.0


def _copy_sqlite_for_read(db_path: Path) -> Optional[Path]:
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
    s = platform.system()
    if s == "Windows": return Path(os.environ["APPDATA"]) / "Mozilla" / "Firefox"
    if s == "Darwin": return Path.home() / "Library" / "Application Support" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


def _zen_profiles_root() -> Path:
    s = platform.system()
    if s == "Windows": return Path(os.environ["APPDATA"]) / "Zen"
    if s == "Darwin": return Path.home() / "Library" / "Application Support" / "Zen"
    return Path.home() / ".zen"


def _default_zen_profile_dir() -> Optional[Path]:
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
    q = "SELECT 1 FROM moz_cookies WHERE host LIKE ? OR host LIKE ? OR host LIKE ? LIMIT 1"
    return _sqlite_table_has_any_row(db, q, ("%youtube.%", "%google.%", "%accounts.google.%"))


def _chromium_cookie_db_for(browser: str) -> Optional[Path]:
    s = platform.system()
    h = Path.home()

    if s == "Windows":
        lad = Path(os.environ.get("LOCALAPPDATA", h / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", h / "AppData" / "Roaming"))
        mapping = {
            "chromium": lad / "Chromium" / "User Data" / "Default" / "Cookies",
            "chrome": lad / "Google" / "Chrome" / "User Data" / "Default" / "Cookies",
            "brave": lad / "BraveSoftware" / "Brave-Browser" / "User Data" / "Default" / "Cookies",
            "edge": lad / "Microsoft" / "Edge" / "User Data" / "Default" / "Cookies",
            "vivaldi": lad / "Vivaldi" / "User Data" / "Default" / "Cookies",
            "opera": roaming / "Opera Software" / "Opera Stable" / "Cookies",
            "whale": lad / "Naver" / "Naver Whale" / "User Data" / "Default" / "Cookies",
        }
    elif s == "Darwin":
        sup = h / "Library" / "Application Support"
        mapping = {
            "chromium": sup / "Chromium" / "Default" / "Cookies",
            "chrome": sup / "Google" / "Chrome" / "Default" / "Cookies",
            "brave": sup / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies",
            "edge": sup / "Microsoft Edge" / "Default" / "Cookies",
            "vivaldi": sup / "Vivaldi" / "Default" / "Cookies",
            "opera": sup / "com.operasoftware.Opera" / "Cookies",
            "whale": sup / "Naver Whale" / "Default" / "Cookies",
        }
    else:
        # Linux fallback
        cfg = h / ".config"
        mapping = {
            "chromium": cfg / "chromium" / "Default" / "Cookies",
            "chrome": cfg / "google-chrome" / "Default" / "Cookies",
            "brave": cfg / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies",
            "edge": cfg / "microsoft-edge" / "Default" / "Cookies",
            "vivaldi": cfg / "vivaldi" / "Default" / "Cookies",
            "opera": cfg / "opera" / "Default" / "Cookies",
            "whale": cfg / "naver-whale" / "Default" / "Cookies",
        }

    p = mapping.get(browser)
    return p if p and p.exists() else None


def _chromium_db_has_youtubeish_cookies(db: Path) -> bool:
    q = "SELECT 1 FROM cookies WHERE host_key LIKE ? OR host_key LIKE ? OR host_key LIKE ? LIMIT 1"
    return _sqlite_table_has_any_row(db, q, ("%youtube.%", "%google.%", "%accounts.google.%"))


@dataclass
class CookieCandidate:
    kind: str
    label: str
    browser: Optional[str] = None
    zen_db: Optional[Path] = None
    score: int = 0
    reason: str = ""


@dataclass
class AuthMethod:
    label: str
    # 'ydl_opts_subset' contains options to merge into the main ydl_opts (e.g. {'cookiefile': ...})
    ydl_opts_subset: Dict[str, Any]
    temp_files: List[Path]


def _rank_cookie_candidates(zen_profile_path: Optional[str]) -> List[CookieCandidate]:
    out: List[CookieCandidate] = []

    # Zen first
    zen_db = _resolve_zen_cookie_db(zen_profile_path)
    zen_score = 0
    zen_reason = "Zen preferred"
    if zen_db and zen_db.exists():
        zen_score += 100
        if _firefox_db_has_youtubeish_cookies(zen_db):
            zen_score += 50
        zen_score += int(min(30, max(0, (datetime.now().timestamp() - _safe_stat_mtime(zen_db)) // -86400)))
        zen_reason = f"Zen cookies.sqlite found ({'has' if _firefox_db_has_youtubeish_cookies(zen_db) else 'no'} YT/Google cookies hit)"
    else:
        zen_reason = "Zen cookies.sqlite not found"

    out.append(
        CookieCandidate(kind="zen", label="Zen", zen_db=zen_db, score=10_000 + zen_score, reason=zen_reason)
    )

    # Firefox
    ff_dbs = _list_firefox_cookie_dbs()
    ff_best = None
    ff_score = 0
    ff_reason = "Firefox profile cookies.sqlite not found"
    if ff_dbs:
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

    # Chromium & others
    popularity_bonus = {
        "chromium": 60, "chrome": 55, "brave": 50, "edge": 40,
        "vivaldi": 25, "opera": 20, "whale": 10, "safari": 1,
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

    out.sort(key=lambda c: c.score, reverse=True)
    return out


def _export_firefox_sqlite_to_netscape(db_path: Path, out_path: Path) -> bool:
    tmp = _copy_sqlite_for_read(db_path)
    if tmp is None:
        return False
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='moz_cookies'")
            if cur.fetchone() is None:
                return False

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

                host_s = host_s.replace("\t", " ").replace("\n", " ").replace("\r", " ")
                path_s = path_s.replace("\t", " ").replace("\n", " ").replace("\r", " ")
                name_s = name_s.replace("\t", " ").replace("\n", " ").replace("\r", " ")
                val_s = val_s.replace("\t", " ").replace("\n", " ").replace("\r", " ")

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
            \d+:\d{2}:\d{2}(?:\.\d+)?
            |
            \d+:\d{2}(?:\.\d+)?
            |
            \d+(?:\.\d+)?
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


# ---------------------------
# UPDATED SELECTORS (Max Quality + Efficiency)
# ---------------------------

def _format_selector_1080_best() -> str:
    """
    God Tier Selector for MKV:
    1. AV1 + Opus (Max efficiency/quality)
    2. VP9 + Opus (Great quality)
    3. H.264 + AAC (Fallback)
    All capped at <= 1080p.
    """
    return (
        "bv*[height<=1080][vcodec^=av01]+ba[acodec^=opus]/"
        "bv*[height<=1080][vcodec^=vp09]+ba[acodec^=opus]/"
        "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/"
        "bestvideo[height<=1080]+bestaudio/"
        "best[height<=1080]/best"
    )


def _format_selector_mp4_h264_1080() -> str:
    # High-profile H.264 priority for direct downloads
    return (
        "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/"
        "best[height<=1080][ext=mp4]/"
        "best[ext=mp4]"
    )


def _format_selector_compact_720() -> str:
    h = COMPACT_MAX_HEIGHT
    fps = COMPACT_MAX_FPS
    abr = COMPACT_MAX_AUDIO_ABR
    return (
        f"bv*[height<={h}][fps<={fps}][vcodec^=av01]+ba[acodec^=opus][abr<={abr}]/"
        f"bv*[height<={h}][fps<={fps}][vcodec^=vp09]+ba[acodec^=opus][abr<={abr}]/"
        f"bv*[height<={h}][fps<={fps}][vcodec^=av01]+ba[acodec^=opus]/"
        f"bv*[height<={h}][fps<={fps}][vcodec^=vp09]+ba[acodec^=opus]/"
        f"bv*[height<={h}][vcodec^=av01]+ba[acodec^=opus][abr<={abr}]/"
        f"bv*[height<={h}][vcodec^=vp09]+ba[acodec^=opus][abr<={abr}]/"
        f"bv*[height<={h}][vcodec^=av01]+ba[acodec^=opus]/"
        f"bv*[height<={h}][vcodec^=vp09]+ba[acodec^=opus]/"
        f"bv*[height<={h}]+ba[acodec^=opus]/"
        f"bv*[height<={h}]+ba/"
        f"best[height<={h}]/best"
    )


def _format_selector_mp4_h264_720() -> str:
    return (
        "bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/"
        "best[height<=720][ext=mp4]/"
        "best[ext=mp4]"
    )


def _format_selector_best_audio() -> str:
    # UPDATED: Prioritize Opus (AV1 of audio) over AAC (Compatibility)
    return (
        "ba[ext=opus]/"                 # 1. Native Opus container
        "ba[ext=webm][acodec^=opus]/"   # 2. WebM container with Opus
        "ba[ext=m4a]/"                  # 3. Fallback to AAC/M4A
        "ba[ext=mp4][acodec^=mp4a]/"
        "ba[acodec^=mp4a]/"
        "ba[ext=mp3]/"
        "ba[ext=flac]/"
        "ba[ext=wav]/"
        "ba[ext=ogg]/"
        "ba/bestaudio"
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


def _make_range_func(ranges_norm: List[str]) -> Callable:
    def range_func(info_dict, ydl):
        sections = []
        for r in ranges_norm:
            # r is expected to be *START-END (normalized)
            clean_r = r.lstrip('*')
            start_str, _, end_str = clean_r.partition('-')

            s = _parse_time_to_seconds_for_ranges(start_str)
            if s is None:
                s = 0.0

            e = _parse_time_to_seconds_for_ranges(end_str)
            # if e is None, it means infinity/end-of-video

            sections.append({'start_time': s, 'end_time': e})
        return sections
    return range_func


def build_ydl_opts(
    *,
    ranges_norm: List[str],
    download_root: Path,
    profile: str,  # "mkv" | "mp4_direct" | "mkv_intermediate" | "audio"
    whole_video: bool = False,
    preset: str = "quality",  # "quality" | "compact" (video only)
) -> Dict[str, Any]:

    is_audio = profile == "audio"

    # Base options
    opts = {
        'paths': {'home': str(download_root)},
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'restrictfilenames': True,  # safer filenames
        # Map file output logic
        # If whole video: title [id].ext
        # If ranges: title [id] - frag XX (start-end).ext
    }

    if whole_video:
        if is_audio:
            opts['outtmpl'] = "%(title)s [%(id)s]/%(title)s [%(id)s] - audio.%(ext)s"
        else:
            opts['outtmpl'] = "%(title)s [%(id)s]/%(title)s [%(id)s].%(ext)s"
    else:
        if is_audio:
            opts['outtmpl'] = (
                "%(title)s [%(id)s]/"
                "%(title)s [%(id)s] - audio frag %(section_number)02d "
                "(%(section_start>%H-%M-%S)s-%(section_end>%H-%M-%S)s).%(ext)s"
            )
        else:
            opts['outtmpl'] = (
                "%(title)s [%(id)s]/"
                "%(title)s [%(id)s] - frag %(section_number)02d "
                "(%(section_start>%H-%M-%S)s-%(section_end>%H-%M-%S)s).%(ext)s"
            )

    # Concurrency
    if not is_audio:
        opts['concurrent_fragment_downloads'] = VIDEO_CONCURRENT_FRAGMENTS

    # Format Selection
    if profile in ("mkv", "mkv_intermediate"):
        opts['merge_output_format'] = 'mkv'
        opts['format'] = _format_selector_compact_720() if preset == "compact" else _format_selector_1080_best()
    elif profile == "mp4_direct":
        opts['merge_output_format'] = 'mp4'
        opts['format'] = _format_selector_mp4_h264_720() if preset == "compact" else _format_selector_mp4_h264_1080()
    elif profile == "audio":
        opts['format'] = _format_selector_best_audio()
    else:
        # Default fallback
        opts['merge_output_format'] = 'mkv'
        opts['format'] = _format_selector_1080_best()

    # Ranges
    if not whole_video:
        opts['download_ranges'] = _make_range_func(ranges_norm)
        opts['force_keyframes_at_cuts'] = True  # recommended for accurate cuts

    return opts


@dataclass
class AppState:
    url: str = ""
    count: int = 1
    starts: List[str] = None
    ends: List[str] = None
    ranges_norm: List[str] = None
    whole_video: bool = False

    media_mode: str = "video"
    preset_mode: str = "quality"
    container_mode: str = "mkv"
    mp4_method: str = "direct"  # "direct" | "remux"

    def __post_init__(self) -> None:
        if self.starts is None:
            self.starts = []
        if self.ends is None:
            self.ends = []
        if self.ranges_norm is None:
            self.ranges_norm = []


class CountSelector(Widget):
    can_focus = True
    value: reactive[int] = reactive(1)
    confirmed: reactive[bool] = reactive(False)

    class Confirmed(Message):
        bubble = True
        def __init__(self, value: int) -> None:
            super().__init__()
            self.value = value

    def __init__(self, *, value: int = 1, min_value: int = 1, max_value: int = MAX_FRAGMENTS, id: str | None = None) -> None:
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

    def __init__(self, *, key: str, label: str, options: List[Tuple[str, str]], value: str, hint: str = "(←/→ change, Enter confirm)", id: str | None = None) -> None:
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

            # Use standard spaces for 100% reliable width in TUI
            SPACE = " "
            SEP = f"{SPACE}/{SPACE}"

            def render_opt(v: str, lab: str) -> str:
                if v == self.value:
                    # Selected: Brackets hug the label
                    return f"[{lab}]"
                else:
                    # Unselected: Spaces replace the brackets exactly
                    return f"{SPACE}{lab}{SPACE}"

            (v0, lab0), (v1, lab1) = self.options[0], self.options[1]
            left = render_opt(v0, lab0)
            right = render_opt(v1, lab1)

            return left + SEP + right

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
    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index

    def compose(self) -> ComposeResult:
        yield Label(f"Select range for Fragment {self.index}", classes="section_title")
        with Horizontal(classes="range_row"):
            yield Input(placeholder="Start  (e.g. 00:00:30 or -30)", id=f"range_start_{self.index}", classes="range_input")
            yield Static("→", classes="range_arrow")
            yield Input(placeholder="End  (e.g. 00:01:10 or inf / -10)", id=f"range_end_{self.index}", classes="range_input")
        yield Static(
            "Examples: 00:00:30 → 00:01:10   |   10:15 → inf   |   -30 → -10\n"
            "Tip: Start=0 and End=0 downloads the full video (no trimming).",
            classes="hint",
        )


class WizardScreen(Screen):
    phase: reactive[str] = reactive("url")
    current_fragment: reactive[int] = reactive(1)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="panel"):
            with VerticalScroll(id="scroll"):
                yield Static(
                    "Controls: Enter = confirm/next  |  ←/→ = toggles  |  PgUp/PgDn = scroll  |  Tab = focus  |  Ctrl+Q = quit",
                    id="controls",
                )
                yield Static(colored_ascii_block(ASCII_ART_HEADER), id="ascii")

                yield Label("Video URL", classes="section_title")
                yield Input(placeholder="Paste the YouTube URL here, then press Enter", id="url_input")
                yield Vertical(id="flow")
            yield Static("", id="status", classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#url_input", Input))
        self._set_status("Paste a URL and press Enter.")

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _scroll_end(self) -> None:
        scroll = self.query_one("#scroll", VerticalScroll)
        try:
            scroll.scroll_end(animate=False)
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
            scroll.scroll_relative(y=delta_y)
        except Exception:
            try:
                scroll.scroll_relative(0, delta_y)
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
                    self._set_status("Tip: Start=0 and End=0 downloads the FULL video, but only when fragment count is 1.")
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
        self.app.state.mp4_method = self.app.default_mp4_method

        self.phase = "output"
        await self._show_output_step()

    async def _show_output_step(self) -> None:
        note_lines: List[Widget] = []
        if self.app.startup_note:
            note_lines.append(Static(f"Note: {self.app.startup_note}", classes="hint"))

        await self._flow_mount(
            Label("Output settings", classes="section_title"),
            Static("Pick what to download. Audio ignores Preset/MKV/MP4. MKV ignores Method.", classes="hint"),
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
                hint="(video only)",
                id="preset_selector",
            ),
            TwoChoiceSelector(
                key="container",
                label="Container:",
                options=[("mkv", "MKV"), ("mp4", "MP4")],
                value=self.app.state.container_mode,
                hint="(video only)",
                id="container_selector",
            ),
            # New Method Selector for MP4
            TwoChoiceSelector(
                key="method",
                label="Method:",
                options=[("direct", "Direct"), ("remux", "Remux")],
                value=self.app.state.mp4_method,
                hint="(MP4 only) Direct=Fast, Remux=Max Quality and Smaller Size",
                id="method_selector",
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
            meth = self.query_one("#method_selector", TwoChoiceSelector)
        except Exception:
            return

        is_video = self.app.state.media_mode == "video"
        is_mp4 = self.app.state.container_mode == "mp4"

        preset.display = is_video
        cont.display = is_video
        meth.display = is_video and is_mp4

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
                self._set_status("Audio-only selected → Preset/Container/Method ignored. Press Enter to continue.")
            else:
                self._set_status("Video selected. Press Enter on Media to choose preset.")
            return

        if message.key == "preset":
            self.app.state.preset_mode = message.value
            return

        if message.key == "container":
            self.app.state.container_mode = message.value
            self._update_output_visibility()
            if message.value == "mp4":
                self._set_status("MP4 selected. Press Enter to configure Method (Direct vs Remux).")
            else:
                self._set_status("MKV selected. Press Enter to continue to ranges.")
            return

        if message.key == "method":
            self.app.state.mp4_method = message.value
            if message.value == "remux":
                self._set_status("Remux: Downloads Best source (AV1/VP9) then converts to MP4. Slower, better quality, smaller file.")
            else:
                self._set_status("Direct: Tries native H.264. Fastest. Fallback to remux if needed.")
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
            self._update_output_visibility()
            if self.app.state.container_mode == "mp4":
                self._set_status("Select Method (Direct/Remux) with ←/→, then press Enter.")
                self.call_after_refresh(lambda: self.set_focus(self.query_one("#method_selector", TwoChoiceSelector)))
                return
            else:
                await self._begin_ranges_phase()
                return

        if message.key == "method":
            self.app.state.mp4_method = message.value
            await self._begin_ranges_phase()
            return

    async def _begin_ranges_phase(self) -> None:
        self.phase = "ranges"
        self._set_status("Fill Start + End for Fragment 1 (press Enter on End).")
        await self._flow_mount(
            Label("Time syntax", classes="section_title"),
            Static(
                "Accepted: SS, MM:SS, HH:MM:SS (optional .ms). End may be 'inf'.\n"
                "Examples: 00:00:30 → 00:01:10 | 10:15 → inf | -30 → -10\n"
                "Tip: Start=0 and End=0 downloads the full video.",
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
        method = self.app.state.mp4_method

        # Build options dictionary to show preview info
        opts = build_ydl_opts(
            ranges_norm=ranges_norm,
            download_root=root,
            profile="audio" if media == "audio" else ("mp4_direct" if container == "mp4" and method == "direct" else "mkv"),
            whole_video=whole_video,
            preset=preset
        )

        output_note = "Output: "
        if media == "audio":
            output_note += "AUDIO ONLY (best audio available)."
        else:
            if container == "mkv":
                if preset == "compact":
                    output_note += f"MKV COMPACT (≤720p, AV1/VP9, {VIDEO_CONCURRENT_FRAGMENTS}x parallel)"
                else:
                    output_note += f"MKV QUALITY (≤1080p, AV1 > VP9, {VIDEO_CONCURRENT_FRAGMENTS}x parallel)"
            else:
                if method == "remux":
                    output_note += "MP4 REMUX (DL Best ≤1080p source -> Remux - Best Video Codec preserved)."
                else:
                    output_note += "MP4 DIRECT (Try Native H.264 -> Fallback Remux)."

        if whole_video:
            frag_line = "Fragments: full video"
            summary = "Fragment 1: FULL VIDEO (no trimming)"
        else:
            frag_line = f"Fragments: {len(ranges_norm)}"
            lines = [f"Fragment {i}: {s} → {e}" for i, (s, e) in enumerate(zip(self.app.state.starts, self.app.state.ends), start=1)]
            summary = "\n".join(lines)

        await self._flow_mount(
            Label("Review", classes="section_title"),
            Static(output_note, classes="review_line"),
            Static(f"URL: {url}", classes="review_line"),
            Static(frag_line, classes="review_line"),
            Static(summary, classes="review_block"),
            Static(f"Download folder: {root}", classes="review_line"),
            EnterPrompt("Press Enter to start downloading (Ctrl+Q to quit).", id="start_prompt"),
        )
        self.call_after_refresh(lambda: self.set_focus(self.query_one("#start_prompt", EnterPrompt)))
        self._set_status("Ready. Press Enter to start download.")

    @on(EnterPrompt.Triggered)
    async def _on_start_triggered(self, message: EnterPrompt.Triggered) -> None:
        if self.phase != "review":
            return
        # yt-dlp binary is NOT strictly required now, but useful if user wants to debug
        # ffmpeg IS required for merge and remux
        if not shutil.which(self.app.ffmpeg_path):
            self._set_status(f"Error: '{self.app.ffmpeg_path}' not found (required).")
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
                mp4_method=self.app.state.mp4_method,
                whole_video=self.app.state.whole_video,
                zen_profile_path=self.app.zen_profile_path,
            )
        )


def _format_hhmmss(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--:--"
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _format_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or seconds > 999 * 3600:
        return "--:--"
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


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


class YtDlpLogger:
    """Redirects yt-dlp internal logs to a thread-safe queue."""
    def __init__(self, msg_queue: queue.Queue):
        self.msg_queue = msg_queue

    def debug(self, msg):
        if not msg.startswith('[debug] '):
            self.msg_queue.put(("log", msg))

    def warning(self, msg):
        self.msg_queue.put(("log", f"[WARN] {msg}"))

    def error(self, msg):
        self.msg_queue.put(("log", f"[ERROR] {msg}"))


class RunScreen(Screen):
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
        media_mode: str,
        preset_mode: str,
        container_mode: str,
        mp4_method: str,
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
        self.mp4_method = mp4_method
        self.whole_video = bool(whole_video)
        self.zen_profile_path = zen_profile_path

        self.seg_lengths = _segment_lengths_from_ranges(self.ranges_norm)

        self.proc: Optional[asyncio.subprocess.Process] = None
        self.done: bool = False
        self.cancel_requested: bool = False
        self.progress = LiveProgress(fragment_idx=1, seg_len_s=self.seg_lengths[0] if self.seg_lengths else None)
        self._ticker = None
        self._buffer = ""
        self.log_path: Optional[Path] = None
        self._log_fp = None
        self.dest_paths: List[Path] = []
        self.stage: str = "Preparing"
        self._recent_lines: Deque[str] = deque(maxlen=250)
        self._last_run_authy: bool = False
        self._auth_method: Optional[AuthMethod] = None
        self._auth_temp_files: List[Path] = []

        # Thread-safe queue for comms between yt-dlp thread and TUI
        self.msg_queue = queue.Queue()

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
        events.write("Starting… (Using Native yt-dlp Library)")
        events.write(f"Log file: {self.log_path}")

        mode_str = "AUDIO ONLY" if self.media_mode == "audio" else f"VIDEO ({self.container_mode.upper()}, {self.preset_mode.upper()})"
        events.write(f"Mode: {mode_str}")
        events.write("")

        self._ticker = self.set_interval(0.1, self._tick)
        asyncio.create_task(self._run_pipeline())
        self._render_status_line()

    def _tick(self) -> None:
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == "log":
                    self.handle_lib_log(data)
                elif msg_type == "progress":
                    self.handle_progress_update(data)
        except queue.Empty:
            pass

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
        # If in remuxing stage, show specific message
        if self.stage == "Remuxing":
            stats.append("(please wait, processing video...)")

        if self.progress.size:
            stats.append(self.progress.size)
        if self.progress.speed_x:
            stats.append(self.progress.speed_x)
        if self.progress.eta_s is not None and not self.done and self.stage != "Remuxing":
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

    def handle_lib_log(self, msg: str) -> None:
            line = msg.strip()
            self._write_log_file(line + "\n")
            self._recent_lines.append(line)
            if self.show_details:
                self.query_one("#details", RichLog).write(line)
            if _should_keep_event(line) and not _is_url_spam(line):
                self.query_one("#events", RichLog).write(line)

            # --- PARSING LOGIC START ---
            found_path = None

            # Case 1: Standard download (often unquoted in Audio mode)
            # Log: "[download] Destination: filename.ext"
            if "Destination: " in line:
                _, _, found_path = line.partition("Destination: ")

            # Case 2: Merging formats (usually quoted)
            # Log: "[Merger] Merging formats into "filename.mkv""
            elif "Merging formats into" in line:
                m = re.search(r'Merging formats into "(.*?)"', line)
                if m:
                    found_path = m.group(1)

            # Case 3: Already downloaded
            # Log: "[download] filename.ext has already been downloaded"
            elif "has already been downloaded" in line:
                # Extract text between "] " and " has already..."
                m = re.search(r'\] (.*?) has already been downloaded', line)
                if m:
                    found_path = m.group(1)

            if found_path:
                found_path = found_path.strip()
                # Clean up quotes if present (standardization)
                if found_path.startswith('"') and found_path.endswith('"'):
                    found_path = found_path[1:-1]

                try:
                    p = Path(found_path)
                    if not p.is_absolute():
                        p = (self.download_root / p).resolve()

                    if p not in self.dest_paths:
                        self.dest_paths.append(p)
                        self.progress.fragment_idx = min(len(self.dest_paths) + 1, self.total_fragments)
                except Exception:
                    pass
            # --- PARSING LOGIC END ---

    def handle_progress_update(self, d: dict) -> None:
        if d['status'] == 'downloading':
            try:
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                if total:
                    self.progress.percent = (downloaded / total) * 100.0

                speed = d.get('speed')
                if speed:
                    self.progress.speed_x = f"{speed / 1024 / 1024:.1f}MiB/s"

                eta = d.get('eta')
                if eta:
                    self.progress.eta_s = float(eta)

                if total:
                    self.progress.size = f"{downloaded/1024/1024:.1f}/{total/1024/1024:.1f}MiB"
            except Exception:
                pass
        elif d['status'] == 'finished':
            self.progress.percent = 100.0

    def progress_hook(self, d):
        self.msg_queue.put(("progress", d))

    def _run_lib_ytdlp(self, opts: Dict[str, Any]) -> int:
        opts['logger'] = YtDlpLogger(self.msg_queue)
        opts['progress_hooks'] = [self.progress_hook]

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([self.url])
            return 0
        except DownloadError as e:
            self.msg_queue.put(("log", f"[DownloadError] {str(e)}"))
            return 1
        except Exception as e:
            self.msg_queue.put(("log", f"[Exception] {str(e)}"))
            return 1

    async def _run_ytdlp_async(self, opts: Dict[str, Any], stage: str) -> int:
        self.stage = stage
        if self._auth_method:
            opts.update(self._auth_method.ydl_opts_subset)

        rc = await asyncio.to_thread(self._run_lib_ytdlp, opts)

        if self.cancel_requested:
            return rc

        last_log = "\n".join(list(self._recent_lines)[-50:])
        authy = _looks_like_auth_problem(last_log)
        self._last_run_authy = authy

        if rc != 0 and authy and not self._auth_method:
            self._auth_method = await self._select_auth_method(opts)
            if self._auth_method:
                self.stage = f"{stage} (retry: {self._auth_method.label})"
                opts.update(self._auth_method.ydl_opts_subset)
                rc = await asyncio.to_thread(self._run_lib_ytdlp, opts)

        return rc

    async def _select_auth_method(self, base_opts: Dict[str, Any]) -> Optional[AuthMethod]:
            def log_line(msg: str) -> None:
                self.msg_queue.put(("log", msg))

            def run_probe(opts_subset: Dict[str, Any]) -> bool:
                try:
                    test_opts = base_opts.copy()
                    test_opts.update(opts_subset)
                    test_opts['logger'] = YtDlpLogger(self.msg_queue)
                    with yt_dlp.YoutubeDL(test_opts) as ydl:
                        ydl.extract_info(self.url, download=False)
                    return True
                except Exception:
                    return False

            zen_dir_to_try: Optional[Path] = None
            zen_label = "Zen (root)"
            if self.zen_profile_path:
                p = Path(self.zen_profile_path).expanduser()
                if p.is_dir():
                    zen_dir_to_try = p
                    zen_label = "Zen (from --zen-profile-path)"
            else:
                root = _zen_profiles_root()
                if root.exists():
                    zen_dir_to_try = root
                    zen_label = "Zen (root)"

            if zen_dir_to_try and zen_dir_to_try.exists():
                log_line("Auth: Probing Zen (root)...")
                cookie_tuple = ('firefox', str(zen_dir_to_try), None, None)
                subset = {'cookiesfrombrowser': [cookie_tuple]}

                success = await asyncio.to_thread(run_probe, subset)
                if success:
                    log_line(f"Auth OK via: {zen_label}")
                    return AuthMethod(label=zen_label, ydl_opts_subset=subset, temp_files=[])

            # --- FIX: Run the heavy cookie search in a thread to avoid freezing UI ---
            candidates = await asyncio.to_thread(_rank_cookie_candidates, self.zen_profile_path)
            # -----------------------------------------------------------------------

            log_line("Auth: Probing browser cookies...")

            for cand in candidates:
                if self.cancel_requested: return None

                subset = {}
                temp_files = []

                if cand.kind == "zen" and cand.zen_db:
                    tmp_cookie = Path(tempfile.mkstemp(prefix="ytfrags_zen_", suffix=".cookies.txt")[1])
                    ok = _export_firefox_sqlite_to_netscape(cand.zen_db, tmp_cookie)
                    if ok:
                        subset = {'cookiefile': str(tmp_cookie)}
                        temp_files = [tmp_cookie]
                    else:
                        try: tmp_cookie.unlink()
                        except: pass
                        continue

                elif cand.kind == "browser" and cand.browser:
                    subset = {'cookiesfrombrowser': [(cand.browser, None, None, None)]}

                if not subset: continue

                log_line(f"Probing {cand.label}...")
                success = await asyncio.to_thread(run_probe, subset)
                if success:
                    log_line(f"Auth OK via: {cand.label}")
                    return AuthMethod(label=cand.label, ydl_opts_subset=subset, temp_files=temp_files)

            return None

    def _ffmpeg_remux_cmd(self, inp: Path, out: Path) -> List[str]:
            # Simple, non-interactive command
            # "copy" remuxes the video stream (fast, preserves AV1/VP9).
            # Audio is converted (trans-coded) to AAC for MP4 compatibility.
            return [
                self.app.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-i", str(inp),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", MP4_AAC_BITRATE, "-movflags", "+faststart", str(out),
            ]

    def _ffmpeg_audio_remux_cmd(self, inp: Path, out: Path) -> List[str]:
            # Helper to remux WebM audio (Opus) to Ogg Opus (.opus)
            return [
                self.app.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-i", str(inp),
                "-vn",           # Drop any video/thumbnail streams
                "-c:a", "copy",  # Copy the audio stream exactly (Lossless)
                str(out),
            ]

    async def _run_subprocess_simple(self, cmd: List[str]) -> int:
        self.msg_queue.put(("log", f"$ {shlex.join(cmd)}"))

        # Create subprocess and wait for it to finish using communicate()
        # This handles buffers automatically so no deadlocks happen
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )

        stdout_data, _ = await proc.communicate()

        if stdout_data:
            text = stdout_data.decode(errors="replace")
            if text.strip():
                # Dump ffmpeg logs/warnings to our log queue
                for line in text.splitlines():
                    self.msg_queue.put(("log", f"[FFmpeg] {line}"))

        return proc.returncode

    async def _run_pipeline(self) -> None:
            try:
                # Common options builder
                opts = build_ydl_opts(
                    ranges_norm=self.ranges_norm,
                    download_root=self.download_root,
                    profile="audio" if self.media_mode == "audio" else self.container_mode if self.media_mode == "video" else "mkv",
                    whole_video=self.whole_video,
                    preset=self.preset_mode
                )

                if self.media_mode == "audio":
                    opts = build_ydl_opts(
                        ranges_norm=self.ranges_norm,
                        download_root=self.download_root,
                        profile="audio",
                        whole_video=self.whole_video,
                        preset="quality"
                    )
                    rc = await self._run_ytdlp_async(opts, stage="Downloading (Audio)")

                    # --- NEW: Auto-Remux WebM(Opus) -> .opus ---
                    if rc == 0:
                        # Identify downloaded WebM files (which likely contain Opus)
                        # We copy the list to avoid modifying it while iterating if needed
                        webm_files = [p for p in self.dest_paths if p.exists() and p.suffix.lower() == ".webm"]

                        if webm_files:
                            self.stage = "Remuxing (Audio)"
                            self._render_status_line()

                            for i, inp in enumerate(webm_files, start=1):
                                if self.cancel_requested: break

                                out = inp.with_suffix(".opus")
                                self.msg_queue.put(("log", f"Remuxing {inp.name} to .opus..."))

                                # Use the new audio-specific remux command
                                cmd = self._ffmpeg_audio_remux_cmd(inp, out)
                                rc_remux = await self._run_subprocess_simple(cmd)

                                if rc_remux == 0:
                                    try: inp.unlink() # Delete the original .webm
                                    except: pass
                                else:
                                    self.msg_queue.put(("log", f"Warning: Failed to remux {inp.name}"))
                    # -------------------------------------------

                    self._finalize(rc)
                    return

                if self.container_mode == "mkv":
                    opts = build_ydl_opts(
                        ranges_norm=self.ranges_norm,
                        download_root=self.download_root,
                        profile="mkv",
                        whole_video=self.whole_video,
                        preset=self.preset_mode
                    )
                    rc = await self._run_ytdlp_async(opts, stage="Downloading")
                    self._finalize(rc)
                    return

                # MP4 Logic
                skip_direct = (self.mp4_method == "remux")

                # 1. Try Direct
                if not skip_direct:
                    opts_direct = build_ydl_opts(
                        ranges_norm=self.ranges_norm,
                        download_root=self.download_root,
                        profile="mp4_direct",
                        whole_video=self.whole_video,
                        preset=self.preset_mode
                    )
                    rc = await self._run_ytdlp_async(opts_direct, stage="Downloading (Direct)")

                    if self.cancel_requested:
                        self._finalize(rc)
                        return

                    # Check if we actually got an MP4
                    produced_mp4 = any(p.suffix.lower() == ".mp4" for p in self.dest_paths)

                    if rc == 0 and produced_mp4:
                        self._finalize(0)
                        return

                    if rc != 0 and self._last_run_authy:
                        self._finalize(rc)
                        return

                    self.msg_queue.put(("log", "MP4 Direct failed or skipped. Falling back to Remux."))

                # 2. Fallback / Forced Remux
                opts_src = build_ydl_opts(
                    ranges_norm=self.ranges_norm,
                    download_root=self.download_root,
                    profile="mkv_intermediate",
                    whole_video=self.whole_video,
                    preset=self.preset_mode
                )

                rc = await self._run_ytdlp_async(opts_src, stage="Downloading Source")

                if rc != 0 or self.cancel_requested:
                    self._finalize(rc)
                    return

                # 3. Remux Step (Manually via ffmpeg subprocess)
                self.stage = "Remuxing"

                # Look for files that match our request but aren't MP4 yet
                inputs = [p for p in self.dest_paths if p.exists() and p.suffix.lower() != ".mp4"]

                if not inputs:
                    self.msg_queue.put(("log", "No intermediate files found to remux."))
                    self._finalize(0)
                    return

                for i, inp in enumerate(inputs, start=1):
                    if self.cancel_requested: break
                    out = inp.with_suffix(".mp4")

                    self.progress.fragment_idx = min(i, self.total_fragments)
                    idx0 = self.progress.fragment_idx - 1
                    self.progress.seg_len_s = self.seg_lengths[idx0] if 0 <= idx0 < len(self.seg_lengths) else None
                    # No progress percentage updates during simple remux

                    self.msg_queue.put(("log", f"Remuxing to {out.name}..."))
                    ff_cmd = self._ffmpeg_remux_cmd(inp, out)

                    # Simple execution, no live parsing
                    rc_t = await self._run_subprocess_simple(ff_cmd)

                    if rc_t != 0:
                        self.msg_queue.put(("log", f"Remux failed for: {inp.name}"))
                        self._finalize(rc_t)
                        return

                    try: inp.unlink()
                    except: pass

                self._finalize(0)

            finally:
                self._cleanup_auth_temp_files()

    def _finalize(self, rc: int) -> None:
        self.done = True
        try:
            if self._ticker: self._ticker.stop()
        except Exception: pass
        try:
            if self._log_fp: self._log_fp.close()
        except Exception: pass

        if rc == 0:
            self.progress.percent = 100.0

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
            if not self.done:
                self.cancel_requested = True
                self._render_status_line()
                self.query_one("#events", RichLog).write("[Cancel requested]")
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
    .toggle_value { width: auto; padding: 0 1; content-align: center middle; border: round $panel; margin-right: 1; }
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

    BINDINGS = [("ctrl+q", "quit", "Quit")]

    def __init__(
        self,
        *,
        default_media_mode: str = "video",
        default_preset_mode: str = "quality",
        default_container_mode: str = "mkv",
        default_mp4_method: str = "direct",
        startup_note: str = "",
        zen_profile_path: Optional[str] = None,
        ffmpeg_path: str = "ffmpeg",
        default_theme: str = "tokyo-night",
    ) -> None:
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.state = AppState(
            media_mode=default_media_mode,
            preset_mode=default_preset_mode,
            container_mode=default_container_mode,
            mp4_method=default_mp4_method,
        )
        self.download_root = _get_videos_dir() / "yt-fragments"
        self.default_media_mode = default_media_mode
        self.default_preset_mode = default_preset_mode
        self.default_container_mode = default_container_mode
        self.default_mp4_method = default_mp4_method
        self.startup_note = startup_note.strip()
        self.zen_profile_path = zen_profile_path
        self._preferred_theme = _load_saved_theme() or default_theme

    def on_mount(self) -> None:
        try:
            self.theme = self._preferred_theme
        except Exception:
            pass
        self.push_screen(WizardScreen())

    async def shutdown(self) -> None:
        try:
            t = getattr(self, "theme", None)
            if t:
                _save_theme(str(t))
        finally:
            await super().shutdown()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ytfrags TUI: download time-range fragments from a video.")
    p.add_argument("--mp4", action="store_true", help="Default container = MP4 (video only).")
    p.add_argument("--audio", action="store_true", help="Default media = audio only.")
    p.add_argument("--compact", action="store_true", help="Default preset = Compact (video only).")
    p.add_argument("--remux", action="store_true", help="Default MP4 Method = Remux (.mp4 video only).")
    p.add_argument(
        "--zen-profile-path",
        type=str,
        default=None,
        help='Path to Zen profile DIR or cookies.sqlite. If omitted, uses default.',
    )
    p.add_argument(
        "--ffmpeg-path",
        type=str,
        default="ffmpeg",
        help='Path to ffmpeg binary. Default is "ffmpeg" (system PATH).',
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    default_media = "audio" if args.audio else "video"
    default_preset = "compact" if args.compact else "quality"
    default_container = "mp4" if args.mp4 else "mkv"
    default_method = "remux" if args.remux else "direct"

    note = ""
    if args.audio and (args.mp4 or args.remux):
        note = "--mp4/--remux ignored because --audio selects audio-only."
    if args.audio and args.compact:
        note = (note + " " if note else "") + "--compact ignored because --audio selects audio-only."

    YtFragsApp(
        default_media_mode=default_media,
        default_preset_mode=default_preset,
        default_container_mode=default_container,
        default_mp4_method=default_method,
        startup_note=note,
        zen_profile_path=args.zen_profile_path,
        ffmpeg_path=args.ffmpeg_path,
    ).run()
# --- END ytfrags_tui.py ---
