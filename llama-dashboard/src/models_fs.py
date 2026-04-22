from __future__ import annotations

import os
from pathlib import Path
from collections import defaultdict
import time

_cache = {"models": [], "mmproj": [], "ts": 0.0}
CACHE_TTL = 5


def _human_size(n: int) -> str:
    for unit in ("B", "Ki", "Mi", "Gi", "Ti"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} Pi"


def _scan_dir(directory: str, extension: str = ".gguf") -> list[dict]:
    results = []
    dir_path = Path(directory)
    if not dir_path.exists():
        return results

    # Non-recursive + one level deep
    entries = set()
    for p in dir_path.iterdir():
        entries.add(p.name)
        if p.is_dir():
            try:
                for sub in p.iterdir():
                    if sub.is_file():
                        entries.add(f"{p.name}/{sub.name}")
            except PermissionError:
                pass

    for entry in sorted(entries):
        full = dir_path / entry
        if not full.is_file():
            continue
        if not entry.endswith(extension):
            continue
        try:
            stat = full.stat()
            results.append({
                "filename": entry,
                "full_path": str(full),
                "size": stat.st_size,
                "size_human": _human_size(stat.st_size),
                "mtime": stat.st_mtime,
                "used_by": [],  # filled in by caller
            })
        except (PermissionError, OSError):
            pass

    return results


def scan_models(models_dir: str, presets: dict[str, str]) -> list[dict]:
    """Scan model files and compute used_by from presets."""
    files = _scan_dir(models_dir, ".gguf")
    for f in files:
        basename = f["filename"]
        used = []
        for pname, model_path in presets.items():
            model_basename = os.path.basename(model_path) if model_path else ""
            if model_basename == basename or model_path.endswith("/" + basename) or model_path == basename:
                used.append(pname)
        f["used_by"] = used
    return files


def scan_mmproj(mmproj_dir: str, presets: dict[str, str]) -> list[dict]:
    """Scan mmproj files and compute used_by from presets."""
    files = _scan_dir(mmproj_dir, ".gguf")
    for f in files:
        basename = f["filename"]
        used = []
        for pname, mmproj_path in presets.items():
            mmproj_basename = os.path.basename(mmproj_path) if mmproj_path else ""
            if mmproj_basename == basename or mmproj_path.endswith("/" + basename) or mmproj_path == basename:
                used.append(pname)
        f["used_by"] = used
    return files


def scan_models_cached(settings) -> list[dict]:
    global _cache
    now = time.time()
    if now - _cache["ts"] < CACHE_TTL:
        return _cache["models"]

    presets = {}
    try:
        from .ini_store import IniStore
        store = IniStore(settings.models_ini_path)
        for name in store.section_names():
            section = store.get_section(name)
            if section:
                presets[name] = section.get("model", "")
    except Exception:
        pass

    _cache["models"] = scan_models(settings.models_dir, presets)
    _cache["ts"] = now
    return _cache["models"]


def scan_mmproj_cached(settings) -> list[dict]:
    global _cache
    now = time.time()
    if now - _cache["ts"] < CACHE_TTL:
        return _cache["mmproj"]

    presets = {}
    try:
        from .ini_store import IniStore
        store = IniStore(settings.models_ini_path)
        for name in store.section_names():
            section = store.get_section(name)
            if section:
                presets[name] = section.get("mmproj", "")
    except Exception:
        pass

    _cache["mmproj"] = scan_mmproj(settings.mmproj_dir, presets)
    _cache["ts"] = now
    return _cache["mmproj"]


def get_model_list(settings) -> list[dict]:
    """Get model files with used_by info."""
    return scan_models_cached(settings)


def get_mmproj_list(settings) -> list[dict]:
    """Get mmproj files with used_by info."""
    return scan_mmproj_cached(settings)
