import os
import time
import threading
from pathlib import Path


def get_log_files(log_dir: str) -> list[str]:
    """Get list of log files in log_dir."""
    dir_path = Path(log_dir)
    if not dir_path.exists():
        return []
    return sorted(
        [f.name for f in dir_path.iterdir() if f.is_file() and f.name.startswith("llama-")],
        reverse=True,
    )


def get_current_log(log_dir: str) -> str | None:
    """Get the most recent log file (current server log)."""
    files = get_log_files(log_dir)
    return files[0] if files else None


def tail_sse(log_path: str, follow: bool = True):
    """Generator for SSE log tailing."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            if not follow:
                # Read to EOF
                lines = f.readlines()
                for line in lines[-2000:]:
                    yield f"data: {line.rstrip(chr(10))}\n\n"
                yield "data: [EOF]\n\n"
                return

            # Seek to end
            f.seek(0, 2)
            line_count = 0
            while True:
                line = f.readline()
                if line:
                    yield f"data: {line.rstrip(chr(10))}\n\n"
                    line_count += 1
                    if line_count > 2000:
                        yield "data: [max lines reached]\n\n"
                        break
                else:
                    time.sleep(0.25)
    except (OSError, IOError):
        yield "data: [log file not available]\n\n"
