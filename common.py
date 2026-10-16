"""Shared helpers for talking to the player-cli daemon.

Everything that doesn't need to *start* the daemon talks to it directly
over its Unix control socket - the same newline-delimited JSON protocol
`player-cli` itself speaks (see `src/ipc.rs` in the player-cli repo) - so
there's no subprocess spawn per action/tick. The one exception is starting
playback when nothing is running yet: only the `player-cli` binary knows
how to detach and launch the background daemon (`player-cli play`), so
that one case still shells out to it.
"""

import json
import os
import socket
import subprocess
import threading
import time
import tomllib
from pathlib import Path

from loguru import logger as log

DEFAULT_BINARY = "player-cli"
BOOTSTRAP_TIMEOUT = 10  # seconds; starting the daemon for the first time can be slow
DEFAULT_BOOTSTRAP_VOLUME = 50  # applied only when the plugin starts a fresh daemon
SOCKET_TIMEOUT = 3.0
STATUS_CACHE_TTL = 0.75  # seconds; avoids a socket round-trip per action per tick

_status_cache_lock = threading.Lock()
_status_cache = {"time": 0.0, "data": None}


# --- Socket plumbing (mirrors src/ipc.rs::runtime_dir/socket_path) ---


def _runtime_dir() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        return Path(xdg) / "player-cli"
    return Path(f"/tmp/player-cli-{os.getuid()}")


def _socket_path() -> Path:
    return _runtime_dir() / "daemon.sock"


def send_request(request):
    """Sends one JSON request to the daemon's control socket. Blocking -
    call from a background thread, never from `on_key_down`/`on_tick` directly.

    `request` must already be in player-cli's wire format (see the
    `*_request` builders below). Returns `(kind, payload)`:
      - `(None, None)` if the daemon isn't running/reachable
      - `("Ok", None)`
      - `("Played" | "Action" | "Status", <dict>)`
      - `("Error", <message string>)`
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect(str(_socket_path()))
    except OSError:
        return None, None

    try:
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        line = sock.makefile("r", encoding="utf-8").readline()
    except OSError as e:
        log.warning(f"player-cli: socket I/O failed: {e}")
        return None, None
    finally:
        sock.close()

    if not line:
        return None, None
    try:
        response = json.loads(line)
    except json.JSONDecodeError:
        log.warning(f"player-cli: couldn't parse daemon response: {line!r}")
        return None, None

    if response == "Ok":
        return "Ok", None
    if isinstance(response, dict):
        for key in ("Played", "Action", "Status", "Error"):
            if key in response:
                return key, response[key]
    log.warning(f"player-cli: unrecognized daemon response: {response!r}")
    return None, None


def send_request_async(request, on_done=None):
    """Fire-and-forget version of `send_request` for simple commands
    (pause/resume/stop/volume) that never need the daemon-bootstrap path.
    `on_done(kind, payload)` runs on the background thread once a reply
    arrives (or immediately with `(None, None)` if nothing is running).
    """

    def task():
        kind, payload = send_request(request)
        if on_done is not None:
            try:
                on_done(kind, payload)
            except Exception as e:
                log.error(f"player-cli: on_done callback failed: {e}")

    threading.Thread(target=task, daemon=True).start()


# --- Request builders (must match the `Request` enum in src/ipc.rs) ---


def play_request(paths, replace=False, looping=False, fade_ms=0):
    return {
        "Play": {
            "paths": [str(p) for p in paths],
            "replace": bool(replace),
            "looping": bool(looping),
            "fade_ms": int(fade_ms),
        }
    }


def pause_request(tracks=()):
    return {"Pause": {"tracks": list(tracks)}}


def resume_request(tracks=()):
    return {"Resume": {"tracks": list(tracks)}}


def stop_request(tracks=()):
    return {"Stop": {"tracks": list(tracks)}}


def volume_request(level):
    return {"Volume": float(level)}


STATUS_REQUEST = "Status"


# --- Starting the daemon (the one thing the socket alone can't do) ---


def resolve_binary(plugin_base) -> str:
    settings = plugin_base.get_settings() or {}
    path = settings.get("binary_path", "").strip()
    return path or DEFAULT_BINARY


def bootstrap_play_sync(plugin_base, paths, replace=False, looping=False, fade_ms=0, volume=DEFAULT_BOOTSTRAP_VOLUME):
    """Runs `player-cli play <paths>`, which starts the background daemon
    if needed. Blocking - call from a background thread. Returns
    `(success, error_message_or_None)`.

    `volume` only has an effect the first time this starts a fresh daemon -
    the daemon has no volume of its own to default to otherwise, so this is
    the one place that gets to set it. A daemon that's already running
    (the common case) keeps whatever volume it's already at; pass
    `volume=None` to skip setting it even on a fresh start.
    """
    binary = resolve_binary(plugin_base)
    args = [binary, "play", *[str(p) for p in paths]]
    if replace:
        args.append("--replace")
    if looping:
        args.append("--loop")
    if fade_ms:
        args.extend(["--fade", str(int(fade_ms))])
    if volume is not None:
        args.extend(["--volume", str(int(volume))])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=BOOTSTRAP_TIMEOUT)
        if result.returncode == 0:
            return True, None
        return False, (result.stderr.strip() or result.stdout.strip())
    except FileNotFoundError:
        return False, f"'{binary}' not found (set the full path in the plugin settings)"
    except subprocess.TimeoutExpired:
        return False, f"'{binary}' timed out after {BOOTSTRAP_TIMEOUT}s"
    except Exception as e:  # defensive: never let a key press crash the deck
        log.error(f"player-cli: failed to start daemon: {e}")
        return False, str(e)


# --- Status (polled by every action's on_tick) ---


def get_status(force: bool = False):
    """Returns the daemon's current status dict (`{"volume": .., "tracks": [...]}`)
    or `None` if it isn't running/reachable. Cached briefly so several
    actions ticking at once share one socket round-trip; `force=True`
    bypasses the cache right after this plugin itself changed playback
    state, for immediate feedback.
    """
    now = time.monotonic()
    if not force:
        with _status_cache_lock:
            if _status_cache["data"] is not None and now - _status_cache["time"] < STATUS_CACHE_TTL:
                return _status_cache["data"]

    kind, payload = send_request(STATUS_REQUEST)
    data = payload if kind == "Status" else None

    with _status_cache_lock:
        _status_cache["time"] = now
        _status_cache["data"] = data
    return data


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "player-cli" / "config.toml"


def library_dirs():
    """Reads the library directories from player-cli's config file - the
    same file `player-cli library add` writes to, so directories added via
    either the CLI or (if ever added) a plugin setting are shared.
    """
    try:
        with open(_config_path(), "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return []
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning(f"player-cli: couldn't read config: {e}")
        return []
    return [Path(d) for d in data.get("library", []) if isinstance(d, str)]


AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
    ".wma",
    ".aiff",
    ".aif",
    ".alac",
    ".caf",
}


def list_library_tracks():
    """Recursively lists audio files under each configured library
    directory, for UI pickers (e.g. the Play/Stop Track dropdown).

    Returns `(label, absolute_path)` tuples sorted by label, where `label`
    is the file's path relative to its library root, extension stripped
    and each segment capitalized (e.g. `ambience/rain-thunder.opus` becomes
    `Ambience/Rain-Thunder`) - so files that share a filename in different
    subfolders stay distinguishable, but the label itself stays clean.
    `absolute_path` keeps its exact original casing and extension, since
    that's what's actually used to play the file.
    """
    results = []
    seen = set()
    for directory in library_dirs():
        if not directory.is_dir():
            continue
        try:
            paths = sorted(directory.rglob("*"))
        except OSError as e:
            log.warning(f"player-cli: couldn't scan library directory {directory}: {e}")
            continue
        for path in paths:
            if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
                continue
            try:
                resolved = str(path.resolve())
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                label = str(path.relative_to(directory).with_suffix(""))
            except ValueError:
                label = path.stem
            results.append((label.title(), resolved))
    results.sort(key=lambda item: item[0].lower())
    return results


def _find_by_name(directory: Path, name: str):
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    subdirs = []
    for entry in entries:
        if entry.is_dir():
            subdirs.append(entry)
        elif entry.name == name:
            return entry
    for subdir in subdirs:
        found = _find_by_name(subdir, name)
        if found is not None:
            return found
    return None


def resolve_audio_path(path: str):
    """Resolves `path` the same way `player-cli play` does, trying, in
    order: the path as given (after expanding `~`, since GTK entries don't
    do shell-style expansion); that path joined onto each library
    directory (see `library_dirs()`); and a bare filename match searched
    recursively under each library directory.

    Returns `(resolved_path, None)` or `(None, error_message)`.
    """
    expanded = os.path.expanduser(path)
    direct = os.path.realpath(expanded)
    if os.path.isfile(direct):
        return direct, None

    dirs = library_dirs()
    for directory in dirs:
        candidate = os.path.realpath(directory / expanded)
        if os.path.isfile(candidate):
            return candidate, None

    name = os.path.basename(expanded)
    if name:
        for directory in dirs:
            found = _find_by_name(directory, name)
            if found is not None:
                return str(found.resolve()), None

    if not dirs:
        return None, f"no such file: {direct} (no library directories configured - see `player-cli library add`)"
    return None, "no such file: {} (also searched your library: {})".format(
        direct, ", ".join(str(d) for d in dirs)
    )


# --- Spotify (via `player-cli spotify`, itself a `playerctl` wrapper) ---
#
# No daemon/socket involved here - `player-cli spotify` talks to Spotify
# directly over MPRIS via playerctl, so every call below is a subprocess
# spawn rather than a socket round-trip.

SPOTIFY_TIMEOUT = 5  # seconds; local D-Bus round-trips via playerctl, should be fast

_spotify_status_cache_lock = threading.Lock()
_spotify_status_cache = {"time": 0.0, "data": None}


def _spotify_command(plugin_base, *args):
    """Runs `player-cli spotify <args>`. Blocking - call from a background
    thread. Returns `(success, stdout_or_error_message)`.
    """
    binary = resolve_binary(plugin_base)
    try:
        result = subprocess.run(
            [binary, "spotify", *args],
            capture_output=True,
            text=True,
            timeout=SPOTIFY_TIMEOUT,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, (result.stderr.strip() or result.stdout.strip())
    except FileNotFoundError:
        return False, f"'{binary}' not found (set the full path in the plugin settings)"
    except subprocess.TimeoutExpired:
        return False, f"'{binary}' timed out after {SPOTIFY_TIMEOUT}s"
    except Exception as e:  # defensive: never let a key press crash the deck
        log.error(f"player-cli: spotify command failed: {e}")
        return False, str(e)


def spotify_play(plugin_base):
    return _spotify_command(plugin_base, "play")


def spotify_pause(plugin_base):
    return _spotify_command(plugin_base, "pause")


def spotify_play_pause(plugin_base):
    return _spotify_command(plugin_base, "play-pause")


def spotify_next(plugin_base):
    return _spotify_command(plugin_base, "next")


def spotify_previous(plugin_base):
    return _spotify_command(plugin_base, "previous")


def spotify_stop(plugin_base):
    return _spotify_command(plugin_base, "stop")


def spotify_open(plugin_base, uri: str):
    """Starts playing a specific track/playlist/album/etc. `uri` can be a
    `spotify:` URI, an open.spotify.com share link, or a `<kind>:<id>`
    shorthand - see `player-cli spotify open --help`.
    """
    return _spotify_command(plugin_base, "open", uri)


def spotify_adjust_volume(plugin_base, delta: int):
    """Relative volume change, e.g. `10` or `-10`. playerctl supports this
    directly, so unlike the daemon's own (absolute-only) volume control
    there's no need to read the current value first.
    """
    value = f"+{delta}" if delta >= 0 else str(delta)
    return _spotify_command(plugin_base, "volume", value)


def spotify_status(plugin_base, force: bool = False):
    """Returns Spotify's current status (`{"status", "artist", "title",
    "volume"}`, `volume` possibly `None`) or `None` if Spotify isn't
    running. Cached briefly, same idea as `get_status` - but unlike that
    one, the cache also covers the "nothing running" result, since each
    lookup here is a subprocess spawn rather than a cheap socket connect.
    """
    now = time.monotonic()
    if not force:
        with _spotify_status_cache_lock:
            if now - _spotify_status_cache["time"] < STATUS_CACHE_TTL:
                return _spotify_status_cache["data"]

    success, output = _spotify_command(plugin_base, "status", "--json")
    data = None
    if success and output:
        try:
            parsed = json.loads(output)
            if parsed.get("artist"):
                data = parsed
        except json.JSONDecodeError:
            log.warning(f"player-cli: couldn't parse spotify status: {output!r}")

    with _spotify_status_cache_lock:
        _spotify_status_cache["time"] = now
        _spotify_status_cache["data"] = data
    return data


def find_track(status, path: str):
    """Finds the track in `status` whose path matches `path`, if any.

    Resolves `path` the same way `resolve_audio_path` does (including
    library lookups) before comparing, so a track configured by bare
    filename is still recognized as already playing - otherwise toggle
    actions would never see it as running and would try to start a
    duplicate instead of stopping/pausing the existing one.
    """
    if not status or not path:
        return None
    resolved, _ = resolve_audio_path(path)
    if resolved is None:
        try:
            resolved = os.path.realpath(os.path.expanduser(path))
        except OSError:
            return None
    target = resolved
    for track in status.get("tracks", []):
        try:
            if os.path.realpath(track["path"]) == target:
                return track
        except OSError:
            continue
    return None


def shorten(text, length=18):
    if not text:
        return ""
    text = str(text)
    if len(text) > length:
        return text[: length - 1] + "…"
    return text
