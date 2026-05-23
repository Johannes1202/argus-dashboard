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
            for svc in services:
                svc.setdefault("public", None)
            return {"services": services, "device_lan_map": device_lan_map}
    except Exception:
        pass
    return {"services": [], "device_lan_map": {}}


def load() -> dict:
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text())
            if not cfg.get("services"):
                y = _load_yaml()
                cfg.setdefault("services",       y.get("services", []))
                cfg.setdefault("device_lan_map", y.get("device_lan_map", {}))
            return cfg
    except Exception:
        pass
    return _load_yaml()


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get_proxmox(cfg: dict) -> dict:
    p = cfg.get("proxmox", {})
    return {
        "api_url":      p.get("api_url")      or os.getenv("PROXMOX_API",          "https://192.168.1.10:8006/api2/json"),
        "node":         p.get("node")          or os.getenv("PROXMOX_NODE",         "pve"),
        "token_id":     p.get("token_id")      or os.getenv("PROXMOX_TOKEN_ID",     ""),
        "token_secret": p.get("token_secret")  or os.getenv("PROXMOX_TOKEN_SECRET", ""),
        "storage":      cfg.get("pbs", {}).get("storage") or os.getenv("PBS_STORAGE", "PBS-Server"),
    }


def get_tailscale(cfg: dict) -> dict:
    t = cfg.get("tailscale", {})
    return {
        "api_key": t.get("api_key") or os.getenv("TAILSCALE_API_KEY", ""),
        "tailnet": t.get("tailnet") or os.getenv("TAILSCALE_TAILNET", "-"),
    }


def get_pihole(cfg: dict) -> list:
    ph = cfg.get("pihole", [])
    if ph:
        return ph
    password = os.getenv("PIHOLE_PASSWORD", "")
    instances = []
    primary  = os.getenv("PIHOLE_PRIMARY_URL",  "")
    failsafe = os.getenv("PIHOLE_FAILSAFE_URL", "")
    if primary:
        instances.append({"name": "DNS",         "url": primary,  "password": password})
    if failsafe:
        instances.append({"name": "DNSFailsafe", "url": failsafe, "password": password})
    return instances
