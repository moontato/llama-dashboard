from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .db import get_db


@dataclass
class DownloadItem:
    id: int
    repo_id: str
    filename: str
    dest_path: str
    bytes_done: int = 0
    bytes_total: int = 0
    status: str = "queued"
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class ProgressTracker:
    """tqdm-compatible progress tracker for huggingface_hub."""

    def __init__(self, download_id: int):
        self.download_id = download_id
        self._lock = threading.Lock()

    def update(self, n: int = 1, total: int | None = None):
        with self._lock:
            db = get_db()
            if total is not None:
                db.execute(
                    "UPDATE downloads SET bytes_done = bytes_done + ?, bytes_total = ? WHERE id = ?",
                    (n, total, self.download_id),
                )
            else:
                db.execute(
                    "UPDATE downloads SET bytes_done = bytes_done + ? WHERE id = ?",
                    (n, self.download_id),
                )
            db.commit()


class DownloadWorker:
    """Manage HuggingFace downloads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._downloads: dict[int, DownloadItem] = {}

    def _normalize_source(self, source: str) -> tuple[str, str] | None:
        """Parse source into (repo_id, filename)."""
        source = source.strip()

        # hf://user/repo/filename.gguf
        m = re.match(r"^hf://([^/]+)/([^/]+)/(.+)$", source)
        if m:
            return m.group(1) + "/" + m.group(2), m.group(3)

        # https://huggingface.co/user/repo/resolve/main/filename.gguf
        m = re.match(r"^https://huggingface\.co/([^/]+)/([^/]+)/resolve/.+/(.+)$", source)
        if m:
            return m.group(1) + "/" + m.group(2), m.group(3)

        # https://huggingface.co/user/repo/blob/main/filename.gguf
        m = re.match(r"^https://huggingface\.co/([^/]+)/([^/]+)/blob/.+/(.+)$", source)
        if m:
            return m.group(1) + "/" + m.group(2), m.group(3)

        # user/repo/filename.gguf
        m = re.match(r"^([^/]+)/([^/]+)/([^/]+)$", source)
        if m:
            return m.group(1) + "/" + m.group(2), m.group(3)

        # user/repo (list files)
        m = re.match(r"^([^/]+)/([^/]+)$", source)
        if m:
            return m.group(1) + "/" + m.group(2), None

        return None

    def queue_download(self, source: str, dest: str) -> dict:
        """Queue a new download."""
        parsed = self._normalize_source(source)
        if parsed is None:
            return {"error": "Invalid HuggingFace source. Use user/repo, user/repo/file.gguf, or a hf.co URL."}

        repo_id, filename = parsed

        if filename is None:
            # List files in repo - return list for user to pick
            return {"list_files": True, "repo_id": repo_id, "source": source, "dest": dest}

        # Create download row
        db = get_db()
        cursor = db.execute(
            """INSERT INTO downloads (repo_id, filename, dest_path, status, started_at)
               VALUES (?, ?, ?, 'queued', ?)""",
            (repo_id, filename, dest, datetime.now(timezone.utc).isoformat()),
        )
        download_id = cursor.lastrowid
        db.commit()

        cancel = threading.Event()
        item = DownloadItem(
            id=download_id,
            repo_id=repo_id,
            filename=filename,
            dest_path=dest,
            cancel_event=cancel,
        )

        with self._lock:
            self._downloads[download_id] = item
            t = threading.Thread(
                target=self._do_download,
                args=(item,),
                daemon=True,
                name=f"hf-download-{download_id}",
            )
            self._threads[download_id] = t
            t.start()

        return {"id": download_id, "status": "queued"}

    def _do_download(self, item: DownloadItem) -> None:
        """Execute a single download."""
        from huggingface_hub import hf_hub_download

        db = get_db()
        tracker = ProgressTracker(item.id)

        try:
            db.execute(
                "UPDATE downloads SET status = 'running', bytes_done = 0 WHERE id = ?",
                (item.id,),
            )
            db.commit()

            # Get total size from repo
            from huggingface_hub import HfApi
            api = HfApi()
            try:
                info = api.repo_info(item.repo_id, filename=item.filename)
                total = info.size or 0
                if total > 0:
                    db.execute(
                        "UPDATE downloads SET bytes_total = ? WHERE id = ?",
                        (total, item.id),
                    )
                    db.commit()
            except Exception:
                pass

            tracker.update(0, total)

            dest_dir = os.path.dirname(item.dest_path)
            os.makedirs(dest_dir, exist_ok=True)

            hf_hub_download(
                repo_id=item.repo_id,
                filename=item.filename,
                local_dir=dest_dir,
                local_dir_use_symlinks=False,
                token=None,  # settings.hf_token is not accessible here directly
                force_download=False,
                tqdm_class=lambda *args, **kwargs: tracker,
            )

            # Verify file exists
            expected = os.path.join(dest_dir, item.filename)
            if os.path.exists(expected):
                db.execute(
                    """UPDATE downloads SET status = 'done', bytes_done = ?,
                       bytes_total = ?, finished_at = ? WHERE id = ?""",
                    (os.path.getsize(expected), total, datetime.now(timezone.utc).isoformat(), item.id),
                )
                db.commit()
            else:
                raise RuntimeError("Downloaded file not found")

        except Exception as e:
            error_msg = str(e)
            db.execute(
                "UPDATE downloads SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                ("failed", error_msg, datetime.now(timezone.utc).isoformat(), item.id),
            )
            db.commit()
        finally:
            with self._lock:
                self._threads.pop(item.id, None)

    def cancel_download(self, download_id: int) -> dict:
        with self._lock:
            item = self._downloads.get(download_id)
            if item and item.status == "queued":
                item.status = "canceled"
                db = get_db()
                db.execute(
                    "UPDATE downloads SET status = 'canceled', finished_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), download_id),
                )
                db.commit()
                return {"status": "canceled"}
            elif item:
                item.cancel_event.set()
                return {"status": "canceling"}
        return {"error": "Download not found"}

    def get_queue(self) -> list[dict]:
        db = get_db()
        rows = db.execute(
            "SELECT * FROM downloads WHERE status IN ('queued', 'running') ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_history(self, limit: int = 50) -> list[dict]:
        db = get_db()
        rows = db.execute(
            "SELECT * FROM downloads WHERE status IN ('done', 'failed', 'canceled') ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_download(self, download_id: int) -> dict | None:
        db = get_db()
        row = db.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        if row:
            return dict(row)
        return None

    def list_repo_files(self, repo_id: str) -> list[str]:
        """List .gguf files in a HuggingFace repo."""
        from huggingface_hub import HfApi
        api = HfApi()
        try:
            files = api.list_repo_files(repo_id)
            return [f for f in files if f.endswith(".gguf")]
        except Exception as e:
            return []


downloads = DownloadWorker()
