import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.middleware.base import BaseHTTPMiddleware

from app import config, state
from app.poller import poll_once, start_polling

templates = Jinja2Templates(directory="app/templates")

PAGES = ["overview", "vms", "storage", "backups", "network", "pihole", "settings"]

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "argus")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
SESSION_COOKIE = "argus_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

_signer = URLSafeTimedSerializer(SECRET_KEY)

PUBLIC_PATHS = {"/login"}


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
        if request.url.path in PUBLIC_PATHS:
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


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, password: str = Form(...)):
    if password == DASHBOARD_PASSWORD:
        token = _make_session_token()
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Incorrect password"}, status_code=401
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/status")
async def api_status():
    return state.get()


@app.get("/page/{name}", response_class=HTMLResponse)
async def page(name: str, request: Request):
    if name not in PAGES:
        return HTMLResponse("<p>Page not found</p>", status_code=404)
    if name == "settings":
        cfg = config.load()
        return templates.TemplateResponse("page_settings.html", {
            "request":        request,
            "services_cfg":   cfg["services"],
            "device_lan_map": cfg.get("device_lan_map", {}),
        })
    data = state.get()
    if name == "overview":
        data["totals"] = _compute_totals(data)
    return templates.TemplateResponse(f"page_{name}.html", {"request": request, **data})


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


def _compute_totals(data: dict) -> dict:
    vms     = data.get("vms", [])
    configs = data.get("vm_configs", {})
    node    = data.get("node", {})

    alloc_cores = sum(configs.get(vm["vmid"], {}).get("cores", 0) for vm in vms)
    alloc_ram   = sum(configs.get(vm["vmid"], {}).get("mem_gb", 0) for vm in vms)
    used_ram    = sum(vm["mem_used_gb"] for vm in vms)
    used_cpu    = round(sum(vm["cpu_pct"] for vm in vms) / len(vms), 1) if vms else 0

    node_cores  = node.get("cores", 1)
    node_ram    = node.get("mem_total_gb", 1)

    services       = data.get("services", [])
    services_up    = sum(1 for s in services if s.get("up"))
    services_total = len(services)

    node_cpu = node.get("cpu_pct", 0)
    # Convert per-VM CPU (fraction of its own vCPUs) to host-normalised percentage
    vm_cpu_cores = sum(
        (vm["cpu_pct"] / 100) * configs.get(vm["vmid"], {}).get("cores", 1)
        for vm in vms if vm["status"] == "running"
    )
    vm_cpu_pct  = round(min(vm_cpu_cores / node_cores * 100, node_cpu) if node_cores else 0, 1)
    sys_cpu_pct = round(max(node_cpu - vm_cpu_pct, 0), 1)
    cpu_idle    = round(max(100 - node_cpu, 0), 1)

    return {
        "alloc_cores":      alloc_cores,
        "alloc_ram_gb":     round(alloc_ram, 1),
        "used_ram_gb":      round(used_ram, 1),
        "avg_cpu_pct":      used_cpu,
        "node_cores":       node_cores,
        "node_ram_gb":      node_ram,
        "cores_pct":        round(alloc_cores / node_cores * 100, 1) if node_cores else 0,
        "ram_alloc_pct":    round(alloc_ram   / node_ram   * 100, 1) if node_ram   else 0,
        "ram_used_pct":     round(used_ram    / node_ram   * 100, 1) if node_ram   else 0,
        "ram_overhead_gb":  round(max(alloc_ram - used_ram, 0), 1),
        "ram_free_gb":      round(max(node_ram  - alloc_ram, 0), 1),
        "vm_cpu_pct":       vm_cpu_pct,
        "sys_cpu_pct":      sys_cpu_pct,
        "cpu_idle_pct":     cpu_idle,
        "running":          sum(1 for vm in vms if vm["status"] == "running"),
        "stopped":          sum(1 for vm in vms if vm["status"] != "running"),
        "services_up":      services_up,
        "services_down":    services_total - services_up,
        "services_total":   services_total,
    }
