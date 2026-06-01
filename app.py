from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, send_from_directory

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
MODELS_INI_PATH  = "/ssd/llamacpp_models/models_ini"

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


def _get_board(jetson: Any) -> Dict[str, str]:
    hw = jetson.board.get("hardware", {})
    pf = jetson.board.get("platform", {})
    return {
        "model":   hw.get("Model", ""),
        "jetpack": hw.get("Jetpack", ""),
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
            print(f"[jtop-web] jtop error: {exc}; reconnecting in 5 s")

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


# ─────────────────────── Entry point ─────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=_jtop_thread, daemon=True)
    t.start()
    app.run(host=BIND_HOST, port=PORT, threaded=True)
