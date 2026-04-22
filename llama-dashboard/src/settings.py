import os
import threading
from pathlib import Path
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:
    import tomllib as tomllib  # type: ignore[no-redef]

import tomli_w

DEFAULTS = {
    "llama_server": {
        "bin": "/ssd/llama.cpp/build/bin/llama-server",
        "host": "0.0.0.0",
        "port": 8080,
    },
    "dashboard": {
        "host": "0.0.0.0",
        "port": 8734,
    },
    "paths": {
        "models_dir": "/ssd/llamacpp_models",
        "mmproj_dir": "/ssd/llamacpp_models/mmproj",
        "models_ini_path": "/ssd/llamacpp_models/models.ini",
        "log_dir": "./logs",
    },
    "features": {
        "allow_multiple_servers": False,
    },
    "huggingface": {
        "token": "",
    },
    "boolean_flag_keys": {
        "keys": [],
    },
}


@dataclass
class Settings:
    llama_server_bin: str = DEFAULTS["llama_server"]["bin"]
    llama_server_host: str = DEFAULTS["llama_server"]["host"]
    llama_server_port: int = DEFAULTS["llama_server"]["port"]
    dashboard_host: str = DEFAULTS["dashboard"]["host"]
    dashboard_port: int = DEFAULTS["dashboard"]["port"]
    models_dir: str = DEFAULTS["paths"]["models_dir"]
    mmproj_dir: str = DEFAULTS["paths"]["mmproj_dir"]
    models_ini_path: str = DEFAULTS["paths"]["models_ini_path"]
    log_dir: str = DEFAULTS["paths"]["log_dir"]
    allow_multiple_servers: bool = DEFAULTS["features"]["allow_multiple_servers"]
    hf_token: str = DEFAULTS["huggingface"]["token"]
    boolean_flag_keys: list[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "llama_server_bin": self.llama_server_bin,
            "llama_server_host": self.llama_server_host,
            "llama_server_port": self.llama_server_port,
            "dashboard_host": self.dashboard_host,
            "dashboard_port": self.dashboard_port,
            "models_dir": self.models_dir,
            "mmproj_dir": self.mmproj_dir,
            "models_ini_path": self.models_ini_path,
            "log_dir": self.log_dir,
            "allow_multiple_servers": self.allow_multiple_servers,
            "hf_token": self.hf_token,
            "boolean_flag_keys": self.boolean_flag_keys,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            llama_server_bin=d.get("llama_server_bin", cls.llama_server_bin),
            llama_server_host=d.get("llama_server_host", cls.llama_server_host),
            llama_server_port=d.get("llama_server_port", cls.llama_server_port),
            dashboard_host=d.get("dashboard_host", cls.dashboard_host),
            dashboard_port=d.get("dashboard_port", cls.dashboard_port),
            models_dir=d.get("models_dir", cls.models_dir),
            mmproj_dir=d.get("mmproj_dir", cls.mmproj_dir),
            models_ini_path=d.get("models_ini_path", cls.models_ini_path),
            log_dir=d.get("log_dir", cls.log_dir),
            allow_multiple_servers=d.get("allow_multiple_servers", cls.allow_multiple_servers),
            hf_token=d.get("hf_token", cls.hf_token),
            boolean_flag_keys=d.get("boolean_flag_keys", cls.boolean_flag_keys),
        )


_settings_lock = threading.Lock()
_settings: Settings | None = None


def _resolve_log_dir(raw: str, app_dir: Path) -> str:
    if os.path.isabs(raw):
        return raw
    return str(app_dir / raw)


def load_settings(app_dir: Path | None = None) -> Settings:
    global _settings
    if app_dir is None:
        app_dir = Path(__file__).resolve().parent.parent

    config_path = app_dir / "config.toml"
    example_path = app_dir / "config.toml.example"

    if not config_path.exists():
        if example_path.exists():
            import shutil
            shutil.copy2(str(example_path), str(config_path))
        else:
            config_path.write_text("")

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        data = {}

    settings = Settings()

    llama = data.get("llama_server", {})
    settings.llama_server_bin = llama.get("bin", settings.llama_server_bin)
    settings.llama_server_host = llama.get("host", settings.llama_server_host)
    settings.llama_server_port = llama.get("port", settings.llama_server_port)

    dash = data.get("dashboard", {})
    settings.dashboard_host = dash.get("host", settings.dashboard_host)
    settings.dashboard_port = dash.get("port", settings.dashboard_port)

    paths = data.get("paths", {})
    settings.models_dir = paths.get("models_dir", settings.models_dir)
    settings.mmproj_dir = paths.get("mmproj_dir", settings.mmproj_dir)
    settings.models_ini_path = paths.get("models_ini_path", settings.models_ini_path)
    raw_log = paths.get("log_dir", settings.log_dir)
    settings.log_dir = _resolve_log_dir(raw_log, app_dir)

    features = data.get("features", {})
    settings.allow_multiple_servers = features.get(
        "allow_multiple_servers", settings.allow_multiple_servers
    )

    hf = data.get("huggingface", {})
    settings.hf_token = hf.get("token", settings.hf_token)

    bfk = data.get("boolean_flag_keys", {})
    keys = bfk.get("keys", [])
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    settings.boolean_flag_keys = keys

    with _settings_lock:
        _settings = settings
    return settings


def get_settings() -> Settings:
    with _settings_lock:
        if _settings is None:
            raise RuntimeError("Settings not loaded. Call load_settings() first.")
        return _settings


def save_settings(s: Settings) -> None:
    app_dir = Path(__file__).resolve().parent.parent
    config_path = app_dir / "config.toml"

    # Resolve log_dir for display (relative to app_dir)
    if os.path.isabs(s.log_dir):
        try:
            relative_log = os.path.relpath(s.log_dir, app_dir)
        except ValueError:
            relative_log = s.log_dir
    else:
        relative_log = s.log_dir

    data = {
        "llama_server": {
            "bin": s.llama_server_bin,
            "host": s.llama_server_host,
            "port": s.llama_server_port,
        },
        "dashboard": {
            "host": s.dashboard_host,
            "port": s.dashboard_port,
        },
        "paths": {
            "models_dir": s.models_dir,
            "mmproj_dir": s.mmproj_dir,
            "models_ini_path": s.models_ini_path,
            "log_dir": s.log_dir,
        },
        "features": {
            "allow_multiple_servers": s.allow_multiple_servers,
        },
        "huggingface": {
            "token": s.hf_token,
        },
        "boolean_flag_keys": {
            "keys": s.boolean_flag_keys,
        },
    }

    tmp = config_path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        tomli_w.dump(data, f)
    os.replace(str(tmp), str(config_path))

    with _settings_lock:
        _settings = s
