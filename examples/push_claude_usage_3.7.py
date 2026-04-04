#!/usr/bin/env python3
"""Render Claude Code usage on a 3.7-inch e-ink tag (landscape 416×240).

Shows two rows: Current session (5h) and Weekly limits (7d all models).

Usage:
    uv run examples/push_claude_usage.py
    uv run examples/push_claude_usage.py --preview-only
    uv run examples/push_claude_usage.py --input-json sample.json --preview-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag.ble import BleDependencyError
from bluetag import quantize, pack_2bpp, build_frame, packetize
from bluetag.protocol import parse_mac_suffix
from bluetag.screens import get_screen_profile

USAGE_API = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_OUTPUT = "claude-usage-3.7inch.png"
DEFAULT_SCREEN = "3.7inch"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3

WIDTH = 416
HEIGHT = 240

MONO_FONT_SEARCH = [
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:\\Windows\\Fonts\\consola.ttf",
]


@dataclass
class UsageRow:
    label: str
    left_percent: float
    resets_text: str


# ── OAuth Token ───────────────────────────────────────────────────────────────


def _detect_token_from_keychain() -> str | None:
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "usage-elink-oauth", "-w"],
        capture_output=True, text=True,
    ).stdout.strip()
    if raw:
        return raw

    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True,
    ).stdout.strip()
    if raw:
        try:
            return json.loads(raw)["claudeAiOauth"]["accessToken"]
        except Exception:
            pass
    return None


def _load_elink_config_token() -> str | None:
    cfg_path = Path.home() / ".config" / "elink" / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text()).get("oauth_token")
        except Exception:
            pass
    return None


def get_oauth_token() -> str | None:
    return _load_elink_config_token() or _detect_token_from_keychain()


# ── API ───────────────────────────────────────────────────────────────────────


def fetch_usage(token: str, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(
        USAGE_API,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed: {exc.reason}") from exc


# ── Data ──────────────────────────────────────────────────────────────────────


def _fmt_resets(iso: str | None) -> str:
    if not iso:
        return "resets unknown"
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        s = (dt - datetime.now(timezone.utc).astimezone()).total_seconds()
        if s <= 0:
            return "reset"
        h, rem = divmod(int(s), 3600)
        m = rem // 60
        if h < 24:
            return f"resets in {h}h {m}m"
        now = datetime.now().astimezone()
        time_text = dt.strftime("%H:%M")
        if dt.date() == now.date():
            return f"resets {time_text}"
        return f"resets {dt.strftime('%a')} {time_text}"
    except (ValueError, TypeError):
        return "resets unknown"


def build_rows(payload: dict[str, Any]) -> list[UsageRow]:
    fh = payload.get("five_hour") or {}
    sd = payload.get("seven_day") or {}

    rows: list[UsageRow] = []
    for label, section in [("5h limit", fh), ("weekly limit", sd)]:
        util = section.get("utilization", 0) or 0
        left = max(0.0, min(100.0, 100.0 - util))
        rows.append(UsageRow(
            label=label,
            left_percent=left,
            resets_text=_fmt_resets(section.get("resets_at")),
        ))
    return rows


# ── Rendering (same style as push_codex_usage_3.7.py) ────────────────────────


def load_font(size: int, *, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    if font_path:
        return ImageFont.truetype(font_path, size)
    for path in MONO_FONT_SEARCH:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_progress_bar(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    percent: float,
):
    """Draw a bordered progress bar (codex style). percent is 'left'."""
    draw.rectangle((x, y, x + width, y + height), outline="black", width=2)
    inner_x0 = x + 3
    inner_y0 = y + 3
    inner_x1 = x + width - 2
    inner_y1 = y + height - 2
    inner_width = max(0, inner_x1 - inner_x0)
    # percent is "left", so filled = used = 100 - left
    used = max(0.0, min(100.0, 100.0 - percent))
    fill_width = round(inner_width * used / 100.0)

    if fill_width > 0:
        draw.rectangle(
            (inner_x0, inner_y0, inner_x0 + fill_width - 1, inner_y1),
            fill="black",
        )


def render_usage_image(
    rows: list[UsageRow],
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    font_path: str | None = None,
) -> Image.Image:
    """Render usage image for 3.7 inch landscape layout (416x240)."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    title_font = load_font(24, font_path=font_path)
    label_font = load_font(20, font_path=font_path)
    stat_font = load_font(22, font_path=font_path)
    detail_font = load_font(14, font_path=font_path)

    left_pad = 20
    right_pad = 20
    top_pad = 15
    bottom_pad = 15
    title_gap = 28
    gap = 25

    title_text = "CC Usage"
    title_bbox = draw.textbbox((0, 0), title_text, font=title_font)
    title_w = title_bbox[2] - title_bbox[0]
    title_h = title_bbox[3] - title_bbox[1]
    tx = (width - title_w) // 2
    ty = top_pad
    # Bold by drawing with 1px offsets
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            draw.text((tx + dx, ty + dy), title_text, fill="black", font=title_font)

    rows_top = top_pad + title_h + title_gap
    row_count = max(1, len(rows))
    row_height = (height - rows_top - bottom_pad - gap * (row_count - 1)) // row_count

    for idx, row in enumerate(rows):
        row_top = rows_top + idx * (row_height + gap)
        percent_text = f"{int(round(row.left_percent))}% left"

        label_bbox = draw.textbbox((0, 0), row.label, font=label_font)
        percent_bbox = draw.textbbox((0, 0), percent_text, font=stat_font)
        label_h = label_bbox[3] - label_bbox[1]
        percent_w = percent_bbox[2] - percent_bbox[0]

        draw.text((left_pad, row_top), row.label, fill="black", font=label_font)
        draw.text(
            (width - right_pad - percent_w, row_top - 2),
            percent_text,
            fill="black",
            font=stat_font,
        )

        bar_y = row_top + label_h + 8
        bar_h = 20
        draw_progress_bar(
            draw,
            x=left_pad,
            y=bar_y,
            width=width - left_pad - right_pad - 1,
            height=bar_h,
            percent=row.left_percent,
        )

        detail_bbox = draw.textbbox((0, 0), row.resets_text, font=detail_font)
        detail_w = detail_bbox[2] - detail_bbox[0]
        draw.text(
            (width - right_pad - detail_w, bar_y + bar_h + 8),
            row.resets_text,
            fill="black",
            font=detail_font,
        )

    return img


# ── BLE push ──────────────────────────────────────────────────────────────────


def _save_device(device: dict, profile):
    profile.cache_path.write_text(f"{device['name']}\n{device['address']}\n")


def _load_device(profile) -> dict | None:
    if not profile.cache_path.exists():
        return None
    lines = profile.cache_path.read_text().strip().splitlines()
    if len(lines) >= 2:
        return {"name": lines[0], "address": lines[1]}
    return None


async def _find_target(args, profile) -> dict | None:
    from bluetag.ble import find_device

    cached = None
    search_name = args.device
    search_address = getattr(args, "address", None)
    if not search_name and not search_address:
        cached = _load_device(profile)
        if cached:
            print(
                f"使用 {profile.name} 缓存设备: "
                f"{cached['name']} ({cached['address']})"
            )
            search_name = cached["name"]
            search_address = cached["address"]

    print(f"扫描 {profile.name} 设备 ({profile.device_prefix}*, {args.scan_timeout:.1f}s/次)...")
    target = await find_device(
        device_name=search_name,
        device_address=search_address,
        timeout=args.scan_timeout,
        scan_retries=DEFAULT_SCAN_RETRIES,
        prefixes=(profile.device_prefix,),
    )
    if target:
        _save_device(target, profile)
        return target

    if cached:
        print("未扫描到缓存设备，改为搜索任意同型号设备...")
        target = await find_device(
            timeout=args.scan_timeout,
            scan_retries=DEFAULT_SCAN_RETRIES,
            prefixes=(profile.device_prefix,),
        )
        if target:
            _save_device(target, profile)
            return target

    return None


def _on_progress(sent: int, total: int):
    if sent == total:
        print(f"\r✅ 发送完成! ({total} 包)")
    elif sent == 1 or sent % 10 == 0:
        print(f"\r  发送中 {sent}/{total}...", end="", flush=True)


def prepare_landscape_image_for_37_screen(
    image: Image.Image,
    profile,
) -> Image.Image:
    if image.size != (WIDTH, HEIGHT):
        image = image.convert("RGB").resize((WIDTH, HEIGHT), Image.LANCZOS)
    else:
        image = image.convert("RGB")

    native = image.transpose(Image.Transpose.ROTATE_270)
    if native.size != profile.size:
        native = native.resize(profile.size, Image.LANCZOS)
    return native


async def push_image_to_37_screen(image: Image.Image, args) -> bool:
    from bluetag.ble import connect_session

    profile = get_screen_profile(args.screen)
    interval_ms = args.interval or profile.default_interval_ms

    native_img = prepare_landscape_image_for_37_screen(image, profile)
    indices = quantize(native_img, flip=profile.mirror, size=profile.size)
    data_2bpp = pack_2bpp(indices)

    target = await _find_target(args, profile)
    if not target:
        print("❌ 未找到设备")
        return False

    mac_suffix = parse_mac_suffix(target["name"])
    frame = build_frame(mac_suffix, data_2bpp)
    packets = packetize(frame)

    session = await connect_session(
        target.get("_ble_device") or target["address"],
        timeout=20.0,
        connect_retries=DEFAULT_CONNECT_RETRIES,
    )
    if not session:
        print("❌ 连接设备失败")
        return False

    try:
        print(
            f"连接 {target['name']} [{profile.name}], "
            f"帧数据 {len(frame)} bytes, {len(packets)} 包"
        )

        total = len(packets)
        for index, packet in enumerate(packets, start=1):
            await session.write(packet, response=False)
            await asyncio.sleep(interval_ms / 1000.0)
            _on_progress(index, total)

        return True
    except Exception as exc:
        print(f"\n❌ 发送失败: {exc}")
        return False
    finally:
        await session.close()


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push Claude Code usage to 3.7-inch e-ink tag (landscape).",
    )
    parser.add_argument("--screen", default=DEFAULT_SCREEN)
    parser.add_argument("--device", "-d", help="Device name e.g. EPD-7F1C654B")
    parser.add_argument("--address", "-a", help="Device BLE address")
    parser.add_argument("--interval", "-i", type=int, help="Packet interval (ms)")
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--input-json", type=Path, help="Read usage from local JSON")
    parser.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    parser.add_argument("--font", help="Custom monospace font path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Load data
    if args.input_json:
        payload = json.loads(args.input_json.read_text())
        source = f"file:{args.input_json}"
    else:
        token = get_oauth_token()
        if not token:
            print("❌ No OAuth token found.", file=sys.stderr)
            print("   Run: claude auth login", file=sys.stderr)
            return 1
        payload = fetch_usage(token)
        source = USAGE_API

    rows = build_rows(payload)
    image = render_usage_image(rows, font_path=args.font)
    image.save(args.output)
    print(f"预览已保存: {args.output}")
    print(f"数据来源: {source}")
    for row in rows:
        print(f"  {row.label}: {int(round(row.left_percent))}% left, {row.resets_text}")

    if args.preview_only:
        return 0

    try:
        ok = asyncio.run(push_image_to_37_screen(image, args))
    except BleDependencyError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
