from typing import Any

_state: dict[str, Any] = {
    "vms": [],
    "vm_configs": {},
    "node": {},
    "storage": [],
    "disks": [],
    "backups": [],
    "tailscale": [],
    "services": [],
    "pihole": [],
    "vm_pressure": {},
    "vm_lan_ips":  {},
    "last_updated": None,
}


def get() -> dict:
    return dict(_state)


def update(**kwargs) -> None:
    _state.update(kwargs)
