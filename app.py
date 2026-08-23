from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, request, send_from_directory

from models_ini import ModelsIniError, parse

# ─────────────────────── Thresholds (tune here) ──────────────────
RAM_WARN_PCT  = 85.0
RAM_CRIT_PCT  = 93.0
SWAP_WARN_PCT = 25.0
SWAP_CRIT_PCT = 50.0

# ─────────────────────── Config ──────────────────────────────────
HISTORY_LEN        = 120   # ~2 min rolling window at 1 Hz
BIND_HOST          = "127.0.0.1"
PORT               = 8080
RESTART_COOLDOWN_S = 30    # minimum seconds between llama-server restarts

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

_gitpull_state   = {"last_ts": 0.0}
_gitpull_lock    = threading.Lock()
MODELS_INI_PATH  = "/mnt/ssd/llamacpp_models/models_ini"

app = Flask(__name__, static_folder="static")


# ─────────────────────── Helpers ─────────────────────────────────

def _severity(ram_pct: float, swap_pct: float) -> str:
    order = {"ok": 0, "warn": 1, "critical": 2}
    r = "critical" if ram_pct  >= RAM_CRIT_PCT  else ("warn" if ram_pct  >= RAM_WARN_PCT  else "ok")
    s = "critical" if swap_pct >= SWAP_CRIT_PCT else ("warn" if swap_pct >= SWAP_WARN_PCT else "ok")
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
                yield "data: " + json.dumps(out) + "\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx/proxy buffering if ever added
        },
    )


@app.route("/healthz")
def healthz() -> Response:
    return "ok"


@app.route("/api/restart-llama", methods=["POST"])
def restart_llama() -> Response:
    with _restart_lock:
        elapsed = time.time() - _restart_state["last_ts"]
        if elapsed < RESTART_COOLDOWN_S:
            remaining = int(RESTART_COOLDOWN_S - elapsed)
            return jsonify({"ok": False, "error": f"Cooldown: wait {remaining} s"}), 429
        _restart_state["last_ts"] = time.time()

    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "llama-server.service"],
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


@app.route("/api/git-pull-models", methods=["POST"])
def git_pull_models() -> Response:
    with _gitpull_lock:
        elapsed = time.time() - _gitpull_state["last_ts"]
        if elapsed < RESTART_COOLDOWN_S:
            remaining = int(RESTART_COOLDOWN_S - elapsed)
            return jsonify({"ok": False, "error": f"Cooldown: wait {remaining} s"}), 429
        _gitpull_state["last_ts"] = time.time()

    try:
        result = subprocess.run(
            ["sudo", "git", "-C", MODELS_INI_PATH, "pull"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode == 0:
            return jsonify({"ok": True, "output": output})
        return jsonify({"ok": False, "error": output or "git pull returned non-zero"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "git pull timed out after 60 s"}), 500
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


_models_gate = {"ok": False, "reason": "not checked"}


def _refresh_models_gate() -> None:
    """Byte-verify the live file round-trips through our parser."""
    try:
        with open(_ini_file(), "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        _models_gate.update(ok=False, reason="cannot read models.ini")
        return
    try:
        doc = parse(text)
        if doc.render() != text:
            _models_gate.update(ok=False, reason="byte round-trip failed")
        elif not doc.blocks:
            _models_gate.update(ok=False, reason="no sections found")
        else:
            _models_gate.update(ok=True, reason="")
    except Exception:
        _models_gate.update(ok=False, reason="parse error")


def _load_doc():
    with open(_ini_file(), "r", encoding="utf-8") as fh:
        text = fh.read()
    return text, parse(text)


def _save_doc(doc) -> None:
    text = doc.render()
    if parse(text).render() != text:
        raise ModelsIniError("internal render check failed; file not written")
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


def _valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[^\s\[\]=]+", name))


def _guard_write():
    """503 response when writes are gated, else None."""
    _refresh_models_gate()
    if not _models_gate["ok"]:
        return jsonify({"ok": False,
                        "error": "models.ini read-only: " + _models_gate["reason"]}), 503
    return None


def _mutate(fn):
    """Apply fn(doc) to fresh parse, self-check, write atomically."""
    try:
        _text, doc = _load_doc()
        fn(doc)
        _save_doc(doc)
        return jsonify({"ok": True}), 200
    except ModelsIniError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"edit failed: {exc}"}), 500


def _section_view(b) -> Dict[str, Any]:
    keys = []
    model = ""
    for k, v in b.keys():
        keys.append({"key": k, "value": v})
        if k == "model":
            model = v
    return {"name": b.name, "region": b.region, "archived": b.archived,
            "model": model, "keys": keys}


def _git(*args, timeout: int = 60):
    try:
        r = subprocess.run(["git", "-C", _ini_dir(), *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git not available"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"


def _git_status() -> Dict[str, Any]:
    fname = os.path.basename(_ini_file())
    code, out, err = _git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return {"ok": False, "error": err or out}
    info = {"ok": True, "branch": out}
    code, out, _ = _git("status", "--porcelain", "--", fname)
    info["dirty"] = bool(out)
    code, out, _ = _git("rev-list", "--count", f"origin/{out}..HEAD")
    info["ahead"] = int(out) if code == 0 else None
    code, out, _ = _git("rev-list", "--count", f"HEAD..origin/{out}")
    info["behind"] = int(out) if code == 0 else None
    code, out, _ = _git("log", "-1", "--format=%h %s")
    info["last_commit"] = out or "(none)"
    return info


@app.route("/api/models", methods=["GET"])
def api_models() -> Response:
    _refresh_models_gate()
    try:
        _text, doc = _load_doc()
        git = _git_status()
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "models.ini not found"}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "writable": _models_gate["ok"],
        "write_reason": _models_gate["reason"],
        "models": [_section_view(b) for b in doc.blocks],
        "aliases": doc.group_aliases(),
        "git": git,
    })


@app.route("/api/models/section/add", methods=["POST"])
def api_models_add() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    model = str(data.get("model", "")).strip()
    params = data.get("params") or {}
    region = str(data.get("region", "individual")).strip()
    if region not in ("profiles", "individual"):
        return jsonify({"ok": False, "error": "region must be profiles or individual"}), 400
    if not _valid_name(name):
        return jsonify({"ok": False, "error": "invalid section name"}), 400
    if not model:
        return jsonify({"ok": False, "error": "model is required"}), 400
    keys = [("model", model)]
    for k, v in params.items():
        k = str(k).strip()
        v = str(v)
        if not k or "=" in k or any(c in v for c in "\r\n"):
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
    remove = [str(k) for k in (data.get("remove") or [])]
    sets: Dict[str, str] = {}
    for k, v in (data.get("set") or {}).items():
        k, v = str(k).strip(), str(v)
        if not k or "=" in k or any(c in v for c in "\r\n"):
            return jsonify({"ok": False, "error": f"invalid key {k!r}"}), 400
        sets[k] = v

    def fn(doc):
        cur = name
        if new_name and new_name != name:
            doc.rename_section(name, new_name)
            cur = new_name
        for k in remove:
            if k not in sets:
                try:
                    doc.remove_key(cur, k)
                except ModelsIniError:
                    pass        # key absence on remove is not an error
        for k, v in sets.items():
            doc.upsert_key(cur, k, v)

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


@app.route("/api/models/section/delete", methods=["POST"])
def api_models_delete() -> Response:
    guard = _guard_write()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    archived = bool(data.get("archived", False))
    return _mutate(lambda doc: doc.delete_section(name, archived=archived))


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

    if action == "commit":
        msg = str(data.get("message", "")).strip() or "models.ini"
        code, out, _ = _git("status", "--porcelain", "--", fname)
        if not out:
            return jsonify({"ok": False, "error": "no changes to commit"}), 400
        for args in (["add", fname], ["commit", "-m", msg]):
            code, _out, err = _git(*args)
            if code != 0:
                return jsonify({"ok": False, "error": err or "git failed"}), 500
        code, out, _ = _git("log", "-1", "--format=%h %s")
        return jsonify({"ok": True, "commit": out})
    if action == "pull":
        code, out, err = _git("pull", "--ff-only", "origin", branch, timeout=120)
        text = out or err
        return (jsonify({"ok": True, "output": text}), 200) if code == 0 else \
               (jsonify({"ok": False, "error": text, "output": text}), 500)
    if action == "push":
        code, out, err = _git("push", "origin", branch, timeout=120)
        text = out or err
        return (jsonify({"ok": True, "output": text}), 200) if code == 0 else \
               (jsonify({"ok": False, "error": text, "output": text}), 500)


# ─────────────────────── Entry point ─────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=_jtop_thread, daemon=True)
    t.start()
    app.run(host=BIND_HOST, port=PORT, threaded=True)
