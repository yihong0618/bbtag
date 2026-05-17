"""
图像处理模块 — 量化、2bpp 编解码、双色屏图层处理

无外部 BLE 依赖，可在任何平台使用。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from bluetag.screens import get_screen_profile

WIDTH, HEIGHT = 240, 416
PIXELS = WIDTH * HEIGHT
BPP2_SIZE = PIXELS // 4  # 24960 bytes

# 4色调色板 (RGB) — 按 2bpp 值索引
# 00=黑 01=白 10=黄 11=红
PALETTE = np.array(
    [
        [0, 0, 0],
        [255, 255, 255],
        [255, 255, 0],
        [255, 0, 0],
    ],
    dtype=np.float32,
)


def _ensure_image(source: Image.Image | str | Path) -> Image.Image:
    if isinstance(source, Image.Image):
        return source.copy()
    return Image.open(source)


def quantize(
    img: Image.Image,
    flip: bool = True,
    size: tuple[int, int] = (WIDTH, HEIGHT),
) -> np.ndarray:
    """
    将图像量化为 4 色索引数组。

    Args:
        img: PIL Image (任意尺寸/模式)
        flip: 水平翻转
        size: 目标尺寸

    Returns:
        np.ndarray shape=(pixels,) dtype=uint8, 值 0-3
    """
    width, height = size
    img = img.convert("RGB").resize((width, height), Image.LANCZOS)
    if flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    pixels = np.array(img, dtype=np.float32).reshape(-1, 3)
    dists = np.linalg.norm(pixels[:, None, :] - PALETTE[None, :, :], axis=2)
    return dists.argmin(axis=1).astype(np.uint8)


def quantize_for_screen(
    img: Image.Image,
    screen: str = "3.7inch",
    flip: bool | None = None,
) -> np.ndarray:
    """按屏幕尺寸量化图像。"""
    profile = get_screen_profile(screen)
    effective_flip = profile.mirror if flip is None else flip
    return quantize(img, flip=effective_flip, size=profile.size)


def pack_2bpp(indices: np.ndarray) -> bytes:
    """
    将 4 色索引数组打包为 2bpp 字节流 (MSB first, 每字节 4 像素)。

    Args:
        indices: shape=(PIXELS,) dtype=uint8, 值 0-3

    Returns:
        bytes, 长度 24960
    """
    assert len(indices) == PIXELS
    groups = indices.reshape(-1, 4)
    packed = (
        (groups[:, 0] << 6) | (groups[:, 1] << 4) | (groups[:, 2] << 2) | groups[:, 3]
    )
    return bytes(packed.astype(np.uint8))


def unpack_2bpp(data: bytes) -> np.ndarray:
    """
    将 2bpp 字节流解包为 4 色索引数组。

    Args:
        data: 24960 bytes

    Returns:
        np.ndarray shape=(PIXELS,) dtype=uint8, 值 0-3
    """
    arr = np.frombuffer(data, dtype=np.uint8)
    p0 = (arr >> 6) & 3
    p1 = (arr >> 4) & 3
    p2 = (arr >> 2) & 3
    p3 = arr & 3
    return np.stack([p0, p1, p2, p3], axis=1).flatten()


def indices_to_image(
    indices: np.ndarray,
    size: tuple[int, int] = (WIDTH, HEIGHT),
) -> Image.Image:
    """
    将 4 色索引数组转为 RGB PIL Image。

    Args:
        indices: shape=(pixels,) dtype=uint8, 值 0-3
        size: 输出尺寸

    Returns:
        PIL Image
    """
    width, height = size
    rgb = PALETTE[indices].astype(np.uint8).reshape(height, width, 3)
    return Image.fromarray(rgb)


def process_bicolor_image(
    source: Image.Image | str | Path,
    screen: str,
    *,
    threshold: int = 128,
    dither: bool = False,
    rotate: int = 0,
    mirror: bool = True,
    swap_wh: bool = False,
    detect_red: bool = True,
    fit: str = "contain",
) -> tuple[np.ndarray, np.ndarray, Image.Image]:
    """
    将图像处理为双色电子墨水屏的黑层/红层。

    Returns:
        (black_layer, red_layer, preview_image)
    """
    profile = get_screen_profile(screen)
    img = _ensure_image(source).convert("RGB")

    width, height = profile.size
    if swap_wh:
        width, height = height, width

    if rotate:
        img = img.rotate(rotate, expand=True)

    if fit == "cover":
        scale = max(width / img.width, height / img.height)
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    else:
        img.thumbnail((width, height), Image.Resampling.LANCZOS)
    if mirror:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    canvas = Image.new("RGB", (width, height), "white")
    x_offset = (width - img.width) // 2
    y_offset = (height - img.height) // 2
    canvas.paste(img, (x_offset, y_offset))

    gray = canvas.convert("L")
    if dither:
        gray = gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")

    img_array = np.array(gray)
    black_layer = (img_array >= threshold).astype(np.uint8)
    red_layer = np.zeros_like(black_layer)

    if detect_red:
        rgb_array = np.array(canvas)
        is_red = (
            (rgb_array[:, :, 0] > 150)
            & (rgb_array[:, :, 1] < 100)
            & (rgb_array[:, :, 2] < 100)
        )
        red_layer = is_red.astype(np.uint8)
        black_layer = black_layer & (~red_layer)

    return black_layer, red_layer, bicolor_layers_to_image(black_layer, red_layer)


def layer_to_bytes_rowwise(layer: np.ndarray) -> bytes:
    """Pack a layer row by row, 8 horizontal pixels per byte."""
    height, width = layer.shape
    bytes_per_row = (width + 7) // 8
    data = []

    for row in range(height):
        for byte_idx in range(bytes_per_row):
            start_col = byte_idx * 8
            byte_val = 0
            for bit_idx in range(8):
                col = start_col + (7 - bit_idx)
                if col < width and layer[row, col]:
                    byte_val |= 1 << bit_idx
            data.append(byte_val)

    return bytes(data)


def layer_to_bytes_columnwise(layer: np.ndarray) -> bytes:
    """Pack a layer column by column, 8 vertical pixels per byte."""
    height, width = layer.shape
    bytes_per_column = (height + 7) // 8
    data = []

    for col in range(width):
        for byte_idx in range(bytes_per_column):
            start_row = byte_idx * 8
            byte_val = 0
            for bit_idx in range(8):
                row = start_row + bit_idx
                if row < height and layer[row, col]:
                    byte_val |= 1 << bit_idx
            data.append(byte_val)

    return bytes(data)


def layer_to_bytes(layer: np.ndarray, encoding: str = "row") -> bytes:
    """Convert image layer to transmission bytes."""
    if encoding == "row":
        return layer_to_bytes_rowwise(layer)
    if encoding == "column":
        return layer_to_bytes_columnwise(layer)
    raise ValueError(f"Unsupported encoding: {encoding}")


def bicolor_layers_to_image(
    black_layer: np.ndarray,
    red_layer: np.ndarray,
) -> Image.Image:
    """Convert black/red binary layers into an RGB preview image."""
    height, width = black_layer.shape
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)
    canvas[black_layer == 0] = (0, 0, 0)
    canvas[red_layer == 1] = (255, 0, 0)
    return Image.fromarray(canvas)
