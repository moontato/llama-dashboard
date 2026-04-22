from __future__ import annotations

import shlex
import os
from .settings import get_settings
from .ini_store import IniStore


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in ("on", "true", "1", "yes")


def build_argv(preset_name: str) -> list[str]:
    """Build command-line argv for a preset."""
    settings = get_settings()
    store = IniStore(settings.models_ini_path)

    # Start with global defaults
    merged = {}
    global_defaults = store.get_global_defaults()
    if global_defaults:
        merged.update(global_defaults)

    # Override with preset-specific values
    section = store.get_section(preset_name)
    if section:
        merged.update(section)

    # Inject host/port if not defined
    if "host" not in merged:
        merged["host"] = settings.llama_server_host
    if "port" not in merged:
        merged["port"] = str(settings.llama_server_port)

    # Build argv
    argv = [settings.llama_server_bin]
    boolean_keys = set(settings.boolean_flag_keys)

    for key, value in merged.items():
        if key in boolean_keys:
            if _is_truthy(value):
                argv.append(f"--{key}")
            # falsy boolean flags are omitted entirely
        else:
            argv.append(f"--{key}")
            argv.append(value)

    return argv


def build_command_string(preset_name: str) -> str:
    """Build a shell-safe command string for display."""
    argv = build_argv(preset_name)
    return shlex.join(argv)
