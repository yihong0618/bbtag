"""
BluETag Server — BLE 图像推送 API 服务

运行在 BLE 主机上 (Mac Mini / 树莓派)，暴露 REST API 供远程调用。
"""

import asyncio
import hmac
import io
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic_settings import BaseSettings

from bluetag import __version__, build_frame, pack_2bpp, packetize, quantize
from bluetag.ble import connect_session
from bluetag.ble import push as ble_push
from bluetag.ble import scan as ble_scan
from bluetag.image import indices_to_image, layer_to_bytes, process_bicolor_image
from bluetag.protocol import parse_mac_suffix
from bluetag.screens import SCREEN_PROFILES, ScreenProfile, get_screen_profile
from bluetag.text import render_text
from bluetag.transfer import send_bicolor_image


class Settings(BaseSettings):
    api_token: str = ""
    cors_origins: str = "*"
    scan_interval: int = 60  # 自动扫描间隔 (秒)
    packet_interval: int = 50  # BLE 包间隔 (ms)
    host: str = "0.0.0.0"
    port: int = 8090
    serve_web: bool = False  # 默认不挂前端 (保持原 API-only 行为); BLUETAG_SERVE_WEB=1 启用

    class Config:
        env_prefix = "BLUETAG_"
        env_file = ".env"


settings = Settings()

# 设备缓存
device_cache: dict[str, dict] = {}  # {name: {name, address, rssi, last_seen}}
cache_lock = asyncio.Lock()


def _prefix_to_screen(name: str) -> str | None:
    """根据设备名前缀反查屏幕型号。"""
    for profile in SCREEN_PROFILES.values():
        if name.startswith(profile.device_prefix):
            return profile.name
    return None


def _public_fields(d: dict) -> dict:
    """剥掉下划线开头的内部字段 (如 _ble_device)，避免 JSON 序列化失败。"""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _resolve_screen(screen: str | None) -> ScreenProfile | None:
    """解析 screen 参数，无效抛 400，未传返回 None。"""
    if not screen:
        return None
    try:
        return get_screen_profile(screen)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


async def periodic_scan():
    """后台定期扫描 BLE 设备"""
    while True:
        try:
            devices = await ble_scan(timeout=10.0)
            async with cache_lock:
                now = time.time()
                for d in devices:
                    device_cache[d["name"]] = {**d, "last_seen": now}
        except Exception:
            pass
        await asyncio.sleep(settings.scan_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic_scan())
    yield
    task.cancel()


app = FastAPI(
    title="BluETag Server",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Auth ────────────────────────────────────────────────


def verify_token(request: Request):
    if not settings.api_token:
        raise HTTPException(503, "API token not configured")
    token = request.headers.get("X-API-Token") or request.query_params.get("token")
    if not token or not hmac.compare_digest(token, settings.api_token):
        raise HTTPException(401, "Invalid API token")


# ─── Routes ──────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "devices": len(device_cache),
    }


@app.get("/api/v1/devices")
async def list_devices(
    request: Request,
    screen: str | None = Query(None, description="可选: 按屏幕型号过滤设备"),
):
    verify_token(request)
    profile = _resolve_screen(screen)
    async with cache_lock:
        now = time.time()
        devices = []
        for d in device_cache.values():
            if profile and not d["name"].startswith(profile.device_prefix):
                continue
            devices.append(
                {
                    **_public_fields(d),
                    "screen": _prefix_to_screen(d["name"]),
                    "online": (now - d["last_seen"]) < settings.scan_interval * 2,
                }
            )
    return {"items": devices, "total": len(devices)}


@app.post("/api/v1/devices/scan")
async def trigger_scan(
    request: Request,
    screen: str | None = Query(None, description="可选: 只扫指定屏幕的设备"),
):
    verify_token(request)
    profile = _resolve_screen(screen)
    prefixes = (profile.device_prefix,) if profile else None
    devices = await ble_scan(timeout=10.0, prefixes=prefixes)
    async with cache_lock:
        now = time.time()
        for d in devices:
            device_cache[d["name"]] = {**d, "last_seen": now}
    enriched = [
        {**_public_fields(d), "screen": _prefix_to_screen(d["name"])} for d in devices
    ]
    return {"items": enriched, "total": len(enriched)}


# ─── Push / Preview helpers ──────────────────────────────


def _render_text_image(
    *,
    body: str,
    title: str | None,
    title_color: str,
    body_color: str,
    separator_color: str,
    align: str,
    font: str | None,
    screen: str,
) -> Image.Image:
    return render_text(
        body=body.replace("\\n", "\n"),
        title=title,
        title_color=title_color,
        body_color=body_color,
        separator_color=separator_color,
        font_path=font,
        align=align,
        screen=screen,
    )


def _build_image_from_inputs(
    *,
    file_bytes: bytes | None,
    body: str | None,
    title: str | None,
    title_color: str,
    body_color: str,
    separator_color: str,
    align: str,
    font: str | None,
    screen_name: str,
) -> Image.Image:
    """根据 file 或 text 参数构建 PIL.Image。两者必须二选一。"""
    has_file = file_bytes is not None and len(file_bytes) > 0
    has_text = body is not None and body != ""
    if has_file and has_text:
        raise HTTPException(400, "提供 file 或文字字段，不能同时给两者")
    if not has_file and not has_text:
        raise HTTPException(400, "必须提供 file 或 body 文字字段之一")

    if has_file:
        try:
            return Image.open(io.BytesIO(file_bytes))
        except Exception:
            raise HTTPException(400, "Invalid image file")

    return _render_text_image(
        body=body,
        title=title,
        title_color=title_color,
        body_color=body_color,
        separator_color=separator_color,
        align=align,
        font=font,
        screen=screen_name,
    )


def _build_frame_payload(img: Image.Image, profile: ScreenProfile) -> tuple[Image.Image, bytes]:
    indices = quantize(img, flip=profile.mirror, size=profile.size)
    preview = indices_to_image(indices, size=profile.size)
    return preview, pack_2bpp(indices)


def _build_layer_payload(
    img: Image.Image, profile: ScreenProfile
) -> tuple[Image.Image, bytes, bytes]:
    black_layer, red_layer, preview = process_bicolor_image(
        img,
        profile.name,
        threshold=128,
        dither=False,
        rotate=profile.rotate,
        mirror=profile.mirror,
        swap_wh=profile.swap_wh,
        detect_red=profile.detect_red,
    )
    return (
        preview,
        layer_to_bytes(black_layer, profile.encoding),
        layer_to_bytes(red_layer, profile.encoding),
    )


async def _resolve_target(device: str | None, profile: ScreenProfile) -> dict:
    async with cache_lock:
        if device:
            target = device_cache.get(device)
            if not target:
                raise HTTPException(404, f"Device '{device}' not found")
            if not target["name"].startswith(profile.device_prefix):
                raise HTTPException(
                    400,
                    f"Device '{device}' 不属于 {profile.name} (前缀 {profile.device_prefix})",
                )
            return target

        online = [
            d
            for d in device_cache.values()
            if d["name"].startswith(profile.device_prefix)
            and (time.time() - d["last_seen"]) < settings.scan_interval * 2
        ]
        if not online:
            raise HTTPException(404, f"No online {profile.name} devices")
        return online[0]


def _orient_for_preview(preview: Image.Image, profile: ScreenProfile) -> Image.Image:
    """前端展示时把 mirror 翻转回正向。"""
    if profile.mirror:
        return preview.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    return preview


# ─── Push / Preview routes ───────────────────────────────


@app.post("/api/v1/push")
async def push_endpoint(
    request: Request,
    file: UploadFile | None = File(None),
    device: str | None = Query(
        None, description="设备名 (如 EPD-EBB9D76B)，不指定则推送到第一个在线设备"
    ),
    screen: str | None = Query(None, description="屏幕型号，默认 3.7inch"),
    body: str | None = Form(None),
    title: str | None = Form(None),
    title_color: str = Form("red"),
    body_color: str = Form("black"),
    separator_color: str = Form("yellow"),
    align: str = Form("left"),
    font: str | None = Form(None),
):
    verify_token(request)

    profile = _resolve_screen(screen) or get_screen_profile(None)

    file_bytes = await file.read() if file is not None else None
    img = _build_image_from_inputs(
        file_bytes=file_bytes,
        body=body,
        title=title,
        title_color=title_color,
        body_color=body_color,
        separator_color=separator_color,
        align=align,
        font=font,
        screen_name=profile.name,
    )

    target = await _resolve_target(device, profile)

    if profile.transport == "frame":
        _, data_2bpp = _build_frame_payload(img, profile)
        mac_suffix = parse_mac_suffix(target["name"])
        frame = build_frame(mac_suffix, data_2bpp)
        packets = packetize(frame)
        ok = await ble_push(
            packets,
            device_address=target["address"],
            packet_interval=settings.packet_interval / 1000,
            prefixes=(profile.device_prefix,),
        )
        if not ok:
            raise HTTPException(502, "BLE push failed")
        return {
            "status": "ok",
            "device": target["name"],
            "screen": profile.name,
            "transport": "frame",
            "packets": len(packets),
            "frame_size": len(frame),
        }

    # layer transport (e.g. 2.13inch)
    _, black_data, red_data = _build_layer_payload(img, profile)
    session = await connect_session(target["address"], timeout=20.0)
    if not session:
        raise HTTPException(502, "BLE connect failed")
    try:
        ok = await send_bicolor_image(
            session,
            black_data,
            red_data,
            delay_ms=profile.default_interval_ms,
            settle_ms=profile.settle_ms,
            flush_every=profile.flush_every,
        )
    finally:
        await session.close()

    if not ok:
        raise HTTPException(502, "BLE push failed")
    return {
        "status": "ok",
        "device": target["name"],
        "screen": profile.name,
        "transport": "layer",
        "black_size": len(black_data),
        "red_size": len(red_data),
    }


@app.post("/api/v1/preview")
async def preview_endpoint(
    request: Request,
    file: UploadFile | None = File(None),
    screen: str | None = Query(None, description="屏幕型号，默认 3.7inch"),
    body: str | None = Form(None),
    title: str | None = Form(None),
    title_color: str = Form("red"),
    body_color: str = Form("black"),
    separator_color: str = Form("yellow"),
    align: str = Form("left"),
    font: str | None = Form(None),
):
    verify_token(request)

    profile = _resolve_screen(screen) or get_screen_profile(None)

    file_bytes = await file.read() if file is not None else None
    img = _build_image_from_inputs(
        file_bytes=file_bytes,
        body=body,
        title=title,
        title_color=title_color,
        body_color=body_color,
        separator_color=separator_color,
        align=align,
        font=font,
        screen_name=profile.name,
    )

    if profile.transport == "frame":
        preview, _ = _build_frame_payload(img, profile)
    else:
        preview, _, _ = _build_layer_payload(img, profile)

    preview = _orient_for_preview(preview, profile)
    buf = io.BytesIO()
    preview.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


# ─── Static frontend (optional) ──────────────────────────

_web_dir = Path(__file__).parent / "web"
if settings.serve_web and _web_dir.exists():
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
