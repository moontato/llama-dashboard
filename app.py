from __future__ import annotations

import atexit
import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import selectors
import subprocess
import tempfile
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, jsonify, request, send_from_directory

from models_ini import Document, ModelsIniError, parse
from model_downloads import DownloadManager, DownloadError, DownloadConflict

_downloads = DownloadManager()
atexit.register(_downloads.close)

# ─────────────────────── Config ──────────────────────────────────
# Precedence: environment variable (key in upper case) > config.json
# > built-in default. The config file is read once at import time, so
# changes require a service restart. Set CONFIG_FILE to move it from
# the default <app dir>/config.json (see config.example.json).

def _config_file_path() -> str:
    return os.environ.get("CONFIG_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_config_file() -> Dict[str, Any]:
    """Parse the config file; a missing or broken file yields {} (the
    app must never fail to start because of configuration)."""
    path = _config_file_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"[llama-dashboard] ignoring config file {path}: {exc}")
        return {}
    if not isinstance(data, dict):
        print(f"[llama-dashboard] ignoring config file {path}: "
              "top level is not a JSON object")
        return {}
    return data


_FILE_CFG = _load_config_file()


def _cfg(key: str, default: Any = None) -> Any:
    """Config value: env var (KEY upper-cased) > config.json > default.
    Empty strings (env or file) count as unset, e.g. the disabled-by-
    default "llama_server_port": ""."""
    env = os.environ.get(key.upper())
    if env not in (None, ""):
        return env
    v = _FILE_CFG.get(key)
    if v not in (None, ""):
        return v
    return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(_cfg(key, default))
    except (TypeError, ValueError):
        print(f"[llama-dashboard] bad config {key.upper()}="
              f"{_cfg(key, default)!r}; using {default}")
        return int(default)


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(_cfg(key, default))
    except (TypeError, ValueError):
        print(f"[llama-dashboard] bad config {key.upper()}="
              f"{_cfg(key, default)!r}; using {default}")
        return float(default)


HISTORY_LEN = _cfg_int("history_len", 120)   # ~2 min rolling window at 1 Hz
BIND_HOST   = str(_cfg("bind_host", "127.0.0.1"))
PORT        = _cfg_int("port", 8080)
MODELS_INI_PATH = str(_cfg("models_ini_path",
                           "/mnt/ssd/llamacpp_models/models_ini"))

# ─────────────────────── Shared state ────────────────────────────
# One mutable dict so inner functions never need `global`.
_data: Dict[str, Any] = {
    "latest":    None,   # most recent payload dict
    "board":     None,   # board info dict (set once on connect)
    "connected": False,
    "history": {
        "ram_pct": deque(maxlen=HISTORY_LEN),
        "gpu_pct": deque(maxlen=HISTORY_LEN),
    },
}
_lock      = threading.Lock()
_new_data  = threading.Event()   # pulsed on every fresh jtop tick

# Restart endpoint state (mutable dict to avoid `global` keyword)
_restart_state   = {"last_ts": 0.0}
_restart_lock    = threading.Lock()

_write_lock      = threading.Lock()   # serializes models.ini read-modify-write
_git_lock        = threading.Lock()   # serializes git commit/pull/push

# One-level undo: raw text just before the last successful write. All
# access under _write_lock; cleared after use and on git commit/pull.
_last_raw        = {"text": None}
_fetch_state     = {"last_ts": 0.0}
_FETCH_MIN_S     = 60    # min seconds between ahead/behind refreshes
_FETCH_TIMEOUT_S = 10

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


@app.before_request
def validate_request():
    """Reject malformed API bodies and browser cross-site mutations."""
    if request.method != "POST":
        return None
    # Fetch Metadata works behind TLS-terminating proxies without trusting
    # caller-controlled forwarded host/proto headers. JSON mutations also
    # require application/json, which cross-site forms cannot send.
    if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
        return jsonify({"ok": False, "error": "cross-origin write forbidden"}), 403
    origin = request.headers.get("Origin")
    if origin:
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return jsonify({"ok": False, "error": "invalid Origin"}), 403
        if parsed.scheme not in ("http", "https") or parsed.netloc != request.host:
            return jsonify({"ok": False, "error": "cross-origin write forbidden"}), 403
    if request.path in ("/api/restart-llama", "/api/models/undo") and not request.data:
        return None
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    if "archived" in data and not isinstance(data["archived"], bool):
        return jsonify({"ok": False, "error": "archived must be a boolean"}), 400
    return None


def _valid_key(key: str) -> bool:
    return _single_line(key) and bool(re.fullmatch(r"[^=\s#;]+", key))


def _single_line(value: str) -> bool:
    return not any(c in value for c in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029\x00")


# ─────────────────────── Helpers ─────────────────────────────────

def _thresholds() -> Dict[str, float]:
    """Warn/crit thresholds (config keys *_warn_pct / *_crit_pct)."""
    return {
        "ram_warn":  _cfg_float("ram_warn_pct", 85.0),
        "ram_crit":  _cfg_float("ram_crit_pct", 93.0),
        "swap_warn": _cfg_float("swap_warn_pct", 25.0),
        "swap_crit": _cfg_float("swap_crit_pct", 50.0),
    }


def _severity(ram_pct: float, swap_pct: float) -> str:
    t = _thresholds()
    order = {"ok": 0, "warn": 1, "critical": 2}
    r = "critical" if ram_pct  >= t["ram_crit"]  else ("warn" if ram_pct  >= t["ram_warn"]  else "ok")
    s = "critical" if swap_pct >= t["swap_crit"] else ("warn" if swap_pct >= t["swap_warn"] else "ok")
    return max(r, s, key=lambda x: order[x])


def _build_payload(jetson: Any) -> Dict[str, Any]:
    mem = jetson.memory
    st  = jetson.stats

    ram_tot  = mem["RAM"]["tot"]
    ram_used = mem["RAM"]["used"]
    ram_free = mem["RAM"]["free"]
    ram_shrd = mem["RAM"]["shared"]
    ram_pct  = ram_used / ram_tot * 100.0 if ram_tot else 0.0

    swap_tot  = mem["SWAP"]["tot"]
    swap_used = mem["SWAP"]["used"]
    swap_pct  = swap_used / swap_tot * 100.0 if swap_tot else 0.0

    # Discover CPU cores dynamically; sort by core number.
    cpu_keys = sorted(
        [k for k in st if re.match(r"^CPU\d+$", k)],
        key=lambda k: int(k[3:]),
    )

    return {
        "ts": int(time.time()),
        "ram": {
            "used_gib":   round(ram_used  / 1_048_576, 2),
            "total_gib":  round(ram_tot   / 1_048_576, 2),
            "free_gib":   round(ram_free  / 1_048_576, 2),
            "shared_gib": round(ram_shrd  / 1_048_576, 2),
            "pct":        round(ram_pct, 1),
        },
        "swap": {
            "used_gib":  round(swap_used / 1_048_576, 2),
            "total_gib": round(swap_tot  / 1_048_576, 2),
            "pct":       round(swap_pct, 1),
            "is_zram":   True,
        },
        "gpu_pct": round(float(st.get("GPU", 0)), 1),
        "temp_c":  round(float(st.get("Temp tj", 0)), 1),
        # Power TOT is in mW; convert to W.
        "power_w": round(float(st.get("Power TOT", 0)) / 1000.0, 2),
        "cpu_pct": [st[k] for k in cpu_keys],
        "fan_pct": round(float(st.get("Fan pwmfan0", 0)), 1),
        "nvp":     st.get("nvp model", ""),
        "state":   _severity(ram_pct, swap_pct),
        # sent with every tick so the UI colours bars with the same
        # thresholds the server uses for `state`
        "thresholds": _thresholds(),
    }


def _jetpack_fallback() -> str:
    """JetPack version from the nvidia-jetpack apt meta-package.

    jetson-stats maps L4T->JetPack via an exact-match table, so a fresh
    L4T point release (e.g. 39.2.1) leaves that value empty.
    """
    try:
        out = subprocess.check_output(
            ["dpkg-query", "-W", "-f", "${Version}", "nvidia-jetpack"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        return out.strip().split("-b", 1)[0]
    except Exception:
        return ""


def _get_board(jetson: Any) -> Dict[str, str]:
    hw = jetson.board.get("hardware", {})
    pf = jetson.board.get("platform", {})
    jetpack = hw.get("Jetpack", "") or _jetpack_fallback()
    return {
        "model":   hw.get("Model", ""),
        "jetpack": jetpack,
        "python":  pf.get("Python", ""),
    }


# ─────────────────────── jtop background thread ──────────────────

def _jtop_thread() -> None:
    """Holds the single jtop() context for the process lifetime."""
    while True:
        try:
            from jtop import jtop  # noqa: PLC0415
            with jtop() as jetson:
                board = _get_board(jetson)
                with _lock:
                    _data["board"]     = board
                    _data["connected"] = True

                while jetson.ok():           # paced at ~1 Hz by jtop
                    payload = _build_payload(jetson)
                    with _lock:
                        _data["latest"] = payload
                        _data["history"]["ram_pct"].append(payload["ram"]["pct"])
                        _data["history"]["gpu_pct"].append(payload["gpu_pct"])
                    _new_data.set()

        except Exception as exc:
            print(f"[llama-dashboard] jtop error: {exc}; reconnecting in 5 s")

        finally:
            with _lock:
                _data["connected"] = False
            _new_data.set()   # wake SSE generators so they can send disconnected
            time.sleep(5)


# ─────────────────────── Flask routes ────────────────────────────

@app.route("/")
def index() -> Response:
    return send_from_directory("static", "index.html")


@app.route("/stream")
def stream() -> Response:
    def generate() -> Any:
        sent_board = False
        last_ts: Optional[int] = None

        while True:
            _new_data.wait(timeout=2.0)
            # Clear before reading so future events are not missed.
            _new_data.clear()

            with _lock:
                conn    = _data["connected"]
                payload = _data["latest"]
                board   = _data["board"]
                ram_h   = list(_data["history"]["ram_pct"])
                gpu_h   = list(_data["history"]["gpu_pct"])

            if not conn:
                yield "data: " + json.dumps({"disconnected": True}) + "\n\n"
                continue

            if not sent_board and board is not None:
                yield "data: " + json.dumps({"board": board}) + "\n\n"
                sent_board = True

            if payload is not None and payload["ts"] != last_ts:
                last_ts = payload["ts"]
                out = dict(payload)
                out["history"] = {"ram_pct": ram_h, "gpu_pct": gpu_h}
                out["llama"] = _probe_data["llama"]
                out["disk"] = _probe_data["disk"]
                yield "data: " + json.dumps(out) + "\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx/proxy buffering if ever added
        },
    )


# ── Systemd log streaming ────────────────────────────────────

# Each viewer owns its process; bound resource use without evicting peers.
_LOG_SLOTS = threading.BoundedSemaphore(4)
_LOG_HEARTBEAT_S = 2.0


def _log_events(proc):
    """Read a pipe without blocking disconnect detection during quiet logs."""
    pending = b""
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while True:
            if not selector.select(timeout=_LOG_HEARTBEAT_S):
                yield ": heartbeat\n\n"
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                if pending:
                    yield "data: " + pending.decode("utf-8", errors="replace").replace("\r", "") + "\n\n"
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield "data: " + line.decode("utf-8", errors="replace").replace("\r", "") + "\n\n"
            # A malformed/very long journal line must not grow memory forever.
            if len(pending) >= 65536:
                yield "data: " + pending.decode("utf-8", errors="replace").replace("\r", "") + "\n\n"
                pending = b""


def _journalctl_cmd(nlines: Optional[int] = None) -> List[str]:
    """Return the journalctl command to stream the llama-server logs.

    ``nlines`` (the initial tail) defaults to the configured
    ``log_tail_lines``; the log viewer can override it via ``?tail=N``.
    """
    if nlines is None:
        nlines = _cfg_int("log_tail_lines", 500)
    return [
        "sudo", "--non-interactive", "journalctl", "-f", "-u",
        str(_cfg("llama_server_service", "llama-server.service")),
        "--no-pager", "--no-hostname",
        "-n", str(nlines),
    ]


# ────────────────── slow probes: llama-server & disk ─────────────
# Probed on their own ~15 s ticker, never inside the 1 Hz payload
# builder, so subprocess/HTTP latency can't stall telemetry.
_PROBE_INTERVAL_S = _cfg_int("probe_interval_s", 15)
_probe_data: Dict[str, Any] = {"llama": None, "disk": None}


def _systemctl_is_active(unit: str) -> str:
    """``systemctl is-active`` → state word (active/inactive/failed/…)."""
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _systemctl_active_mono_us(unit: str) -> Optional[int]:
    """CLOCK_MONOTONIC µs stamp of when the unit entered active state."""
    try:
        r = subprocess.run(["systemctl", "show", unit, "--value",
                            "-p", "ActiveEnterTimestampMonotonic"],
                           capture_output=True, text=True, timeout=5)
        v = r.stdout.strip()
        return int(v) if v.isdigit() else None
    except Exception:
        return None


def _llama_model_info(host: str, port: int) -> Optional[Dict[str, Any]]:
    """Model name + slot status via the server's HTTP API.

    Router (models-preset) builds list EVERY preset from the ini on
    /v1/models with a per-entry ``status.value`` ("loaded" / "loading" /
    "unloaded") — the loaded model is the entry whose slot is live, not
    the first entry in the list. Stock builds list the loaded model with
    no status field. A reachable /v1/models is authoritative: an empty
    slot is a definite {"name": None, "status": "unloaded"}, not a
    reason to fall back to /props (which on a router reports the router,
    not a model). /props is tried only when /v1/models gave no usable
    answer (unreachable, or no "data" list); None then means unreachable
    (starting up, wrong port, …).
    """
    import urllib.request  # noqa: PLC0415
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/v1/models", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") if isinstance(data, dict) else None
        if items is not None:
            picked: Optional[tuple] = None    # (item, status)
            for item in items:
                if not isinstance(item, dict):
                    continue
                status = item.get("status")
                status = status.get("value") if isinstance(status, dict) else status
                if status == "loaded":
                    picked = (item, status)
                    break
                if picked is None and status == "loading":
                    picked = (item, status)
            if picked is None:
                # stock builds list the loaded model without a status
                for item in items:
                    if isinstance(item, dict) and item.get("status") is None:
                        picked = (item, None)
                        break
            if picked is not None:
                item, status = picked
                name = item.get("id") or item.get("name")
                return {"name": str(name) if name else None,
                        "status": str(status) if status else None}
            return {"name": None, "status": "unloaded"}
    except Exception:
        pass
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/props", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        name = data.get("name") if isinstance(data, dict) else None
        if name:
            return {"name": str(name), "status": None}
    except Exception:
        pass
    return None


def _probe_llama_server() -> Dict[str, Any]:
    unit = str(_cfg("llama_server_service", "llama-server.service"))
    state = _systemctl_is_active(unit)
    out: Dict[str, Any] = {"state": state, "uptime_s": None,
                           "model": None, "model_configured": False}
    if state != "active":
        return out
    mono = _systemctl_active_mono_us(unit)
    if mono is not None:
        up = time.monotonic() * 1_000_000 - mono
        out["uptime_s"] = max(0, int(up // 1_000_000))
    port = _cfg_int("llama_server_port", 0)
    if port:
        out["model_configured"] = True
        info = _llama_model_info(str(_cfg("llama_server_host", "127.0.0.1")), port)
        if info:
            out["model"] = info["name"]
            out["model_status"] = info["status"]
    return out


def _probe_disk() -> Optional[Dict[str, Any]]:
    """Usage of the filesystem holding the model dir (MODELS_DIR)."""
    path = _models_dir()
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if not usage.total:
        return None
    return {
        "path": path,
        "used_gib":  round(usage.used  / 1_073_741_824, 1),
        "total_gib": round(usage.total / 1_073_741_824, 1),
        "pct":       round(usage.used / usage.total * 100.0, 1),
    }


def _probe_thread() -> None:
    while True:
        try:
            _probe_data["llama"] = _probe_llama_server()
        except Exception as exc:
            print(f"[llama-dashboard] llama probe error: {exc}")
            _probe_data["llama"] = {"state": "unknown", "uptime_s": None,
                                    "model": None, "model_configured": False}
        try:
            _probe_data["disk"] = _probe_disk()
        except Exception as exc:
            print(f"[llama-dashboard] disk probe error: {exc}")
            _probe_data["disk"] = None
        time.sleep(_PROBE_INTERVAL_S)


@app.route("/healthz")
def healthz() -> Response:
    return "ok"


@app.route("/api/logs/llama-server")
def logs_llama_server() -> Response:
    """SSE endpoint that streams journalctl output for llama-server.service.

    Optional ``?tail=N`` overrides the initial tail length (clamped to
    10..10000); the log viewer's tail select uses it (#14).
    """
    tail = request.args.get("tail", type=int)
    if tail is not None:
        tail = max(10, min(tail, 10000))

    def generate() -> Any:
        if not _LOG_SLOTS.acquire(blocking=False):
            yield "data: [error] Too many log viewers; try again later\n\n"
            return
        proc = None
        try:
            proc = subprocess.Popen(
                _journalctl_cmd(tail), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0,
            )
            yield from _log_events(proc)
            if proc.wait(timeout=5) != 0:
                yield "data: [error] journalctl exited unsuccessfully\n\n"
        except (BrokenPipeError, GeneratorExit):
            pass
        except Exception as exc:
            yield "data: [error] " + str(exc).replace("\n", " ").replace("\r", " ") + "\n\n"
        finally:
            try:
                if proc is not None:
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    proc.stdout.close()
            finally:
                _LOG_SLOTS.release()

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/restart-llama", methods=["POST"])
def restart_llama() -> Response:
    cooldown = _cfg_int("restart_cooldown_s", 30)
    with _restart_lock:
        elapsed = time.time() - _restart_state["last_ts"]
        if elapsed < cooldown:
            remaining = int(cooldown - elapsed)
            return jsonify({"ok": False, "error": f"Cooldown: wait {remaining} s"}), 429
        _restart_state["last_ts"] = time.time()

    try:
        result = subprocess.run(
            ["sudo", "--non-interactive", "systemctl", "restart",
             str(_cfg("llama_server_service", "llama-server.service"))],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        err = result.stderr.strip() or result.stdout.strip() or "systemctl returned non-zero"
        return jsonify({"ok": False, "error": err}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "systemctl timed out after 15 s"}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ─────────────────── models.ini editing (git-backed) ─────────────

def _ini_file() -> str:
    """Path of the live models.ini (env overrides for tests)."""
    f = os.environ.get("MODELS_INI_FILE")
    if f:
        return f
    d = os.environ.get("MODELS_INI_DIR")
    if d:
        return os.path.join(d, "models.ini")
    return (MODELS_INI_PATH if os.path.isfile(MODELS_INI_PATH)
            else os.path.join(MODELS_INI_PATH, "models.ini"))


def _ini_dir() -> str:
    """Git worktree containing the ini file (resolves file-named layouts)."""
    d = os.environ.get("MODELS_INI_DIR")
    if d:
        return d
    fallback = os.path.dirname(_ini_file()) or "."
    seen = set()
    for cand in (fallback, MODELS_INI_PATH):
        if cand in seen:
            continue
        seen.add(cand)
        r = subprocess.run(["git", "-C", cand, "rev-parse",
                            "--is-inside-work-tree"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return cand
    return fallback


def _models_dir() -> str:
    """Root directory of the model files (where *.gguf live).

    Explicit ``models_dir`` (env or config) wins; otherwise derived from
    the ini location, which works for both layouts: a file ini at
    ``/x/models.ini`` → ``/x``, and the dir layout
    ``/x/models_ini[/models.ini]`` → ``/x``.
    """
    d = _cfg("models_dir", None)
    if d:
        return str(d)
    base = (os.environ.get("MODELS_INI_FILE")
            or os.environ.get("MODELS_INI_DIR")
            or MODELS_INI_PATH)
    return os.path.dirname(base) or "."


_models_gate = {"ok": False, "reason": "not checked"}


def _refresh_models_gate() -> None:
    """Write gate: refuse to modify a file that is unreadable or has no
    recognizable sections. (parse() keeps every byte verbatim by
    construction, so those are the only failure modes that can occur.)"""
    try:
        with open(_ini_file(), "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        _models_gate.update(ok=False, reason="cannot read models.ini")
        return
    if not parse(text).blocks:
        _models_gate.update(ok=False, reason="no sections found")
    else:
        _models_gate.update(ok=True, reason="")


def _load_doc():
    with open(_ini_file(), "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    return text, parse(text)


def _write_text(text: str) -> None:
    """Atomic in-place write of an exact text (shared by save/undo)."""
    f = _ini_file()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f) or ".",
                                prefix="." + os.path.basename(f) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, f)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_revision(text: str):
    """Compare under _write_lock; older API clients may omit If-Match."""
    expected = request.headers.get("If-Match")
    if expected is not None and expected.strip('"') != _revision(text):
        return jsonify({"ok": False, "error": "models.ini changed; reload before saving"}), 409
    return None


def _save_doc(doc) -> None:
    _write_text(doc.render())


def _valid_name(name: str) -> bool:
    return _single_line(name) and bool(re.fullmatch(r"[^\s\[\]=]+", name))


def _guard_write():
    """503 response when writes are gated, else None."""
    _refresh_models_gate()
    if not _models_gate["ok"]:
        return jsonify({"ok": False,
                        "error": "models.ini read-only: " + _models_gate["reason"]}), 503
    return None


def _mutate(fn):
    """Apply fn(doc) to fresh parse, self-check, write atomically.

    The lock makes concurrent edits sequential: without it, two
    simultaneous requests would both parse the old file and the second
    write would silently discard the first edit (lost update).
    A real change records the pre-image for one-level undo; no-ops
    don't consume the undo slot."""
    with _write_lock:
        try:
            _text, doc = _load_doc()
            conflict = _check_revision(_text)
            if conflict:
                return conflict
            fn(doc)
            new_text = doc.render()
            if new_text == _text:
                return jsonify({"ok": True, "revision": _revision(new_text)}), 200
            _write_text(new_text)
            _last_raw["text"] = _text
            return jsonify({"ok": True, "revision": _revision(new_text)}), 200
        except ModelsIniError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"edit failed: {exc}"}), 500


def _section_view(b) -> Dict[str, Any]:
    keys = []
    model = ""
    model_exists: Optional[bool] = None
    model_size_bytes: Optional[int] = None
    for k, v in b.keys():
        keys.append({"key": k, "value": v})
        if k == "model":
            model = v
    if model:
        try:
            st = os.stat(model)
            model_exists = True
            model_size_bytes = st.st_size
        except OSError:
            model_exists = False
    return {"name": b.name, "region": b.region, "archived": b.archived,
            "model": model, "model_exists": model_exists,
            "model_size_bytes": model_size_bytes, "keys": keys}


def _scan_model_files() -> Dict[str, List[Dict[str, Any]]]:
    """Recursively scan the model root for ``*.gguf`` files, categorized
    for the file pickers: mmproj by name, mtp by directory, rest gguf.
    Returns {category: [{path, rel, size_gib, mtime}, …]} sorted by path."""
    root = _models_dir()
    entries: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            if not fn.lower().endswith(".gguf"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue          # vanished or unreadable — skip silently
            rel = os.path.relpath(p, root)
            if fn.lower().startswith("mmproj") or "mmproj" in rel.lower().split(os.sep):
                cat = "mmproj"
            elif "mtp" in rel.lower().split(os.sep):
                cat = "mtp"
            else:
                cat = "gguf"
            entries.append({"path": p, "rel": rel,
                            "size_gib": round(st.st_size / 1_073_741_824, 2),
                            "mtime": int(st.st_mtime), "cat": cat})
    out: Dict[str, List[Dict[str, Any]]] = {"gguf": [], "mmproj": [], "mtp": []}
    for e in sorted(entries, key=lambda x: x["rel"].lower()):
        out[e.pop("cat")].append(e)
    return out


def _git(*args, timeout: int = 60):
    try:
        r = subprocess.run(["git", "-C", _ini_dir(), *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git not available"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"


def _refresh_remote_ref(branch: str) -> None:
    """Best-effort, throttled ``git fetch origin <branch>`` so the
    ahead/behind numbers reflect GitHub without a full pull.

    Skipped when a UI git op (commit/pull/push) is running, so we never
    contend for ref locks; failures are ignored and the numbers simply
    stay stale until the next attempt."""
    now = time.time()
    if now - _fetch_state["last_ts"] < _FETCH_MIN_S:
        return
    _fetch_state["last_ts"] = now
    if not _git_lock.acquire(blocking=False):
        return
    try:
        _git("fetch", "origin", branch, timeout=_FETCH_TIMEOUT_S)
    finally:
        _git_lock.release()


def _git_status() -> Dict[str, Any]:
    fname = os.path.basename(_ini_file())
    code, out, err = _git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return {"ok": False, "error": err or out}
    branch = out
    info = {"ok": True, "branch": branch}
    _refresh_remote_ref(branch)
    code, out, err = _git("status", "--porcelain", "--", fname)
    if code != 0:
        return {"ok": False, "error": err or out or "git status failed"}
    info["dirty"] = bool(out)
    code, out, _ = _git("rev-list", "--count", f"origin/{branch}..HEAD")
    info["ahead"] = int(out) if code == 0 else None
    code, out, _ = _git("rev-list", "--count", f"HEAD..origin/{branch}")
    info["behind"] = int(out) if code == 0 else None
    code, out, _ = _git("log", "-1", "--format=%h %s")
    info["last_commit"] = out or "(none)"
    return info


@app.route("/api/models", methods=["GET"])
def api_models() -> Response:
    _refresh_models_gate()
    try:
        with _write_lock:
            _text, doc = _load_doc()
            undo_available = _last_raw["text"] is not None
    except (OSError, UnicodeDecodeError):
        # file missing or unreadable: degraded payload carrying the
        # gate's reason, so the UI shows its read-only banner
        return jsonify({
            "ok": True,
            "writable": False,
            "undo_available": False,
            "write_reason": _models_gate["reason"],
            "models": [],
            "aliases": {},
            "git": _git_status(),
        })
    git = _git_status()
    return jsonify({
        "ok": True,
        "writable": _models_gate["ok"],
        "write_reason": _models_gate["reason"],
        "models": [_section_view(b) for b in doc.blocks],
        "aliases": doc.group_aliases(),
        "raw": _text,
        "revision": _revision(_text),
        "undo_available": undo_available,
        "git": git,
    })


@app.route("/api/models/files", methods=["GET"])
def api_models_files() -> Response:
    return jsonify({"ok": True, "root": _models_dir(), **_scan_model_files()})


@app.route("/api/models/downloads", methods=["GET", "POST"])
def api_model_downloads() -> Response:
    if request.method == "GET":
        return jsonify({"ok": True, "root": _models_dir(), "jobs": _downloads.snapshot()})
    data = request.get_json()
    try:
        job = _downloads.start(_models_dir(), data.get("url"),
                               data.get("subdirectory", ""), data.get("filename"))
        return jsonify({"ok": True, "job": job}), 202
    except DownloadConflict as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except DownloadError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError:
        return jsonify({"ok": False, "error": "Cannot write to the model destination; check directory permissions"}), 400


@app.route("/api/models/downloads/<job_id>/cancel", methods=["POST"])
def api_model_download_cancel(job_id: str) -> Response:
    job = _downloads.cancel(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "Download not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/models/raw", methods=["POST"])
def api_models_raw() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"ok": False, "error": "expected {text: str}"}), 400
    try:
        doc = parse(text)
        if not doc.blocks:
            return jsonify({"ok": False, "error": "no sections found"}), 400
        # Same lock + pre-image rule as _mutate: a raw save is an edit
        # too, and must not race a concurrent section edit.
        with _write_lock:
            current, _ = _load_doc()
            conflict = _check_revision(current)
            if conflict:
                return conflict
            if doc.render() != current:
                _save_doc(doc)
                _last_raw["text"] = current
        return jsonify({"ok": True, "revision": _revision(doc.render())}), 200
    except ModelsIniError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except (OSError, UnicodeDecodeError) as exc:
        return jsonify({"ok": False, "error": f"cannot read models.ini: {exc}"}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": f"save failed: {exc}"}), 500


@app.route("/api/models/undo", methods=["POST"])
def api_models_undo() -> Response:
    """Restore the pre-image of the last successful edit (one level).
    Consumed on success; 400 when nothing is recorded."""
    guard = _guard_write()
    if guard:
        return guard
    with _write_lock:
        preimage = _last_raw["text"]
        if preimage is None:
            return jsonify({"ok": False, "error": "nothing to undo"}), 400
        try:
            current, _ = _load_doc()
            conflict = _check_revision(current)
            if conflict:
                return conflict
            doc = parse(preimage)   # sanity: pre-image must still parse
            if not doc.blocks:
                return jsonify({"ok": False, "error": "no sections found"}), 400
            _save_doc(doc)
            _last_raw["text"] = None
            return jsonify({"ok": True}), 200
        except ModelsIniError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"undo failed: {exc}"}), 500


def _parse_proposed(text: str) -> Tuple[Optional["Document"], Optional[str]]:
    """Parse proposed raw text without writing. → (doc, None) | (None, err)."""
    try:
        doc = parse(text)
    except ModelsIniError as exc:
        return None, str(exc)
    if not doc.blocks:
        return None, "no sections found"
    return doc, None


@app.route("/api/models/raw/check", methods=["POST"])
def api_models_raw_check() -> Response:
    """Parse-only validation of raw text (no write, no lock needed)."""
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"ok": False, "error": "expected {text: str}"}), 400
    doc, err = _parse_proposed(text)
    if err is not None:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "sections": len(doc.blocks)})


@app.route("/api/models/raw/diff", methods=["POST"])
def api_models_raw_diff() -> Response:
    """Parse-check proposed raw text and return a unified diff vs the
    live file. Read-only — no write, no lock needed."""
    data = request.get_json(silent=True) or {}
    proposed = data.get("text")
    if not isinstance(proposed, str):
        return jsonify({"ok": False, "error": "expected {text: str}"}), 400
    try:
        current, _ = _load_doc()
    except (OSError, UnicodeDecodeError) as exc:
        return jsonify({"ok": False, "error": f"cannot read models.ini: {exc}"}), 500
    doc, err = _parse_proposed(proposed)
    if err is not None:
        return jsonify({"ok": False, "error": err}), 400
    diff = list(difflib.unified_diff(
        current.splitlines(), proposed.splitlines(),
        fromfile="models.ini (current)", tofile="models.ini (proposed)",
        lineterm=""))
    changed = proposed != current
    if changed and not diff:
        diff = ["Line endings or the final newline differ."]
    return jsonify({"ok": True, "sections": len(doc.blocks),
                    "revision": _revision(current),
                    "changed": changed, "diff": diff})


@app.route("/api/models/backup", methods=["GET"])
def api_models_backup() -> Response:
    """Download a byte-for-byte copy of the live models.ini."""
    f = _ini_file()
    if not os.path.isfile(f):
        return jsonify({"ok": False, "error": "models.ini not found"}), 404
    with open(f, "rb") as fh:
        data = fh.read()
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    return Response(
        data,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="models-{stamp}.ini"'},
    )


@app.route("/api/models/section/add", methods=["POST"])
def api_models_add() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    model = str(data.get("model", "")).strip()
    params = data.get("params") or {}
    if not isinstance(params, dict):
        return jsonify({"ok": False, "error": "params must be an object"}), 400
    region = str(data.get("region", "models")).strip()
    if region not in ("profiles", "models"):
        return jsonify({"ok": False, "error": "region must be profiles or models"}), 400
    if not _valid_name(name):
        return jsonify({"ok": False, "error": "invalid section name"}), 400
    if not model or not _single_line(model):
        return jsonify({"ok": False, "error": "model must be a nonempty single line"}), 400
    keys = [("model", model)]
    for k, v in params.items():
        k = str(k).strip()
        v = str(v)
        if not _valid_key(k) or not _single_line(v):
            return jsonify({"ok": False, "error": f"invalid parameter {k!r}"}), 400
        keys.append((k, v))
    return _mutate(lambda doc: doc.add_section(name, keys, region=region))


@app.route("/api/models/section/edit", methods=["POST"])
def api_models_edit() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    new_name = str(data.get("new_name", "")).strip()
    archived = bool(data.get("archived", False))
    if new_name and new_name != name and not _valid_name(new_name):
        return jsonify({"ok": False, "error": "invalid section name"}), 400
    remove_raw = data.get("remove") or []
    if not isinstance(remove_raw, list):
        return jsonify({"ok": False, "error": "remove must be a list"}), 400
    remove = [str(k) for k in remove_raw]
    set_raw = data.get("set") or {}
    if not isinstance(set_raw, dict):
        return jsonify({"ok": False, "error": "set must be an object"}), 400
    sets: Dict[str, str] = {}
    for k, v in set_raw.items():
        k, v = str(k).strip(), str(v)
        if not _valid_key(k) or not _single_line(v):
            return jsonify({"ok": False, "error": f"invalid key {k!r}"}), 400
        sets[k] = v
    order_raw = data.get("key_order") or []
    if not isinstance(order_raw, list):
        return jsonify({"ok": False, "error": "key_order must be a list"}), 400
    key_order = [str(k).strip() for k in order_raw]

    def fn(doc):
        cur = name
        if new_name and new_name != name:
            if doc.block(name, archived).region == "global":
                raise ModelsIniError(
                    "the global [*] section cannot be renamed")
            doc.rename_section(name, new_name, archived)
            cur = new_name
        for k in remove:
            if k not in sets:
                try:
                    doc.remove_key(cur, k, archived)
                except ModelsIniError:
                    pass        # key absence on remove is not an error
        for k, v in sets.items():
            doc.upsert_key(cur, k, v, archived)
        # reorder last, once the section holds its final set of keys
        if key_order:
            doc.reorder_keys(cur, key_order, archived)

    return _mutate(fn)


@app.route("/api/models/section/archive", methods=["POST"])
def api_models_archive() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    return _mutate(lambda doc: doc.archive_section(name))


@app.route("/api/models/section/restore", methods=["POST"])
def api_models_restore() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    return _mutate(lambda doc: doc.restore_section(name))


@app.route("/api/models/section/move", methods=["POST"])
def api_models_move() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    target = str(data.get("target", "")).strip()
    position = str(data.get("position", "")).strip()
    archived = bool(data.get("archived", False))
    if not _valid_name(name) or not _valid_name(target):
        return jsonify({"ok": False, "error": "invalid section name"}), 400
    if position not in ("before", "after"):
        return jsonify(
            {"ok": False, "error": "position must be 'before' or 'after'"}
        ), 400
    return _mutate(lambda doc: doc.move_section(
        name, target, position, archived=archived))


@app.route("/api/models/section/delete", methods=["POST"])
def api_models_delete() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    archived = bool(data.get("archived", False))

    def fn(doc):
        if doc.block(name, archived).region == "global":
            raise ModelsIniError(
                "the global [*] section cannot be deleted "
                "(remove its keys instead)")
        doc.delete_section(name, archived=archived)

    return _mutate(fn)


@app.route("/api/models/git", methods=["POST"])
def api_models_git() -> Response:
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("commit", "pull", "push"):
        return jsonify({"ok": False, "error": f"unknown action {action!r}"}), 400
    fname = os.path.basename(_ini_file())
    code, branch, _err = _git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return jsonify({"ok": False, "error": "not a git repository"}), 500

    # Always acquire Git before the file lock. Edits take only the file
    # lock, so staging/checkout and undo state form one transaction.
    if action == "commit":
        with _git_lock, _write_lock:
            msg = str(data.get("message", "")).strip() or "models.ini"
            code, out, err = _git("status", "--porcelain", "--", fname)
            if code != 0:
                return jsonify({"ok": False, "error": err or "git status failed"}), 500
            if not out:
                return jsonify({"ok": False, "error": "no changes to commit"}), 400
            for args in (["add", "--", fname], ["commit", "--only", "-m", msg, "--", fname]):
                code, _out, err = _git(*args)
                if code != 0:
                    return jsonify({"ok": False, "error": err or "git failed"}), 500
            code, out, _ = _git("log", "-1", "--format=%h %s")
            _last_raw["text"] = None    # committed: pre-image is history
            return jsonify({"ok": True, "commit": out})
    if action == "pull":
        with _git_lock, _write_lock:
            code, out, err = _git("pull", "--ff-only", "origin", branch,
                                  timeout=120)
            text = out or err
            if code != 0:
                return jsonify({"ok": False, "error": text, "output": text}), 500
            _last_raw["text"] = None    # upstream may have moved the file
            return jsonify({"ok": True, "output": text}), 200
    if action == "push":
        with _git_lock:
            code, out, err = _git("push", "origin", branch, timeout=120)
            text = out or err
            return (jsonify({"ok": True, "output": text}), 200) if code == 0 else \
                   (jsonify({"ok": False, "error": text, "output": text}), 500)


# ─────────────────────── Entry point ─────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=_jtop_thread, daemon=True)
    t.start()
    # Slow probes: llama-server status + model disk usage
    threading.Thread(target=_probe_thread, daemon=True).start()
    app.run(host=BIND_HOST, port=PORT, threaded=True)
