"""
文字渲染模块 — 自动排版生成适用于蓝签电子墨水屏的图像
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from bluetag.screens import get_screen_profile

# 颜色
COLOR_BLACK = (0, 0, 0)
COLOR_WHITE = (255, 255, 255)
COLOR_YELLOW = (255, 255, 0)
COLOR_RED = (255, 0, 0)

COLOR_MAP = {
    "black": COLOR_BLACK,
    "white": COLOR_WHITE,
    "yellow": COLOR_YELLOW,
    "red": COLOR_RED,
}

FONTS_PATH = Path(__file__).parent / "fonts"

REGULAR_FONT = FONTS_PATH / "AlibabaPuHuiTi-Regular.ttf"
BOLD_FONT = FONTS_PATH / "AlibabaPuHuiTi-Bold.ttf"


def _find_font(
    size: int, bold: bool = False, font_path: str | None = None
) -> ImageFont.FreeTypeFont:
    """查找可用字体，返回指定大小的 FreeTypeFont。"""
    if font_path:
        return ImageFont.truetype(font_path, size)
    path = BOLD_FONT if bold else REGULAR_FONT
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """将文本按像素宽度自动换行，支持中英文混排。"""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        line = ""
        for char in paragraph:
            test = line + char
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_width:
                if line:
                    lines.append(line)
                line = char
            else:
                line = test
        if line:
            lines.append(line)
    return lines


def _calc_text_height(
    lines: list[str],
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    spacing: int,
) -> int:
    """计算多行文本总高度。"""
    if not lines:
        return 0
    total = 0
    for line in lines:
        text = line or " "
        bbox = draw.textbbox((0, 0), text, font=font)
        total += bbox[3] - bbox[1]
    total += spacing * (len(lines) - 1)
    return total


def _layout_metrics(width: int, height: int, top_safe_px: int = 0) -> dict[str, int]:
    return {
        "padding_x": max(10, width // 17),
        "padding_y": max(8, height // 26) + top_safe_px,
        "line_spacing": max(3, height // 70),
        "title_body_gap": max(8, height // 22),
        "separator_height": max(1, height // 138),
        "separator_inset": max(8, width // 24),
        "body_blank_gap": max(6, height // 10),
        "title_max": min(40, max(18, min(width // 6, height // 5))),
        "title_min": max(14, min(width // 16, height // 9)),
        "body_max": min(30, max(16, min(width // 7, height // 4))),
        "body_min": max(10, min(width // 20, height // 12)),
    }


def render_text(
    body: str,
    title: str | None = None,
    title_color: str = "red",
    body_color: str = "black",
    bg_color: str = "white",
    separator_color: str = "yellow",
    title_size: int | None = None,
    body_size: int | None = None,
    font_path: str | None = None,
    align: str = "left",
    screen: str = "3.7inch",
) -> Image.Image:
    """
    将文字渲染为指定屏幕尺寸的 4 色图像，自动排版。
    """
    profile = get_screen_profile(screen)
    width, height = profile.size
    metrics = _layout_metrics(width, height, top_safe_px=profile.top_safe_px)

    bg_rgb = COLOR_MAP.get(bg_color, COLOR_WHITE)
    title_rgb = COLOR_MAP.get(title_color, COLOR_RED)
    body_rgb = COLOR_MAP.get(body_color, COLOR_BLACK)
    sep_rgb = COLOR_MAP.get(separator_color, COLOR_YELLOW)

    img = Image.new("RGB", (width, height), bg_rgb)
    draw = ImageDraw.Draw(img)
    usable_w = width - 2 * metrics["padding_x"]
    y_cursor = metrics["padding_y"]

    if title:
        t_size = title_size or _auto_title_size(
            draw,
            title,
            usable_w,
            font_path,
            metrics["title_max"],
            metrics["title_min"],
            metrics["line_spacing"],
        )
        title_font = _find_font(t_size, bold=True, font_path=font_path)
        title_lines = _wrap_text(draw, title, title_font, usable_w)

        for line in title_lines:
            bbox = draw.textbbox((0, 0), line, font=title_font)
            line_w = bbox[2] - bbox[0]
            x = metrics["padding_x"] + (usable_w - line_w) // 2
            draw.text((x, y_cursor), line, fill=title_rgb, font=title_font)
            y_cursor += bbox[3] + metrics["line_spacing"]

        y_cursor += metrics["title_body_gap"] // 2
        sep_y = y_cursor
        draw.rectangle(
            [
                metrics["padding_x"] + metrics["separator_inset"],
                sep_y,
                width - metrics["padding_x"] - metrics["separator_inset"],
                sep_y + metrics["separator_height"],
            ],
            fill=sep_rgb,
        )
        y_cursor = sep_y + metrics["separator_height"] + metrics["title_body_gap"]

    remaining_h = height - metrics["padding_y"] - y_cursor
    b_size = body_size or _auto_body_size(
        draw,
        body,
        usable_w,
        remaining_h,
        font_path,
        metrics["body_max"],
        metrics["body_min"],
        metrics["line_spacing"],
    )
    # 电子墨水屏小字优化：字号较小时禁用抗锯齿，避免笔画发灰
    smallest_size = min((t_size if title else 999), b_size)
    if profile.name == "2.13inch" or smallest_size <= 16:
        draw.fontmode = "1"

    # 正文极小字号时自动加粗以提升可读性
    body_bold = b_size <= 12 and body_size is None
    body_font = _find_font(b_size, bold=body_bold, font_path=font_path)
    body_lines = _wrap_text(draw, body, body_font, usable_w)

    for line in body_lines:
        if not line:
            y_cursor += metrics["body_blank_gap"] // 2
            continue

        bbox = draw.textbbox((0, 0), line, font=body_font)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        if y_cursor + line_h > height - metrics["padding_y"]:
            break

        if align == "center":
            x = metrics["padding_x"] + (usable_w - line_w) // 2
        else:
            x = metrics["padding_x"]
        draw.text((x, y_cursor), line, fill=body_rgb, font=body_font)
        y_cursor += line_h + metrics["line_spacing"]

    return img


def _auto_title_size(
    draw: ImageDraw.ImageDraw,
    title: str,
    max_w: int,
    font_path: str | None,
    max_size: int,
    min_size: int,
    spacing: int,
) -> int:
    """自动选择标题字号，尽量大且不超过 2 行。"""
    for size in range(max_size, min_size - 1, -1):
        font = _find_font(size, bold=True, font_path=font_path)
        lines = _wrap_text(draw, title, font, max_w)
        if len(lines) <= 2 and _calc_text_height(lines, draw, font, spacing) > 0:
            return size
    return min_size


def _auto_body_size(
    draw: ImageDraw.ImageDraw,
    body: str,
    max_w: int,
    max_h: int,
    font_path: str | None,
    max_size: int,
    min_size: int,
    spacing: int,
) -> int:
    """自动选择正文字号，尽量大且全部内容放得下。"""
    for size in range(max_size, min_size - 1, -1):
        font = _find_font(size, bold=False, font_path=font_path)
        lines = _wrap_text(draw, body, font, max_w)
        if _calc_text_height(lines, draw, font, spacing) <= max_h:
            return size
    return min_size
