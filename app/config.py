import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/data/config.json"))

_DEFAULT_SERVICES = [
    {"name": "Nextcloud (Home)",  "local": "https://cloud.homebrewjoe.com",  "public": "https://cloud.homebrewjoe.com"},
    {"name": "Start Page",        "local": "http://192.168.0.102:8889",       "public": "https://start.homebrewjoe.com"},
    {"name": "Forgejo",           "local": "http://192.168.0.102:3000",       "public": None},
    {"name": "Notes",             "local": "http://192.168.0.102:9001",       "public": "https://notes.homebrewjoe.com"},
    {"name": "Guacamole",         "local": "http://192.168.0.102:9002",       "public": "https://remote.homebrewjoe.com"},
    {"name": "Jellyfin",          "local": "http://192.168.0.105:8096",       "public": "https://watch.homebrewjoe.com"},
    {"name": "Immich",            "local": "http://192.168.0.107:2283",       "public": "https://photos.homebrewjoe.com"},
    {"name": "Pi-hole",           "local": "http://192.168.0.253/admin",      "public": None},
    {"name": "SchoolNextcloud",   "local": "https://lunchbox.gesspace.com",   "public": "https://lunchbox.gesspace.com"},
    {"name": "Argus Dashboard",   "local": "http://192.168.0.102:8890",       "public": "https://status.homebrewjoe.com"},
]

# Only non-VM devices go here — VMs get their LAN IP from the guest agent automatically.
_DEFAULT_DEVICE_LAN_MAP = {
    "proxmox": "192.168.0.200",
    "pbs":     "192.168.0.210",
    "mainpc":  "192.168.0.138",
    "tvpc":    "192.168.0.47",
    "pwn":     "192.168.0.164",
}


def load() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text())
    except Exception:
        pass
    return {"services": _DEFAULT_SERVICES, "device_lan_map": _DEFAULT_DEVICE_LAN_MAP}


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
