#!/usr/bin/env python3
"""Render BTC/ETH real-time prices in a compact /stats-like layout for 2.13-inch tags.

默认行为:
1. 请求币安 Market Data API 获取 BTCUSDT / ETHUSDT 当前价格
2. 生成 250x122 的价格面板
3. 保存预览图
4. 推送到 2.13 寸设备

示例:
    uv run examples/push_crypto_binance_price.py --preview-only
    uv run examples/push_crypto_binance_price.py --device EDP-F3F4F5F6
    uv run examples/push_crypto_binance_price.py --loop
    uv run examples/push_crypto_binance_price.py --loop --interval-sec 30
    uv run examples/push_crypto_binance_price.py --preview-only --mock --loop
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag.ble import BleDependencyError
from bluetag.image import layer_to_bytes, process_bicolor_image
from bluetag.screens import get_screen_profile
from bluetag.transfer import send_bicolor_image

DEFAULT_OUTPUT = "crypto-price-2.13inch.png"
DEFAULT_SCREEN = "2.13inch"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3

BINANCE_API_URL = "https://data-api.binance.vision/api/v3/ticker/24hr"

MONO_FONT_SEARCH = [
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:\\Windows\\Fonts\\consola.ttf",
]


class CryptoPriceError(RuntimeError):
    """Raised when the script cannot load credentials or fetch prices."""


@dataclass
class PriceRow:
    symbol: str        # e.g. "BTC-USDT", "ETH-USDT"
    price: str         # formatted price string
    change_24h: float  # 24h change percent
    ts: int | None = None  # Unix timestamp in ms from Binance API


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 BTC/ETH 价格画成 2.13 寸电子价签样式并推送。",
    )
    parser.add_argument(
        "--screen",
        default=DEFAULT_SCREEN,
        help="屏幕尺寸，默认 2.13inch",
    )
    parser.add_argument(
        "--device",
        "-d",
        help="设备名，例如 EDP-F3F4F5F6",
    )
    parser.add_argument(
        "--address",
        "-a",
        help="设备 BLE 地址，优先于 --device",
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        help="包间隔 (ms，默认按屏幕选择)",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="只生成图片，不推送",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"预览图输出路径，默认 {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--env-path",
        type=Path,
        help="覆盖 .env 路径，默认 .env",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=DEFAULT_SCAN_TIMEOUT,
        help=f"BLE 单次扫描超时秒数，默认 {DEFAULT_SCAN_TIMEOUT}",
    )
    parser.add_argument(
        "--font",
        help="自定义等宽字体路径",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="使用模拟数据（用于无网络时测试）",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="循环模式：推送后等待 --interval-sec 秒，再次获取并推送",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=90,
        help="循环模式下每次等待的秒数，默认 90",
    )
    return parser.parse_args()


def fetch_mock_prices() -> list[PriceRow]:
    """Return mock data for offline testing."""
    return [
        PriceRow(symbol="BTC-USDT", price="$67,432", change_24h=2.34),
        PriceRow(symbol="ETH-USDT", price="$3,521", change_24h=-1.08),
    ]


def fetch_crypto_prices() -> list[PriceRow]:
    import urllib.request
    import json

    symbols = ["BTCUSDT","ETHUSDT"]
    url = f"{BINANCE_API_URL}?symbols={json.dumps(symbols, separators=(',', ':'))}"
    # print(f"从以下地址获取数据：{url}")

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        raise CryptoPriceError(f"Network error fetching prices: {exc}") from exc

    if not isinstance(data, list):
        raise CryptoPriceError(f"Unexpected Binance API response: {data}")

    rows: list[PriceRow] = []
    for item in data:
        symbol = item.get("symbol", "")
        # Binance uses BTCUSDT, display with hyphen
        display_symbol = symbol
        last_price = item.get("lastPrice", "0")
        price_change_percent = float(item.get("priceChangePercent", "0"))
        open_time = item.get("closeTime")

        try:
            last_f = float(last_price)
            price_fmt = f"${last_f:,.0f}" if last_f >= 1 else f"${last_f:.4f}"
        except (ValueError, ZeroDivisionError):
            last_f = 0.0
            price_fmt = "$0"

        # print(f"{display_symbol}: {item}")
        rows.append(PriceRow(
            symbol=display_symbol,
            price=price_fmt,
            change_24h=price_change_percent,
            ts=open_time,
        ))

    return rows


def load_font(size: int, *, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    if font_path:
        return ImageFont.truetype(font_path, size)
    for path in MONO_FONT_SEARCH:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _new_crisp_canvas(
    width: int, height: int
) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Render in 1-bit mode first to avoid grayscale edges on the e-ink panel."""
    img = Image.new("1", (width, height), 1)
    draw = ImageDraw.Draw(img)
    draw.fontmode = "1"
    return img, draw


def _format_ts(ts_ms: int | None) -> str:
    """Format a Unix timestamp in ms to YYYY-MM-DD HH:MM:SS local time."""
    if ts_ms is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        ts = int(ts_ms)
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def render_price_image(
    rows: list[PriceRow],
    *,
    width: int = 250,
    height: int = 122,
    font_path: str | None = None,
) -> Image.Image:
    img, draw = _new_crisp_canvas(width, height)

    price_font = load_font(22, font_path=font_path)
    change_font = load_font(12, font_path=font_path)
    time_font = load_font(10, font_path=font_path)

    left_pad = 8
    top_pad = 4

    # Timestamp from first row's ts field (top-right)
    ts_ms = rows[0].ts if rows else None
    time_text = _format_ts(ts_ms)
    time_bbox = draw.textbbox((0, 0), time_text, font=time_font)
    draw.text((width - left_pad - (time_bbox[2] - time_bbox[0]), top_pad), time_text, fill=0, font=time_font)

    row_top = top_pad + (time_bbox[3] - time_bbox[1]) + 10
    row_height = (height - row_top - 4) // len(rows)

    for idx, row in enumerate(rows):
        y = row_top + idx * row_height

        # Symbol (left-aligned)
        draw.text((left_pad, y), row.symbol, fill=0, font=price_font)

        # Price (right-aligned, prominent)
        price_bbox = draw.textbbox((0, 0), row.price, font=price_font)
        price_w = price_bbox[2] - price_bbox[0]
        draw.text((width - left_pad - price_w, y), row.price, fill=0, font=price_font)

        # 24h change (below price on the right)
        arrow = "▲" if row.change_24h >= 0 else "▼"
        change_text = f"{arrow} {abs(row.change_24h):.2f}%"
        change_bbox = draw.textbbox((0, 0), change_text, font=change_font)
        draw.text(
            (width - left_pad - (change_bbox[2] - change_bbox[0]), y + (price_bbox[3] - price_bbox[1]) + 3),
            change_text,
            fill=0,
            font=change_font,
        )

    # source label (bottom-left)
    source_text = "data-api.binance.vision"
    source_bbox = draw.textbbox((0, 0), source_text, font=time_font)
    draw.text((left_pad, height - (source_bbox[3] - source_bbox[1]) - 4), source_text, fill=0, font=time_font)

    return img.convert("RGB")


def save_preview(image: Image.Image, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


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
    search_address = args.address
    if not search_name and not search_address:
        cached = _load_device(profile)
        if cached:
            print(
                f"使用 {profile.name} 缓存设备作为扫描目标: "
                f"{cached['name']} ({cached['address']})"
            )
            search_name = cached["name"]
            search_address = cached["address"]

    print(
        f"扫描 {profile.name} 设备 "
        f"({profile.device_prefix}*, {args.scan_timeout:.1f}s/次)..."
    )
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


def _layer_progress(layer_name: str, sent: int, total: int):
    if sent == total:
        print(f"\r✅ {layer_name}发送完成! ({total} 包)")
    elif sent == 1 or sent % 10 == 0:
        print(f"\r  {layer_name}发送中 {sent}/{total}...", end="", flush=True)


async def push_image_to_small_screen(image: Image.Image, args) -> bool:
    from bluetag.ble import connect_session

    profile = get_screen_profile(args.screen)
    interval_ms = args.interval or profile.default_interval_ms

    black_layer, red_layer, _preview = process_bicolor_image(
        image,
        profile.name,
        threshold=128,
        dither=False,
        rotate=profile.rotate,
        mirror=profile.mirror,
        swap_wh=profile.swap_wh,
        detect_red=profile.detect_red,
    )
    black_data = layer_to_bytes(black_layer, profile.encoding)
    red_data = layer_to_bytes(red_layer, profile.encoding)

    target = await _find_target(args, profile)
    if not target:
        print("未找到设备")
        return False

    session = await connect_session(
        target.get("_ble_device") or target["address"],
        timeout=20.0,
        connect_retries=DEFAULT_CONNECT_RETRIES,
    )
    if not session:
        print("连接设备失败")
        return False

    try:
        print(
            f"连接 {target['name']} [{profile.name}], "
            f"黑层 {len(black_data)} bytes, 红层 {len(red_data)} bytes"
        )
        ok = await send_bicolor_image(
            session,
            black_data,
            red_data,
            delay_ms=interval_ms,
            settle_ms=profile.settle_ms,
            flush_every=profile.flush_every,
            on_progress=_layer_progress,
        )
        if not ok:
            print("发送失败")
        return ok
    finally:
        try:
            await session.close()
        except Exception as e:
            print(f"关闭连接时出错: {e}")


_last_interrupt_time = 0.0


def _handle_interrupt(signum, frame):
    global _last_interrupt_time
    now = time.monotonic()
    if now - _last_interrupt_time < 2.0:
        print("\n⚠️ 确认退出...", flush=True)
        raise KeyboardInterrupt("user requested exit")
    else:
        print("\n⚠️ Ctrl+C 再按一次确认退出 (2秒内有效)", flush=True)
    _last_interrupt_time = now


async def run_loop(args, profile) -> int:
    """Fetch, render, push — then repeat every --interval-sec seconds."""
    loop_count = 0
    while True:
        loop_count += 1
        print(f"\n[{loop_count}] 获取价格数据...")

        try:
            if args.mock:
                rows = fetch_mock_prices()
            else:
                rows = fetch_crypto_prices()
        except CryptoPriceError as exc:
            print(f" 获取失败: {exc}")
            print(f" 1 秒后重试...")
            await asyncio.sleep(1)
            continue

        image = render_price_image(
            rows,
            width=profile.width,
            height=profile.height,
            font_path=args.font,
        )

        if args.preview_only:
            output_path = save_preview(image, Path(args.output))
            print(f"  预览已保存: {output_path}")
            for row in rows:
                arrow = "▲" if row.change_24h >= 0 else "▼"
                print(f"    {row.symbol}: {row.price} ({arrow}{abs(row.change_24h):.2f}%)")
        else:
            ok = await push_image_to_small_screen(image, args)
            if not ok:
                print(f" 推送失败，{args.interval_sec} 秒后重试...")
                await asyncio.sleep(args.interval_sec)
                continue

            for row in rows:
                arrow = "▲" if row.change_24h >= 0 else "▼"
                print(f"    {row.symbol}: {row.price} ({arrow}{abs(row.change_24h):.2f}%)")

        if not args.loop:
            return 0

        print(f"  ⏳ {args.interval_sec} 秒后再次刷新... (Ctrl+C 取消)")
        try:
            await asyncio.sleep(args.interval_sec)
        except asyncio.CancelledError:
            print("\n⚠️ 取消刷新")
            return 0


def main() -> int:
    args = parse_args()
    profile = get_screen_profile(args.screen)
    if profile.name != "2.13inch":
        print(
            "当前脚本只为2.13寸布局设计，请使用 --screen 2.13inch",
            file=sys.stderr,
        )
        return 2

    signal.signal(signal.SIGINT, _handle_interrupt)
    try:
        result = asyncio.run(run_loop(args, profile))
        return result
    except KeyboardInterrupt:
        print("\n 已退出")
        return 0
    except CryptoPriceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
