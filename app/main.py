import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.middleware.base import BaseHTTPMiddleware

from app import config, state
from app.poller import poll_once, start_polling

templates = Jinja2Templates(directory="app/templates")

PAGES = ["overview", "guests", "storage", "backups", "services", "tailscale", "dns", "settings"]

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "argus")
SECRET_KEY         = os.environ.get("SECRET_KEY", "change-me")
APP_TITLE          = os.environ.get("APP_TITLE", "Argus")

_signer = URLSafeTimedSerializer(SECRET_KEY)

SESSION_COOKIE  = "argus_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
PUBLIC_PATHS    = {"/login", "/static"}


def _make_session_token() -> str:
    return _signer.dumps("authenticated")


def _verify_session_token(token: str) -> bool:
    try:
        _signer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/static/"):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE)
        if not token or not _verify_session_token(token):
            return RedirectResponse(url="/login", status_code=302)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await poll_once()
    task = asyncio.create_task(start_polling())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None, "title": APP_TITLE})


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, password: str = Form(...)):
    if password == DASHBOARD_PASSWORD:
        token    = _make_session_token()
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
        return response
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Incorrect password", "title": APP_TITLE}, status_code=401
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "title": APP_TITLE})


@app.get("/api/status")
async def api_status():
    return state.get()


@app.get("/page/{name}", response_class=HTMLResponse)
async def page(name: str, request: Request):
    if name not in PAGES:
        return HTMLResponse("<p>Page not found</p>", status_code=404)

    data = state.get()
    ctx  = {"request": request, **data}

    if name == "overview":
        ctx["totals"] = _compute_totals(data)

    if name == "settings":
        cfg = config.load()
        px  = cfg.get("proxmox", {})
        ts  = cfg.get("tailscale", {})
        ctx.update({
            "px_api_url":    px.get("api_url",      ""),
            "px_node":       px.get("node",         ""),
            "px_token_set":  bool(px.get("token_id")),
            "px_secret_set": bool(px.get("token_secret")),
            "pbs_storage":   cfg.get("pbs", {}).get("storage", ""),
            "ts_tailnet":    ts.get("tailnet", "-"),
            "ts_key_set":    bool(ts.get("api_key")),
            "pihole_instances": [
                {"name": ph["name"], "url": ph["url"], "password_set": bool(ph.get("password"))}
                for ph in cfg.get("pihole", [])
            ],
            "services_cfg":   cfg.get("services", []),
            "device_lan_map": cfg.get("device_lan_map", {}),
        })

    return templates.TemplateResponse(f"page_{name}.html", ctx)


# ── config API ────────────────────────────────────────────────────────────────

@app.post("/api/config/proxmox")
async def save_proxmox(request: Request):
    data = await request.json()
    cfg  = config.load()
    cfg.setdefault("proxmox", {})
    if data.get("api_url"):      cfg["proxmox"]["api_url"]      = data["api_url"]
    if data.get("node"):         cfg["proxmox"]["node"]          = data["node"]
    if data.get("token_id"):     cfg["proxmox"]["token_id"]      = data["token_id"]
    if data.get("token_secret"): cfg["proxmox"]["token_secret"]  = data["token_secret"]
    config.save(cfg)
    return JSONResponse({"ok": True})


@app.post("/api/config/pbs")
async def save_pbs(request: Request):
    data = await request.json()
    cfg  = config.load()
    cfg.setdefault("pbs", {})
    if data.get("storage"): cfg["pbs"]["storage"] = data["storage"]
    config.save(cfg)
    return JSONResponse({"ok": True})


@app.post("/api/config/tailscale")
async def save_tailscale(request: Request):
    data = await request.json()
    cfg  = config.load()
    cfg.setdefault("tailscale", {})
    if data.get("tailnet"): cfg["tailscale"]["tailnet"] = data["tailnet"]
    if data.get("api_key"): cfg["tailscale"]["api_key"] = data["api_key"]
    config.save(cfg)
    return JSONResponse({"ok": True})


@app.post("/api/config/pihole")
async def save_pihole(request: Request):
    instances = await request.json()
    cfg = config.load()
    # Preserve existing passwords for instances where password was not re-entered
    existing = {ph["name"]: ph for ph in cfg.get("pihole", [])}
    merged = []
    for inst in instances:
        name     = inst.get("name", "")
        url      = inst.get("url", "")
        password = inst.get("password") or existing.get(name, {}).get("password", "")
        if name and url:
            merged.append({"name": name, "url": url, "password": password})
    cfg["pihole"] = merged
    config.save(cfg)
    return JSONResponse({"ok": True})


@app.post("/api/config/services")
async def save_services(request: Request):
    services = await request.json()
    cfg = config.load()
    cfg["services"] = services
    config.save(cfg)
    return JSONResponse({"ok": True})


@app.post("/api/config/devices")
async def save_devices(request: Request):
    devices = await request.json()
    cfg = config.load()
    cfg["device_lan_map"] = devices
    config.save(cfg)
    return JSONResponse({"ok": True})


# ── totals ────────────────────────────────────────────────────────────────────

def _compute_totals(data: dict) -> dict:
    vms         = data.get("vms", [])
    lxc         = data.get("lxc", [])
    vm_configs  = data.get("vm_configs", {})
    lxc_configs = data.get("lxc_configs", {})
    node        = data.get("node", {})
    services    = data.get("services", [])
    backups     = data.get("backups", [])
    storage     = data.get("storage", [])
    tailscale   = data.get("tailscale", [])
    vm_pressure = data.get("vm_pressure", {})

    # Guest counts
    running_vms = sum(1 for v in vms if v["status"] == "running")
    running_lxc = sum(1 for c in lxc if c["status"] == "running")
    stopped_vms = len(vms) - running_vms
    stopped_lxc = len(lxc) - running_lxc

    # Services
    services_up    = sum(1 for s in services if s.get("up"))
    services_total = len(services)

    # Storage
    storage_peak_pct  = max((s["pct"] for s in storage), default=0)
    storage_peak_name = next((s["name"] for s in storage if s["pct"] == storage_peak_pct), "")

    # Tailscale
    ts_online = sum(1 for d in tailscale if d.get("online"))

    # Backup coverage
    backed_vmids = {str(b["vmid"]) for b in backups}
    all_vmids    = {str(v["vmid"]) for v in vms + lxc}
    uncovered    = len(all_vmids - backed_vmids)
    ages = [b["age_hours"] for b in backups if b.get("age_hours") is not None]
    oldest_label = next(
        (b["age_label"] for b in backups if b.get("age_hours") == max(ages)), "—"
    ) if ages else "—"

    # CPU/RAM
    node_cores  = node.get("cores", 1)
    node_cpu    = node.get("cpu_pct", 0)
    node_ram    = node.get("mem_total_gb", 1)

    alloc_ram = (
        sum(vm_configs.get(v["vmid"], {}).get("mem_gb", 0) for v in vms) +
        sum(lxc_configs.get(c["vmid"], {}).get("mem_gb", 0) for c in lxc)
    )
    used_ram = round(sum(p.get("used_gb", 0) for p in vm_pressure.values()), 1)

    vm_cpu_cores = sum(
        (v["cpu_pct"] / 100) * vm_configs.get(v["vmid"], {}).get("cores", 1)
        for v in vms if v["status"] == "running"
    )
    vm_cpu_pct  = round(min(vm_cpu_cores / node_cores * 100, node_cpu) if node_cores else 0, 1)
    sys_cpu_pct = round(max(node_cpu - vm_cpu_pct, 0), 1)
    cpu_idle    = round(max(100 - node_cpu, 0), 1)

    return {
        "running_vms":        running_vms,
        "stopped_vms":        stopped_vms,
        "running_lxc":        running_lxc,
        "stopped_lxc":        stopped_lxc,
        "total_vms":          len(vms),
        "total_lxc":          len(lxc),
        "total_guests":       len(vms) + len(lxc),
        "services_up":        services_up,
        "services_down":      services_total - services_up,
        "services_total":     services_total,
        "storage_peak_pct":   storage_peak_pct,
        "storage_peak_name":  storage_peak_name,
        "ts_online":          ts_online,
        "ts_total":           len(tailscale),
        "backup_uncovered":   uncovered,
        "backup_oldest":      oldest_label,
        "vm_cpu_pct":         vm_cpu_pct,
        "sys_cpu_pct":        sys_cpu_pct,
        "cpu_idle_pct":       cpu_idle,
        "used_ram_gb":        used_ram,
        "ram_overhead_gb":    round(max(alloc_ram - used_ram, 0), 1),
        "ram_free_gb":        round(max(node_ram  - alloc_ram, 0), 1),
        "ram_used_pct":       round(used_ram / node_ram * 100, 1) if node_ram else 0,
        "alloc_cores":        sum(vm_configs.get(v["vmid"], {}).get("cores", 0) for v in vms),
        "alloc_ram_gb":       round(alloc_ram, 1),
        "running":            running_vms + running_lxc,
        "stopped":            stopped_vms + stopped_lxc,
    }
