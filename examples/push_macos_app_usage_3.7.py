#!/usr/bin/env python3
"""Render today's macOS app usage on a 3.7-inch BluETag (portrait).

默认行为:
1. 读取 macOS Knowledge 数据库中的当天 App 前台使用时长
2. 过滤掉少于 5 分钟的记录
3. 按使用时长倒序取 Top 5
4. 生成 240x416 的竖向榜单预览图
5. 推送到 3.7 寸设备

示例:
    uv run examples/push_macos_app_usage_3.7.py --preview-only
    uv run examples/push_macos_app_usage_3.7.py --device EPD-D984FADA
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag import build_frame, pack_2bpp, packetize, quantize
from bluetag.ble import BleDependencyError
from bluetag.protocol import parse_mac_suffix
from bluetag.screens import get_screen_profile

DEFAULT_DB = Path.home() / "Library/Application Support/Knowledge/knowledgeC.db"
DEFAULT_OUTPUT = "macos-app-usage-3.7inch.png"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3

APPLE_EPOCH_OFFSET = 978307200
MIN_USAGE_SECONDS = 5 * 60
MAX_ROWS = 5
WIDTH = 240
HEIGHT = 416

MONO_FONT_SEARCH = [
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:\\Windows\\Fonts\\consola.ttf",
]


class MacAppUsageError(RuntimeError):
    """Raised when the script cannot load app usage or push the preview."""


@dataclass
class AppUsageRow:
    app_name: str
    bundle_id: str
    seconds: int
    duration_text: str


@dataclass(frozen=True)
class LayoutSpec:
    app_font_size: int
    stat_font_size: int
    row_height: int
    rank_gap: int
    divider_inset: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 macOS 当天 App 使用时长画成 3.7 寸电子价签样式并推送。",
    )
    parser.add_argument(
        "--device",
        "-d",
        help="设备名，例如 EPD-D984FADA",
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
        "--db-path",
        type=Path,
        default=DEFAULT_DB,
        help=f"Knowledge 数据库路径，默认 {DEFAULT_DB}",
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
    return parser.parse_args()


def unix_now() -> int:
    return int(datetime.now().timestamp())


def local_day_start_unix(now: datetime | None = None) -> int:
    current = now or datetime.now().astimezone()
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day_start.timestamp())


def to_apple_epoch_from_unix(unix_ts: int | float) -> int:
    return int(unix_ts) - APPLE_EPOCH_OFFSET


def load_today_usage_rows(
    db_path: Path,
    *,
    now_unix: int | None = None,
    day_start_unix: int | None = None,
) -> list[tuple[str, int]]:
    db_path = db_path.expanduser()
    if not db_path.exists():
        raise MacAppUsageError(f"Knowledge DB not found: {db_path}")
    if not db_path.is_file():
        raise MacAppUsageError(f"Knowledge DB is not a file: {db_path}")

    effective_now_unix = now_unix if now_unix is not None else unix_now()
    effective_day_start_unix = (
        day_start_unix if day_start_unix is not None else local_day_start_unix()
    )
    now_apple = to_apple_epoch_from_unix(effective_now_unix)
    day_start_apple = to_apple_epoch_from_unix(effective_day_start_unix)

    sql = """
WITH agg AS (
  SELECT
    ZVALUESTRING AS bundle_id,
    CAST(ROUND(SUM(
      CASE
        WHEN MIN(ZENDDATE, ?) > MAX(ZSTARTDATE, ?)
        THEN MIN(ZENDDATE, ?) - MAX(ZSTARTDATE, ?)
        ELSE 0
      END
    )) AS INTEGER) AS seconds
  FROM ZOBJECT
  WHERE ZSTREAMNAME = '/app/usage'
    AND ZVALUESTRING IS NOT NULL
    AND ZENDDATE > ?
    AND ZSTARTDATE < ?
  GROUP BY ZVALUESTRING
)
SELECT bundle_id, seconds
FROM agg
WHERE seconds > 0
ORDER BY seconds DESC, bundle_id ASC
"""

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                sql,
                (
                    now_apple,
                    day_start_apple,
                    now_apple,
                    day_start_apple,
                    day_start_apple,
                    now_apple,
                ),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "authorization denied" in message.lower():
            raise MacAppUsageError(
                "Cannot open macOS Knowledge DB. Grant Full Disk Access to Terminal/iTerm/Codex and try again."
            ) from exc
        raise MacAppUsageError(f"Failed to read Knowledge DB: {exc}") from exc

    return [
        (bundle_id, int(seconds))
        for bundle_id, seconds in rows
        if isinstance(bundle_id, str) and seconds
    ]


@lru_cache(maxsize=256)
def resolve_app_name(bundle_id: str) -> str:
    if sys.platform != "darwin":
        return bundle_id

    script = (
        'tell application "System Events" to return name of application id '
        f'"{bundle_id}"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return bundle_id

    name = result.stdout.strip()
    return name or bundle_id


def format_duration_text(seconds: int) -> str:
    total_minutes = max(0, int(seconds)) // 60
    if total_minutes < 60:
        return f"{total_minutes}m"

    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}h {minutes:02d}m"


def build_top_rows(
    usage_rows: list[tuple[str, int]],
    *,
    name_resolver: Callable[[str], str] = resolve_app_name,
) -> list[AppUsageRow]:
    rows: list[AppUsageRow] = []
    for bundle_id, seconds in usage_rows:
        if int(seconds) < MIN_USAGE_SECONDS:
            continue
        rows.append(
            AppUsageRow(
                app_name=name_resolver(bundle_id),
                bundle_id=bundle_id,
                seconds=int(seconds),
                duration_text=format_duration_text(int(seconds)),
            )
        )

    rows.sort(key=lambda row: (-row.seconds, row.app_name.casefold(), row.bundle_id))
    return rows[:MAX_ROWS]


def load_font(size: int, *, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    if font_path:
        return ImageFont.truetype(font_path, size)
    for path in MONO_FONT_SEARCH:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def measure_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def ellipsize_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    text = text.strip()
    if not text:
        return "-"

    text_width, _ = measure_text(draw, text, font)
    if text_width <= max_width:
        return text

    ellipsis = "..."
    ellipsis_width, _ = measure_text(draw, ellipsis, font)
    if ellipsis_width >= max_width:
        return ellipsis

    trimmed = text
    while trimmed:
        trimmed = trimmed[:-1]
        candidate = f"{trimmed}{ellipsis}"
        candidate_width, _ = measure_text(draw, candidate, font)
        if candidate_width <= max_width:
            return candidate
    return ellipsis


def draw_empty_state(
    draw: ImageDraw.ImageDraw,
    *,
    width: int,
    height: int,
    title_bottom: int,
    body_font: ImageFont.ImageFont,
    detail_font: ImageFont.ImageFont,
):
    primary = "No app >= 5m today"
    secondary = "No data yet or Full Disk Access is missing."

    primary_width, primary_height = measure_text(draw, primary, body_font)
    secondary_width, secondary_height = measure_text(draw, secondary, detail_font)
    center_y = title_bottom + (height - title_bottom) // 2

    draw.text(
        ((width - primary_width) // 2, center_y - primary_height),
        primary,
        fill="black",
        font=body_font,
    )
    draw.text(
        ((width - secondary_width) // 2, center_y + 10),
        secondary,
        fill="black",
        font=detail_font,
    )


def choose_layout(
    draw: ImageDraw.ImageDraw,
    rows: list[AppUsageRow],
    *,
    font_path: str | None = None,
) -> LayoutSpec:
    if not rows:
        return LayoutSpec(
            app_font_size=22,
            stat_font_size=18,
            row_height=56,
            rank_gap=12,
            divider_inset=18,
        )

    duration_samples = [row.duration_text for row in rows[:MAX_ROWS]]
    name_samples = [row.app_name for row in rows[:MAX_ROWS]]
    candidates = [
        LayoutSpec(22, 18, 58, 12, 18),
        LayoutSpec(20, 17, 54, 11, 18),
        LayoutSpec(18, 16, 50, 10, 16),
        LayoutSpec(16, 15, 46, 8, 14),
    ]

    max_rank_width, _ = measure_text(
        draw,
        str(min(MAX_ROWS, len(rows))),
        load_font(12, font_path=font_path),
    )
    left_pad = 18
    right_pad = 18

    for layout in candidates:
        app_font = load_font(layout.app_font_size, font_path=font_path)
        stat_font = load_font(layout.stat_font_size, font_path=font_path)
        duration_width = max(
            measure_text(draw, sample, stat_font)[0] for sample in duration_samples
        )
        app_width = max(measure_text(draw, sample, app_font)[0] for sample in name_samples)
        available_width = (
            WIDTH
            - left_pad
            - right_pad
            - max_rank_width
            - layout.rank_gap
            - duration_width
            - 12
        )
        content_height = layout.row_height * min(MAX_ROWS, len(rows))
        max_content_height = HEIGHT - 124
        if app_width <= available_width and content_height <= max_content_height:
            return layout

    return candidates[-1]


def render_usage_image(
    rows: list[AppUsageRow],
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    font_path: str | None = None,
    now: datetime | None = None,
) -> Image.Image:
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    layout = choose_layout(draw, rows, font_path=font_path)

    now_dt = now or datetime.now().astimezone()
    title_font = load_font(28, font_path=font_path)
    subtitle_font = load_font(13, font_path=font_path)
    rank_font = load_font(13, font_path=font_path)
    app_font = load_font(layout.app_font_size, font_path=font_path)
    stat_font = load_font(layout.stat_font_size, font_path=font_path)
    detail_font = load_font(12, font_path=font_path)

    left_pad = 18
    right_pad = 18
    top_pad = 18
    title_gap = 4
    section_gap = 22

    title_text = "Today"
    subtitle_text = f"{now_dt:%b %d} · App Activity"
    title_width, title_height = measure_text(draw, title_text, title_font)
    subtitle_width, subtitle_height = measure_text(draw, subtitle_text, subtitle_font)

    draw.text(
        ((width - title_width) // 2, top_pad),
        title_text,
        fill="black",
        font=title_font,
    )
    subtitle_y = top_pad + title_height + title_gap
    draw.text(
        ((width - subtitle_width) // 2, subtitle_y),
        subtitle_text,
        fill="black",
        font=subtitle_font,
    )

    divider_y = subtitle_y + subtitle_height + 14
    draw.line((62, divider_y, width - 62, divider_y), fill="black", width=1)
    content_top = divider_y + section_gap

    if not rows:
        draw_empty_state(
            draw,
            width=width,
            height=height,
            title_bottom=content_top,
            body_font=app_font,
            detail_font=detail_font,
        )
        return img

    row_height = layout.row_height
    max_rank_text = str(min(MAX_ROWS, len(rows)))
    rank_width, _ = measure_text(draw, max_rank_text, rank_font)
    app_x = left_pad + rank_width + layout.rank_gap
    stat_right = width - right_pad

    for index, row in enumerate(rows[:MAX_ROWS], start=1):
        row_top = content_top + (index - 1) * row_height
        rank_text = str(index)
        duration_width, _ = measure_text(draw, row.duration_text, stat_font)
        app_baseline_y = row_top + max(0, (row_height - measure_text(draw, row.app_name, app_font)[1]) // 2 - 3)

        draw.text(
            (left_pad, app_baseline_y + 3),
            rank_text,
            fill="black",
            font=rank_font,
        )
        draw.text(
            (stat_right - duration_width, app_baseline_y),
            row.duration_text,
            fill="black",
            font=stat_font,
        )

        app_max_width = stat_right - duration_width - 12 - app_x
        app_text = ellipsize_text(draw, row.app_name, app_font, app_max_width)
        draw.text((app_x, app_baseline_y), app_text, fill="black", font=app_font)

        if index < len(rows[:MAX_ROWS]):
            separator_y = row_top + row_height - 6
            draw.line(
                (
                    left_pad + layout.divider_inset,
                    separator_y,
                    width - right_pad - layout.divider_inset,
                    separator_y,
                ),
                fill="black",
                width=1,
            )

    return img


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


def _on_progress(sent: int, total: int):
    if sent == total:
        print(f"\r✅ 发送完成! ({total} 包)")
    elif sent == 1 or sent % 10 == 0:
        print(f"\r  发送中 {sent}/{total}...", end="", flush=True)


async def push_image_to_37_screen(image: Image.Image, args) -> bool:
    from bluetag.ble import connect_session

    profile = get_screen_profile("3.7inch")
    interval_ms = args.interval or profile.default_interval_ms

    native_img = image.convert("RGB")
    if native_img.size != profile.size:
        native_img = native_img.resize(profile.size, Image.LANCZOS)

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
            f"连接 {target['name']} [3.7inch], "
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


def main() -> int:
    args = parse_args()

    if sys.platform != "darwin":
        print("error: this example only supports macOS.", file=sys.stderr)
        return 2

    try:
        usage_rows = load_today_usage_rows(args.db_path)
        rows = build_top_rows(usage_rows)
        image = render_usage_image(rows, font_path=args.font)
        output_path = save_preview(image, Path(args.output))

        print(f"预览已保存: {output_path}")
        print(f"Knowledge DB: {args.db_path.expanduser()}")

        if rows:
            for row in rows:
                print(f"  {row.app_name}: {row.duration_text} ({row.bundle_id})")
        else:
            print("  No app >= 5m today.")

        if args.preview_only:
            return 0

        try:
            ok = asyncio.run(push_image_to_37_screen(image, args))
        except BleDependencyError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return 0 if ok else 1
    except MacAppUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
