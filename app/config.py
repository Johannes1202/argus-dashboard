import json
import os
from pathlib import Path

import yaml

CONFIG_PATH      = Path(os.getenv("CONFIG_PATH",      "/app/data/config.json"))
YAML_CONFIG_PATH = Path(os.getenv("YAML_CONFIG_PATH", "/app/config.yml"))


def _load_yaml() -> dict:
    try:
        if YAML_CONFIG_PATH.exists():
            data = yaml.safe_load(YAML_CONFIG_PATH.read_text()) or {}
            services       = data.get("services", [])
            device_lan_map = data.get("device_lan_map", {})
            # Normalise: ensure every service has a 'public' key
            for svc in services:
                svc.setdefault("public", None)
            return {"services": services, "device_lan_map": device_lan_map}
    except Exception:
        pass
    return {"services": [], "device_lan_map": {}}


def load() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text())
    except Exception:
        pass
    return _load_yaml()


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
