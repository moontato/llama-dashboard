import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone

import psutil

from .settings import get_settings
from .cmd_builder import build_argv
from .db import get_db


class ServerController:
    """Manage llama-server subprocess lifecycle."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def get_state(self) -> dict:
        """Get current server state."""
        db = get_db()
        row = db.execute(
            "SELECT pid, preset, log_path, started_at, port FROM server_state WHERE id = 1"
        ).fetchone()

        if row is None or row["pid"] is None:
            return {"status": "stopped"}

        pid = row["pid"]
        preset = row["preset"]
        log_path = row["log_path"]
        started_at = row["started_at"]
        port = row["port"]

        # Check if process is alive
        if psutil.pid_exists(pid):
            try:
                proc = psutil.Process(pid)
                uptime = time.time() - proc.create_time()
                cpu = proc.cpu_percent(interval=0.1)
                mem = proc.memory_info().rss
                return {
                    "status": "running",
                    "pid": pid,
                    "preset": preset,
                    "log_path": log_path,
                    "started_at": started_at,
                    "port": port,
                    "uptime": uptime,
                    "cpu_percent": cpu,
                    "rss": mem,
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Process is dead, clear stale state
        self._clear_state()
        return {"status": "stopped"}

    def start(self, preset_name: str) -> dict:
        """Start llama-server with the given preset."""
        with self._lock:
            state = self.get_state()
            settings = get_settings()

            if state["status"] == "running" and not settings.allow_multiple_servers:
                return {"error": "Server is already running. Stop it first or enable allow_multiple_servers."}

            try:
                argv = build_argv(preset_name)
            except Exception as e:
                return {"error": f"Failed to build command: {e}"}

            # Ensure log directory exists
            log_dir = settings.log_dir
            os.makedirs(log_dir, exist_ok=True)

            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            log_path = os.path.join(log_dir, f"llama-{preset_name}-{ts}.log")

            try:
                log_file = open(log_path, "w", buffering=1, encoding="utf-8")
            except Exception as e:
                return {"error": f"Cannot open log file: {e}"}

            proc = subprocess.Popen(
                argv,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            # Record state
            db = get_db()
            db.execute(
                "DELETE FROM server_state WHERE id = 1"
            )
            db.execute(
                "INSERT INTO server_state (id, pid, preset, log_path, started_at, port) VALUES (1, ?, ?, ?, ?, ?)",
                (proc.pid, preset_name, log_path, datetime.now(timezone.utc).isoformat(), None),
            )
            db.commit()

            # Determine port from argv
            port = self._extract_port(argv)
            db.execute(
                "UPDATE server_state SET port = ? WHERE id = 1",
                (port,),
            )
            db.commit()

            self._proc = proc

            # Poll for health
            health_ok = self._poll_health(port, timeout=30)
            if not health_ok:
                # Mark as failed
                db.execute(
                    "UPDATE server_state SET port = ? WHERE id = 1",
                    (port,),
                )
                db.commit()
                log_lines = self._read_last_lines(log_path, 40)
                return {
                    "error": "Server failed to start within 30s. Check logs.",
                    "log_lines": log_lines,
                    "log_path": log_path,
                }

            return {"status": "running", "pid": proc.pid, "preset": preset_name}

    def stop(self) -> dict:
        """Stop the running server."""
        with self._lock:
            state = self.get_state()
            if state["status"] != "running":
                return {"status": "stopped"}

            pid = state["pid"]
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

            # Wait up to 15s
            for _ in range(30):
                time.sleep(0.5)
                if not psutil.pid_exists(pid):
                    break
            else:
                # Force kill
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                time.sleep(1)

            self._clear_state()
            return {"status": "stopped"}

    def restart(self, preset_name: str) -> dict:
        """Stop then start the same preset."""
        stop_result = self.stop()
        if stop_result.get("error"):
            return stop_result
        time.sleep(1)
        return self.start(preset_name)

    def reconcile(self) -> None:
        """On boot, verify server_state matches reality."""
        db = get_db()
        row = db.execute(
            "SELECT pid, preset, log_path, started_at, port FROM server_state WHERE id = 1"
        ).fetchone()

        if row is None or row["pid"] is None:
            return

        pid = row["pid"]
        if not psutil.pid_exists(pid):
            self._clear_state()
            return

        try:
            proc = psutil.Process(pid)
            cmdline = proc.cmdline()
            if not cmdline or "llama-server" not in cmdline[0]:
                self._clear_state()
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self._clear_state()
            return

    def _clear_state(self) -> None:
        db = get_db()
        db.execute("DELETE FROM server_state WHERE id = 1")
        db.commit()
        self._proc = None

    def _extract_port(self, argv: list[str]) -> int | None:
        for i, arg in enumerate(argv):
            if arg in ("--port", "-p") and i + 1 < len(argv):
                try:
                    return int(argv[i + 1])
                except ValueError:
                    pass
        return None

    def _poll_health(self, port: int | None, timeout: int = 30) -> bool:
        import urllib.request

        if port is None:
            settings = get_settings()
            port = settings.llama_server_port

        url = f"http://127.0.0.1:{port}/health"
        start = time.time()
        while time.time() - start < timeout:
            try:
                req = urllib.request.Request(url, method="GET")
                resp = urllib.request.urlopen(req, timeout=2)
                if resp.status == 200:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _read_last_lines(self, path: str, n: int = 40) -> list[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return lines[-n:]
        except (OSError, IOError):
            return []


controller = ServerController()
