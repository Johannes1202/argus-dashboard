import asyncio
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from app import config, state

PROXMOX_API  = os.getenv("PROXMOX_API",  "https://192.168.0.200:8006/api2/json")
PROXMOX_NODE = os.getenv("PROXMOX_NODE", "pve2")
PROXMOX_AUTH = f"PVEAPIToken={os.getenv('PROXMOX_TOKEN_ID')}={os.getenv('PROXMOX_TOKEN_SECRET')}"
PBS_STORAGE  = os.getenv("PBS_STORAGE",  "PBS-Server")

TAILSCALE_API     = os.getenv("TAILSCALE_API",     "https://api.tailscale.com/api/v2")
TAILSCALE_TAILNET = os.getenv("TAILSCALE_TAILNET", "-")
TAILSCALE_KEY     = os.getenv("TAILSCALE_API_KEY", "")

PIHOLE_INSTANCES = [
    {"name": "DNS",         "url": os.getenv("PIHOLE_PRIMARY_URL",  "http://192.168.0.253")},
    {"name": "DNSFailsafe", "url": os.getenv("PIHOLE_FAILSAFE_URL", "http://192.168.0.252")},
]
PIHOLE_PASSWORD = os.getenv("PIHOLE_PASSWORD", "")


POLL_INTERVAL          = 15
CONFIG_POLL_INTERVAL   = 300
PRESSURE_POLL_INTERVAL = 60


_config_last_fetched: float = 0.0


# ── helpers ──────────────────────────────────────────────────────────────────

def _bar_class(pct: float) -> str:
    if pct >= 90: return "bar-red"
    if pct >= 70: return "bar-amber"
    return "bar-green"


def _fmt_bytes(n: int) -> str:
    if n < 1024**2: return f"{n/1024:.0f} KB"
    if n < 1024**3: return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.1f} GB"


def _fmt_uptime(seconds: int) -> str:
    if seconds < 60: return f"{seconds}s"
    m = seconds // 60
    if m < 60: return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24: return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def _age_label(age_hours: float | None) -> str:
    if age_hours is None: return "never"
    if age_hours < 1: return "<1h ago"
    if age_hours < 24: return f"{int(age_hours)}h ago"
    return f"{int(age_hours / 24)}d ago"


def _parse_disks(cfg: dict) -> list[dict]:
    disks = []
    for key, val in cfg.items():
        if not re.match(r'^(scsi|virtio|sata|ide)\d+$', key):
            continue
        if 'media=cdrom' in val or val.strip() == 'none' or key == 'efidisk0':
            continue
        m_storage = re.match(r'^([^:]+):', val)
        m_size    = re.search(r'size=([^,\s]+)', val)
        if m_storage and m_size:
            disks.append({"pool": m_storage.group(1), "size": m_size.group(1), "key": key})
    return disks


def _parse_vmbr(cfg: dict) -> str:
    for key in sorted(cfg.keys()):
        if re.match(r'^net\d+$', key):
            m = re.search(r'bridge=([^,\s]+)', cfg[key])
            if m:
                return m.group(1)
    return "—"


def _parse_passthrough(cfg: dict, pci_map: dict) -> list[dict]:
    """Extract PCI passthrough with device names from pci_map {id_prefix: device_name}."""
    devices = []
    for key, val in sorted(cfg.items()):
        if not key.startswith("hostpci"):
            continue
        pci_id  = val.split(",")[0].strip()
        is_gpu  = "x-vga=1" in val
        # Look up device name — pci_map keys may be full (0000:08:00.0) or short (0000:08:00)
        name = pci_map.get(pci_id) or pci_map.get(pci_id + ".0") or "Unknown device"
        # Also infer GPU from known GPU vendors/device names
        if not is_gpu and any(kw in name for kw in ("GeForce", "Radeon", "Arc", "Quadro", "Tesla")):
            is_gpu = True
        devices.append({"pci_id": pci_id, "name": name, "is_gpu": is_gpu})
    return devices


# ── live data (every 15s) ────────────────────────────────────────────────────

async def _fetch_vms(client: httpx.AsyncClient) -> list:
    try:
        r = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/qemu",
                             headers={"Authorization": PROXMOX_AUTH}, timeout=10)
        r.raise_for_status()
        result = []
        for vm in sorted(r.json()["data"], key=lambda v: v.get("name", "")):
            cpu_pct = round(vm.get("cpu", 0) * 100, 1)
            mem     = vm.get("mem", 0)
            maxmem  = vm.get("maxmem", 1)
            mem_pct = min(round(mem / maxmem * 100, 1), 100.0) if maxmem else 0
            result.append({
                "vmid":         vm["vmid"],
                "name":         vm.get("name", f"VM {vm['vmid']}"),
                "status":       vm["status"],
                "cpu_pct":      cpu_pct,
                "cpu_class":    _bar_class(cpu_pct),
                "mem_used_gb":  round(mem / 1024**3, 1),
                "mem_total_gb": round(maxmem / 1024**3, 1),
                "mem_pct":      mem_pct,
                "mem_class":    _bar_class(mem_pct),
                "uptime":       _fmt_uptime(vm.get("uptime", 0)) if vm["status"] == "running" else "—",
                "netin_raw":    vm.get("netin",  0),
                "netout_raw":   vm.get("netout", 0),
            })
        return result
    except Exception:
        return state.get()["vms"]


async def _fetch_storage(client: httpx.AsyncClient) -> list:
    try:
        r = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/storage",
                             headers={"Authorization": PROXMOX_AUTH}, timeout=10)
        r.raise_for_status()
        result = []
        for s in r.json()["data"]:
            if not s.get("active") or not s.get("enabled"):
                continue
            total = s.get("total", 0)
            used  = s.get("used",  0)
            if total == 0:
                continue
            pct = round(used / total * 100, 1)
            result.append({
                "name":      s["storage"],
                "type":      s.get("type", ""),
                "used_gb":   round(used  / 1024**3, 1),
                "total_gb":  round(total / 1024**3, 1),
                "pct":       pct,
                "bar_class": _bar_class(pct),
            })
        return sorted(result, key=lambda s: s["name"])
    except Exception:
        return state.get()["storage"]


async def _fetch_backups(client: httpx.AsyncClient) -> list:
    try:
        r = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/storage/{PBS_STORAGE}/content",
                             headers={"Authorization": PROXMOX_AUTH},
                             params={"content": "backup"}, timeout=15)
        r.raise_for_status()
        snaps_all = r.json()["data"]
        latest: dict[str, dict] = {}
        count_7d: dict[str, int] = {}
        now = datetime.now(timezone.utc).timestamp()
        week_ago = now - 7 * 86400
        for snap in snaps_all:
            vid   = str(snap.get("vmid", "unknown"))
            ctime = snap.get("ctime", 0)
            if vid not in latest or ctime > latest[vid]["ctime"]:
                latest[vid] = snap
            if ctime > week_ago:
                count_7d[vid] = count_7d.get(vid, 0) + 1
        result = []
        for vid, snap in sorted(latest.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            ctime = snap.get("ctime", 0)
            age_h = round((now - ctime) / 3600, 1) if ctime else None
            chip  = "chip-grey" if age_h is None else ("chip-red" if age_h > 168 else ("chip-amber" if age_h > 48 else "chip-green"))
            result.append({
                "vmid":        vid,
                "name":        snap.get("notes", f"VM {vid}"),
                "last_backup": datetime.fromtimestamp(ctime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ctime else "Never",
                "age_label":   _age_label(age_h),
                "chip_class":  chip,
                "size":        _fmt_bytes(snap.get("size", 0)),
                "count_7d":    count_7d.get(vid, 0),
            })
        return result
    except Exception:
        return state.get()["backups"]


async def _fetch_tailscale(client: httpx.AsyncClient) -> list:
    try:
        r = await client.get(f"{TAILSCALE_API}/tailnet/{TAILSCALE_TAILNET}/devices",
                             headers={"Authorization": f"Bearer {TAILSCALE_KEY}"}, timeout=10)
        r.raise_for_status()
        vm_lan_ips    = state.get().get("vm_lan_ips", {})
        device_lan_map = config.load().get("device_lan_map", {})
        devices = []
        for d in r.json().get("devices", []):
            friendly = d.get("name", "").split(".")[0] or d.get("hostname", "unknown")
            key = friendly.lower()
            lan_ip = vm_lan_ips.get(key) or device_lan_map.get(key, "")
            devices.append({
                "name":   friendly,
                "ts_ip":  (d.get("addresses") or [""])[0],
                "lan_ip": lan_ip,
                "online": d.get("connectedToControl", False),
            })
        return sorted(devices, key=lambda d: d["name"])
    except Exception:
        return state.get()["tailscale"]


async def _check_service(client: httpx.AsyncClient, svc: dict) -> dict:
    try:
        r = await client.get(svc["local"], timeout=5, follow_redirects=True)
        return {**svc, "up": r.status_code < 500}
    except Exception:
        return {**svc, "up": False}


async def _fetch_services(client: httpx.AsyncClient) -> list:
    return list(await asyncio.gather(*[_check_service(client, s) for s in config.load()["services"]]))


async def _fetch_one_pihole(client: httpx.AsyncClient, instance: dict) -> dict:
    base = {"name": instance["name"], "available": False}
    if not PIHOLE_PASSWORD:
        return base
    try:
        auth_r = await client.post(f"{instance['url']}/api/auth",
                                   json={"password": PIHOLE_PASSWORD}, timeout=5)
        auth_r.raise_for_status()
        sid = auth_r.json().get("session", {}).get("sid")
        if not sid:
            return base
        stats_r = await client.get(f"{instance['url']}/api/stats/summary",
                                   headers={"X-FTL-SID": sid}, timeout=5)
        stats_r.raise_for_status()
        await client.delete(f"{instance['url']}/api/auth",
                            headers={"X-FTL-SID": sid}, timeout=3)
        d       = stats_r.json()
        queries = d.get("queries", {})
        total   = queries.get("total", 0)
        blocked = queries.get("blocked", 0)
        pct     = round(blocked / total * 100, 1) if total else 0
        return {
            "name":      instance["name"],
            "available": True,
            "total":     total,
            "blocked":   blocked,
            "pct":       pct,
            "cached":    queries.get("cached", 0),
            "domains_blocked": d.get("gravity", {}).get("domains_being_blocked", 0),
            "clients":   d.get("clients", {}).get("active", 0),
        }
    except Exception:
        return base


async def _fetch_pihole(client: httpx.AsyncClient) -> list:
    return list(await asyncio.gather(*[_fetch_one_pihole(client, i) for i in PIHOLE_INSTANCES]))


# ── static config (every 5 min) ──────────────────────────────────────────────

async def _fetch_node(client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/status",
                             headers={"Authorization": PROXMOX_AUTH}, timeout=10)
        r.raise_for_status()
        d = r.json()["data"]
        return {
            "cores":        d["cpuinfo"]["cpus"],
            "cpu_pct":      round(d["cpu"] * 100, 1),
            "mem_total_gb": round(d["memory"]["total"] / 1024**3, 1),
            "mem_used_gb":  round(d["memory"]["used"]  / 1024**3, 1),
            "mem_pct":      round(d["memory"]["used"] / d["memory"]["total"] * 100, 1),
        }
    except Exception:
        return state.get()["node"]


async def _fetch_pci_map(client: httpx.AsyncClient) -> dict:
    """Build {pci_id_prefix: device_name} map from the node's PCI hardware list."""
    try:
        r = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/hardware/pci",
                             headers={"Authorization": PROXMOX_AUTH}, timeout=10)
        r.raise_for_status()
        pci_map = {}
        for dev in r.json()["data"]:
            dev_id   = dev.get("id", "")          # e.g. "0000:08:00.0"
            dev_name = dev.get("device_name", "Unknown")
            pci_map[dev_id] = dev_name
            # also index without the function suffix for easier lookup
            prefix = re.sub(r'\.\d+$', '', dev_id)
            if prefix not in pci_map:
                pci_map[prefix] = dev_name
        return pci_map
    except Exception:
        return {}


async def _fetch_vm_configs(client: httpx.AsyncClient, pci_map: dict) -> dict:
    try:
        r = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/qemu",
                             headers={"Authorization": PROXMOX_AUTH}, timeout=10)
        r.raise_for_status()
        vmids = [vm["vmid"] for vm in r.json()["data"]]

        async def one(vmid: int) -> tuple:
            cr = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/qemu/{vmid}/config",
                                  headers={"Authorization": PROXMOX_AUTH}, timeout=10)
            cr.raise_for_status()
            cfg = cr.json()["data"]
            return vmid, {
                "cores":       cfg.get("cores", 1),
                "mem_gb":      round(int(cfg.get("memory", 0)) / 1024, 1),
                "disks":       _parse_disks(cfg),
                "passthrough": _parse_passthrough(cfg, pci_map),
                "vmbr":        _parse_vmbr(cfg),
            }

        results = await asyncio.gather(*[one(v) for v in vmids], return_exceptions=True)
        out = {}
        for r in results:
            if not isinstance(r, BaseException):
                vmid, cfg = r
                out[vmid] = cfg
        return out
    except Exception:
        return state.get()["vm_configs"]


async def _fetch_disks(client: httpx.AsyncClient) -> list:
    """Physical disk inventory — includes unmanaged/unused disks."""
    try:
        r = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/disks/list",
                             headers={"Authorization": PROXMOX_AUTH}, timeout=10)
        r.raise_for_status()
        result = []
        for disk in sorted(r.json()["data"], key=lambda d: d.get("devpath", "")):
            size_gb = round(disk.get("size", 0) / 1024**3, 0)
            used    = disk.get("used", "?")
            result.append({
                "dev":   disk.get("devpath", "?"),
                "model": disk.get("model", "Unknown").strip(),
                "size":  f"{int(size_gb)}GB",
                "used":  used if used else "unused",
            })
        return result
    except Exception:
        return state.get().get("disks", [])


# ── VM LAN IP discovery via QEMU guest agent ─────────────────────────────────

async def _fetch_one_vm_lan(client: httpx.AsyncClient, vmid: int) -> tuple[str, str] | None:
    try:
        hn_r = await client.get(
            f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/qemu/{vmid}/agent/get-host-name",
            headers={"Authorization": PROXMOX_AUTH}, timeout=5,
        )
        hn_r.raise_for_status()
        hostname = hn_r.json()["data"]["result"]["host-name"].lower()

        ni_r = await client.get(
            f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/qemu/{vmid}/agent/network-get-interfaces",
            headers={"Authorization": PROXMOX_AUTH}, timeout=5,
        )
        ni_r.raise_for_status()
        for iface in ni_r.json()["data"]["result"]:
            if iface.get("name") == "lo":
                continue
            for addr in iface.get("ip-addresses", []):
                ip = addr.get("ip-address", "")
                # skip loopback and Tailscale (100.x) addresses
                if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127.") and not ip.startswith("100."):
                    return hostname, ip
        return None
    except Exception:
        return None


async def _fetch_vm_lan_ips(client: httpx.AsyncClient) -> dict[str, str]:
    try:
        r = await client.get(f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/qemu",
                             headers={"Authorization": PROXMOX_AUTH}, timeout=10)
        r.raise_for_status()
        running = [vm["vmid"] for vm in r.json()["data"] if vm.get("status") == "running"]
    except Exception:
        return state.get().get("vm_lan_ips", {})
    results = await asyncio.gather(*[_fetch_one_vm_lan(client, vmid) for vmid in running])
    return {r[0]: r[1] for r in results if r}


# ── RAM pressure via QEMU guest agent ────────────────────────────────────────

_AWK_CMD = [
    "awk",
    "/MemTotal/{t=$2} /MemAvailable/{a=$2} END{printf \"%d %d\", t-a, t}",
    "/proc/meminfo",
]


async def _fetch_one_pressure(client: httpx.AsyncClient, vmid: int) -> tuple[int, dict]:
    try:
        exec_r = await client.post(
            f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/qemu/{vmid}/agent/exec",
            headers={"Authorization": PROXMOX_AUTH},
            json={"command": _AWK_CMD},
            timeout=10,
        )
        exec_r.raise_for_status()
        pid = exec_r.json()["data"]["pid"]

        for _ in range(10):
            await asyncio.sleep(0.5)
            st_r = await client.get(
                f"{PROXMOX_API}/nodes/{PROXMOX_NODE}/qemu/{vmid}/agent/exec-status",
                headers={"Authorization": PROXMOX_AUTH},
                params={"pid": pid},
                timeout=10,
            )
            st_r.raise_for_status()
            d = st_r.json()["data"]
            if d.get("exited"):
                used_kb, total_kb = map(int, d.get("out-data", "").split())
                used_gb  = round(used_kb  / 1024**2, 2)
                total_gb = round(total_kb / 1024**2, 1)
                pct      = round(used_kb / total_kb * 100, 1) if total_kb else 0
                return vmid, {"used_gb": used_gb, "total_gb": total_gb, "pct": pct, "bar_class": _bar_class(pct)}
        return vmid, {}
    except Exception:
        return vmid, {}


async def _fetch_vm_pressure() -> dict:
    running = [vm["vmid"] for vm in state.get()["vms"] if vm["status"] == "running"]
    async with httpx.AsyncClient(verify=False) as client:
        results = await asyncio.gather(*[_fetch_one_pressure(client, vmid) for vmid in running])
    return {vmid: data for vmid, data in results if data}


# ── poll loops ────────────────────────────────────────────────────────────────

async def poll_live() -> None:
    async with httpx.AsyncClient(verify=False) as client:
        vms, storage, backups, tailscale, services, pihole = await asyncio.gather(
            _fetch_vms(client),
            _fetch_storage(client),
            _fetch_backups(client),
            _fetch_tailscale(client),
            _fetch_services(client),
            _fetch_pihole(client),
        )
    now = time.time()
    cutoff = now - PRESSURE_HISTORY_WINDOW
    for vm in vms:
        vmid = vm["vmid"]
        _cpu_history[vmid].append((now, vm["cpu_pct"]))
        _cpu_history[vmid] = [s for s in _cpu_history[vmid] if s[0] > cutoff]
        vm["cpu_peak_pct"] = max(s[1] for s in _cpu_history[vmid])

    # bandwidth accumulation with calendar resets
    global _net_last_day, _net_last_month
    now_dt = datetime.now()
    if now_dt.day != _net_last_day:
        _net_daily.clear()
        _net_last_day = now_dt.day
    if now_dt.month != _net_last_month:
        _net_monthly.clear()
        _net_last_month = now_dt.month

    for vm in vms:
        vmid   = vm["vmid"]
        netin  = vm.pop("netin_raw",  0)
        netout = vm.pop("netout_raw", 0)
        if vmid in _net_prev:
            prev_in, prev_out = _net_prev[vmid]
            delta_in  = max(0, netin  - prev_in)
            delta_out = max(0, netout - prev_out)
            di, do = _net_daily.get(vmid,   (0, 0))
            mi, mo = _net_monthly.get(vmid, (0, 0))
            _net_daily[vmid]   = (di + delta_in,  do + delta_out)
            _net_monthly[vmid] = (mi + delta_in,  mo + delta_out)
        _net_prev[vmid] = (netin, netout)
        d = _net_daily.get(vmid,   (0, 0))
        m = _net_monthly.get(vmid, (0, 0))
        vm["net_day_in"]  = _fmt_bytes(d[0])
        vm["net_day_out"] = _fmt_bytes(d[1])
        vm["net_mon_in"]  = _fmt_bytes(m[0])
        vm["net_mon_out"] = _fmt_bytes(m[1])

    state.update(vms=vms, storage=storage, backups=backups, tailscale=tailscale,
                 services=services, pihole=pihole,
                 last_updated=datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))


async def poll_configs() -> None:
    async with httpx.AsyncClient(verify=False) as client:
        node, pci_map, disks = await asyncio.gather(
            _fetch_node(client),
            _fetch_pci_map(client),
            _fetch_disks(client),
        )
        vm_configs, vm_lan_ips = await asyncio.gather(
            _fetch_vm_configs(client, pci_map),
            _fetch_vm_lan_ips(client),
        )
    state.update(node=node, vm_configs=vm_configs, disks=disks, vm_lan_ips=vm_lan_ips)


_pressure_last_fetched: float = 0.0
_pressure_history: dict = defaultdict(list)
_cpu_history: dict = defaultdict(list)
PRESSURE_HISTORY_WINDOW = 86400  # 24h

_net_prev:      dict[int, tuple[int, int]] = {}  # vmid -> (netin, netout) last seen
_net_daily:     dict[int, tuple[int, int]] = {}  # vmid -> (in_bytes, out_bytes) today
_net_monthly:   dict[int, tuple[int, int]] = {}  # vmid -> (in_bytes, out_bytes) this month
_net_last_day:   int = -1
_net_last_month: int = -1


async def poll_pressure() -> None:
    pressure = await _fetch_vm_pressure()
    now = time.time()
    cutoff = now - PRESSURE_HISTORY_WINDOW
    for vmid, data in pressure.items():
        _pressure_history[vmid].append((now, data["used_gb"], data["pct"]))
        _pressure_history[vmid] = [s for s in _pressure_history[vmid] if s[0] > cutoff]
        peak = max(_pressure_history[vmid], key=lambda s: s[2])
        data["peak_gb"]  = peak[1]
        data["peak_pct"] = peak[2]
    state.update(vm_pressure=pressure)


async def poll_once() -> None:
    await asyncio.gather(poll_live(), poll_configs())
    await poll_pressure()


async def start_polling() -> None:
    global _config_last_fetched, _pressure_last_fetched
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        await poll_live()
        now = time.time()
        if now - _config_last_fetched >= CONFIG_POLL_INTERVAL:
            await poll_configs()
            _config_last_fetched = now
        if now - _pressure_last_fetched >= PRESSURE_POLL_INTERVAL:
            await poll_pressure()
            _pressure_last_fetched = now
