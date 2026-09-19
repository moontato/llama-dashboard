"""Single-process, public Hugging Face GGUF downloads (no Hub CLI needed)."""
from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque


class DownloadError(ValueError):
    pass


class DownloadConflict(DownloadError):
    pass


# Hub redirects currently use these Hugging Face controlled CDN domains.
# Match suffixes on DNS-label boundaries, never arbitrary substring matches.
CDN_DOMAINS = ("huggingface.co", "hf.co", "xethub.hf.co")
PART_RE = re.compile(r"^\.llama-download-[0-9a-f]{32}\.part$")


def validate_remote(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        if (p.scheme != "https" or p.username is not None or p.password is not None
                or p.port not in (None, 443)
                or not any(host == d or host.endswith("." + d) for d in CDN_DOMAINS)):
            raise DownloadError("Download redirected to an unsupported host")
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise DownloadError("Download host must resolve to public addresses")
    except (ValueError, OSError) as exc:
        raise DownloadError("Unsupported or unreachable download host") from exc


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_remote(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_source(url):
    if not isinstance(url, str) or not url.strip() or len(url) > 8192:
        raise DownloadError("Enter a Hugging Face GGUF file URL")
    url = url.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise DownloadError("Invalid URL")
    try:
        p = urllib.parse.urlsplit(url)
        if (p.scheme != "https" or p.netloc != "huggingface.co"
                or p.username is not None):
            raise DownloadError("Use an https://huggingface.co file URL")
        parts = p.path.split("/")
        if (len(parts) < 6 or parts[0] or parts[3] not in ("blob", "resolve")
                or any(not s for s in parts[1:])):
            raise DownloadError("Use a file URL containing /blob/ or /resolve/")
        for s in parts[1:]:
            decoded = urllib.parse.unquote(s)
            if decoded in (".", "..") or any(ord(c) < 32 for c in decoded):
                raise DownloadError("Invalid URL path")
        name = urllib.parse.unquote(parts[-1])
        if not name.lower().endswith(".gguf"):
            raise DownloadError("The source file must end in .gguf")
        filename(name)
        parts[3] = "resolve"
        return urllib.parse.urlunsplit(("https", "huggingface.co", "/".join(parts), "", "")), name
    except ValueError as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError("Invalid URL") from exc


def filename(value):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or value.startswith(".") or "/" in value or "\\" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise DownloadError("Filename must be a plain basename without path separators")
    if not value.lower().endswith(".gguf"):
        if "." in value:
            raise DownloadError("Filename must end in .gguf")
        value += ".gguf"
    if len(os.fsencode(value)) > 255:
        raise DownloadError("Filename is too long")
    return value


def open_destination(root, subdirectory):
    if not isinstance(subdirectory, str) or subdirectory not in ("", "mtp", "mmproj", "archived"):
        raise DownloadError("Invalid destination directory")
    # Resolve the configured root once, then anchor all mutations to directory
    # descriptors. Subdirectory replacement/symlink races cannot escape it.
    root = os.path.realpath(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if subdirectory:
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir(subdirectory, mode=0o755, dir_fd=fd)
            child = os.open(subdirectory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        finally:
            os.close(fd)
        fd = child
    return root, fd


class DownloadManager:
    def __init__(self, opener=None):
        self.lock = threading.RLock()
        self.jobs = deque(maxlen=20)
        self.active = None
        self.thread = None
        self.cleaned = set()
        self.opener = opener or urllib.request.build_opener(SafeRedirect())

    def snapshot(self):
        with self.lock:
            return [dict(j["view"]) for j in self.jobs]

    def start(self, root, url, subdirectory="", name=None):
        source, original = parse_source(url)
        if name is not None and not isinstance(name, str):
            raise DownloadError("Filename must be text")
        target = filename(name if name else original)
        with self.lock:
            if self.active:
                raise DownloadConflict("A download is already running")
            fd = None
            part = None
            output = None
            job = None
            try:
                root, fd = open_destination(root, subdirectory)
                key = (root, subdirectory)
                if key not in self.cleaned:
                    for entry in os.listdir(fd):
                        if PART_RE.fullmatch(entry):
                            os.unlink(entry, dir_fd=fd)
                    self.cleaned.add(key)
                try:
                    os.stat(target, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise DownloadConflict("A file with this name already exists")
                job_id = uuid.uuid4().hex
                part = ".llama-download-" + job_id + ".part"
                output = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
                job = {"cancel": threading.Event(), "view": {
                    "id": job_id, "state": "connecting", "filename": target,
                    "destination": os.path.join(subdirectory, target),
                    "path": os.path.join(root, subdirectory, target),
                    "downloaded_bytes": 0, "total_bytes": None, "error": None,
                }}
                self.jobs.appendleft(job)
                self.active = job
                self.thread = threading.Thread(target=self._run, args=(job, source, fd, output, part, target), daemon=True)
                self.thread.start()
                return dict(job["view"])
            except BaseException:
                if job is not None:
                    self.active = None
                    self.thread = None
                    self.jobs.remove(job)
                if output is not None:
                    os.close(output)
                if fd is not None:
                    if part:
                        with contextlib.suppress(OSError):
                            os.unlink(part, dir_fd=fd)
                    os.close(fd)
                raise

    def cancel(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j["view"]["id"] == job_id), None)
            if job is None:
                return None
            if job is self.active:
                job["cancel"].set()
            return dict(job["view"])

    def close(self):
        with self.lock:
            if self.active:
                self.active["cancel"].set()
            thread = self.thread
        if thread:
            thread.join(timeout=16)

    def _update(self, job, **values):
        with self.lock:
            job["view"].update(values)

    def _run(self, job, source, directory, output, part, target):
        state, error = "failed", None
        try:
            with os.fdopen(output, "wb") as dest:
                validate_remote(source)
                req = urllib.request.Request(source, headers={"User-Agent": "llama-dashboard", "Accept-Encoding": "identity"})
                with self.opener.open(req, timeout=15) as response:
                    if response.status != 200:
                        raise DownloadError("Server did not return a complete file")
                    length = response.headers.get("Content-Length")
                    total = int(length) if length is not None else None
                    if total is not None and total < 4:
                        raise DownloadError("Remote file is too small to be GGUF")
                    space = os.fstatvfs(directory)
                    if total is not None and space.f_bavail * space.f_frsize < total:
                        raise DownloadError("Not enough free disk space")
                    self._update(job, state="downloading", total_bytes=total)
                    received, magic = 0, b""
                    while True:
                        if job["cancel"].is_set():
                            raise DownloadError("Cancelled")
                        # One socket read per iteration keeps progress and cancellation
                        # responsive even on slow links (rather than filling a MB).
                        chunk = response.read1(256 * 1024)
                        if not chunk:
                            break
                        if len(magic) < 4:
                            magic = (magic + chunk)[:4]
                            if len(magic) == 4 and magic != b"GGUF":
                                raise DownloadError("Remote content is not a GGUF file")
                        dest.write(chunk)
                        received += len(chunk)
                        self._update(job, downloaded_bytes=received)
                    if magic != b"GGUF" or (total is not None and received != total):
                        raise DownloadError("Incomplete GGUF download")
                dest.flush()
                os.fsync(dest.fileno())
            # Hard-link publication is atomic and refuses to replace any existing
            # path. Both entries are on the same filesystem; unlink the temp below.
            with self.lock:
                if job["cancel"].is_set():
                    raise DownloadError("Cancelled")
                os.link(part, target, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                state = "completed"
                job["view"]["state"] = state
                self.active = None
        except urllib.error.HTTPError as exc:
            exc.close()
            error = {401: "This file requires Hugging Face authentication (public files only)",
                     403: "Access denied: use a public, ungated file", 404: "File not found on Hugging Face"}.get(exc.code, "Hugging Face returned HTTP " + str(exc.code))
        except FileExistsError:
            error = "Destination already exists; no file was overwritten"
        except DownloadError as exc:
            error = str(exc)
        except OSError as exc:
            error = "Download failed: " + (exc.strerror or "network or filesystem error")
        except Exception:
            error = "Download failed: invalid or interrupted response"
        finally:
            with contextlib.suppress(OSError):
                os.unlink(part, dir_fd=directory)
            os.close(directory)
            with self.lock:
                if state != "completed" and job["cancel"].is_set():
                    state, error = "cancelled", None
                job["view"].update(state=state, error=error)
                if self.active is job:
                    self.active = None
