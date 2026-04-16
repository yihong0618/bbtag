"""
CLI 入口 — bluetag 命令行工具
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from PIL import Image

from bluetag.ble import BleDependencyError
from bluetag.image import (
    indices_to_image,
    layer_to_bytes,
    pack_2bpp,
    process_bicolor_image,
    quantize,
    unpack_2bpp,
)
from bluetag.protocol import build_frame, packetize, parse_mac_suffix
from bluetag.screens import ScreenProfile, get_screen_profile
from bluetag.text import render_text
from bluetag.transfer import send_bicolor_image

DEFAULT_SCAN_TIMEOUT = 5.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3
DEFAULT_SCREEN = "3.7inch"


def _default_text_title() -> str:
    return f"{date.today():%Y-%m-%d}"


def _resolve_profile(screen: str) -> ScreenProfile:
    try:
        return get_screen_profile(screen)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _save_device(device: dict, profile: ScreenProfile):
    """将设备信息写入对应屏幕的缓存文件。"""
    profile.cache_path.write_text(f"{device['name']}\n{device['address']}\n")
    print(f"  💾 已保存到 {profile.cache_path}")


def _load_device(profile: ScreenProfile) -> dict | None:
    """从对应屏幕的缓存文件读取设备信息。"""
    if not profile.cache_path.exists():
        return None
    lines = profile.cache_path.read_text().strip().splitlines()
    if len(lines) >= 2:
        return {"name": lines[0], "address": lines[1]}
    return None


async def _find_target(args, profile: ScreenProfile) -> dict | None:
    from bluetag.ble import find_device

    if not args.device and not args.address:
        cached = _load_device(profile)
        if cached:
            print(
                f"使用 {profile.name} 缓存设备: {cached['name']} ({cached['address']})"
            )
            return cached

    print(f"扫描 {profile.name} 设备 ({profile.device_prefix}*)...")
    target = await find_device(
        device_name=args.device,
        device_address=args.address,
        timeout=DEFAULT_SCAN_TIMEOUT,
        scan_retries=DEFAULT_SCAN_RETRIES,
        prefixes=(profile.device_prefix,),
    )
    if target:
        _save_device(target, profile)
    return target


def _frame_progress(sent: int, total: int):
    if sent == total:
        print(f"\r✅ 发送完成! ({total} 包)")
    elif sent == 1 or sent % 10 == 0:
        print(f"\r  发送中 {sent}/{total}...", end="", flush=True)


def _layer_progress(layer_name: str, sent: int, total: int):
    if sent == total:
        print(f"\r✅ {layer_name}发送完成! ({total} 包)")
    elif sent == 1 or sent % 10 == 0:
        print(f"\r  {layer_name}发送中 {sent}/{total}...", end="", flush=True)


def _build_frame_preview_and_payload(
    img: Image.Image,
    profile: ScreenProfile,
) -> tuple[Image.Image, bytes]:
    indices = quantize(img, flip=profile.mirror, size=profile.size)
    preview = indices_to_image(indices, size=profile.size)
    data_2bpp = pack_2bpp(indices)
    return preview, data_2bpp


def _build_layer_preview_and_payload(
    img: Image.Image,
    profile: ScreenProfile,
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
    black_data = layer_to_bytes(black_layer, profile.encoding)
    red_data = layer_to_bytes(red_layer, profile.encoding)
    return preview, black_data, red_data


async def _push_frame_image(
    profile: ScreenProfile,
    target: dict,
    data_2bpp: bytes,
    interval_ms: int,
) -> bool:
    from bluetag.ble import push

    mac_suffix = parse_mac_suffix(target["name"])
    frame = build_frame(mac_suffix, data_2bpp)
    packets = packetize(frame)
    print(
        f"连接 {target['name']} [{profile.name}], {len(frame)} bytes, {len(packets)} 包"
    )

    return await push(
        packets,
        device_address=target["address"],
        packet_interval=interval_ms / 1000,
        on_progress=_frame_progress,
        prefixes=(profile.device_prefix,),
        scan_timeout=DEFAULT_SCAN_TIMEOUT,
    )


async def _push_layer_image(
    profile: ScreenProfile,
    target: dict,
    black_data: bytes,
    red_data: bytes,
    interval_ms: int,
) -> bool:
    from bluetag.ble import connect_session

    session = await connect_session(
        target.get("_ble_device") or target["address"],
        timeout=20.0,
        connect_retries=DEFAULT_CONNECT_RETRIES,
    )
    if not session:
        return False

    try:
        print(
            f"连接 {target['name']} [{profile.name}], "
            f"黑层 {len(black_data)} bytes, 红层 {len(red_data)} bytes"
        )
        return await send_bicolor_image(
            session,
            black_data,
            red_data,
            delay_ms=interval_ms,
            settle_ms=profile.settle_ms,
            flush_every=profile.flush_every,
            on_progress=_layer_progress,
        )
    finally:
        await session.close()


def cmd_scan(args):
    from bluetag.ble import scan

    profile = _resolve_profile(args.screen)

    async def _scan():
        print(f"扫描蓝签设备 ({profile.name}, {args.timeout}s)...")
        devices = await scan(timeout=args.timeout, prefixes=(profile.device_prefix,))
        if not devices:
            print("  未发现蓝签设备")
            return
        for device in devices:
            print(
                f"  📺 {device['name']}  ({device['address']})  RSSI: {device['rssi']}"
            )
        _save_device(devices[0], profile)

    asyncio.run(_scan())


def cmd_push(args):
    profile = _resolve_profile(args.screen)
    interval_ms = args.interval or profile.default_interval_ms
    img = Image.open(args.image)

    if profile.transport == "frame":
        preview, data_2bpp = _build_frame_preview_and_payload(img, profile)
        preview.show()

        async def _push():
            target = await _find_target(args, profile)
            if not target:
                print("❌ 未找到设备")
                return False

            ok = await _push_frame_image(profile, target, data_2bpp, interval_ms)
            if not ok:
                print("❌ 发送失败")
            return ok

    else:
        preview, black_data, red_data = _build_layer_preview_and_payload(img, profile)
        preview.show()

        async def _push():
            target = await _find_target(args, profile)
            if not target:
                print("❌ 未找到设备")
                return False

            ok = await _push_layer_image(
                profile,
                target,
                black_data,
                red_data,
                interval_ms,
            )
            if not ok:
                print("❌ 发送失败")
            return ok

    asyncio.run(_push())


def cmd_text(args):
    """渲染文字并推送到设备"""
    profile = _resolve_profile(args.screen)
    interval_ms = args.interval or profile.default_interval_ms

    body = args.body.replace("\\n", "\n")
    title = args.title

    rendered = render_text(
        body=body,
        title=title,
        title_color=args.title_color,
        body_color=args.body_color,
        separator_color=args.separator_color,
        font_path=args.font,
        align=args.align,
        screen=profile.name,
    )

    if profile.transport == "frame":
        preview, data_2bpp = _build_frame_preview_and_payload(rendered, profile)
    else:
        preview, black_data, red_data = _build_layer_preview_and_payload(
            rendered, profile
        )

    if args.preview_only:
        if profile.mirror:
            preview = preview.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        preview.show()
        print("仅预览模式，不推送")
        return

    async def _push():
        target = await _find_target(args, profile)
        if not target:
            print("❌ 未找到设备")
            return False

        if profile.transport == "frame":
            ok = await _push_frame_image(profile, target, data_2bpp, interval_ms)
        else:
            ok = await _push_layer_image(
                profile,
                target,
                black_data,
                red_data,
                interval_ms,
            )

        if not ok:
            print("❌ 发送失败")
        return ok

    asyncio.run(_push())


def cmd_decode(args):
    """从 btsnoop HCI 日志解码图像 (调试用)"""
    import subprocess

    import lzokay

    result = subprocess.run(
        [
            "tshark",
            "-r",
            args.log,
            "-Y",
            "btatt.opcode == 0x52 && btatt.handle == 0x0015",
            "-T",
            "fields",
            "-e",
            "btatt.value",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"❌ tshark 失败: {result.stderr}")
        return

    seen = set()
    payload = bytearray()
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            pkt = bytes.fromhex(line)
            if pkt not in (b"\x00\x00", b"\xff\xff"):
                payload.extend(pkt[3:-1])

    if len(payload) < 30:
        print("❌ 数据不足")
        return

    s1 = (payload[13] << 8) | payload[14]
    s2 = (payload[15] << 8) | payload[16]
    l1_lzo_size = (payload[25] << 8) | payload[26]
    l1_method = payload[29]
    l1_block = payload[30 : 30 + s1]

    l1_full = bytes([l1_method]) + bytes(l1_block[: l1_lzo_size - 1])
    l1_dec = lzokay.decompress(l1_full, 20480)

    l1_remaining = l1_block[l1_lzo_size - 1 :]
    if l1_remaining[0] == 0x00:
        l2_meta = l1_remaining[1:]
    else:
        l2_meta = l1_remaining
    l2_method = l2_meta[-1]
    if len(l2_meta) == 5:
        l2_full_size = (l2_meta[0] << 8) | l2_meta[1]
    else:
        l2_full_size = l2_meta[0]

    l2_block = payload[30 + s1 : 30 + s1 + s2]
    l2_lzo_data = l2_block[: l2_full_size - 1]
    l2_full = bytes([l2_method]) + bytes(l2_lzo_data)
    l2_dec = lzokay.decompress(l2_full, 4480)

    full_2bpp = l1_dec + l2_dec
    indices = unpack_2bpp(full_2bpp)
    img = indices_to_image(indices)

    output = args.output or (Path(args.log).stem + "_decoded.png")
    img.save(output)
    print(f"✅ 图像已保存: {output}")


def main():
    parser = argparse.ArgumentParser(
        prog="bluetag",
        description="BluETag — 蓝签电子墨水标签 BLE 图像推送工具",
    )
    sub = parser.add_subparsers(dest="command")

    screen_help = "屏幕尺寸: 3.7inch/3.7 或 2.13inch/2.13 (默认 3.7inch)"

    scan_p = sub.add_parser("scan", help="扫描附近的蓝签设备")
    scan_p.add_argument(
        "--timeout", "-t", type=float, default=5.0, help="扫描超时 (秒)"
    )
    scan_p.add_argument("--screen", default=DEFAULT_SCREEN, help=screen_help)

    push_p = sub.add_parser("push", help="推送图片到设备")
    push_p.add_argument("image", help="图片文件路径")
    push_p.add_argument("--device", "-d", help="设备名")
    push_p.add_argument("--address", "-a", help="设备 BLE 地址")
    push_p.add_argument("--screen", default=DEFAULT_SCREEN, help=screen_help)
    push_p.add_argument(
        "--interval", "-i", type=int, help="包间隔 (ms，默认按屏幕选择)"
    )

    text_p = sub.add_parser("text", help="推送文字到设备 (自动排版)")
    text_p.add_argument("body", help="正文内容 (支持 \\n 换行)")
    default_title = _default_text_title()
    text_p.add_argument(
        "--title",
        "-T",
        default=default_title,
        help=f"标题 (默认 {default_title})",
    )
    text_p.add_argument(
        "--title-color",
        default="red",
        choices=["black", "red", "yellow"],
        help="标题颜色",
    )
    text_p.add_argument(
        "--body-color",
        default="black",
        choices=["black", "red", "yellow"],
        help="正文颜色",
    )
    text_p.add_argument(
        "--separator-color",
        default="yellow",
        choices=["black", "red", "yellow"],
        help="分隔线颜色",
    )
    text_p.add_argument(
        "--align", default="left", choices=["left", "center"], help="正文对齐"
    )
    text_p.add_argument("--font", help="自定义字体路径")
    text_p.add_argument(
        "--preview-only", action="store_true", help="仅生成预览图，不推送"
    )
    text_p.add_argument("--device", "-d", help="设备名")
    text_p.add_argument("--address", "-a", help="设备 BLE 地址")
    text_p.add_argument("--screen", default=DEFAULT_SCREEN, help=screen_help)
    text_p.add_argument(
        "--interval", "-i", type=int, help="包间隔 (ms，默认按屏幕选择)"
    )

    decode_p = sub.add_parser("decode", help="从抓包文件解码图片 (调试)")
    decode_p.add_argument("log", help="btsnoop HCI 日志文件")
    decode_p.add_argument("--output", "-o", help="输出图片路径")

    args = parser.parse_args()

    if args.command == "scan":
        try:
            cmd_scan(args)
        except BleDependencyError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    elif args.command == "push":
        try:
            cmd_push(args)
        except BleDependencyError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    elif args.command == "text":
        try:
            cmd_text(args)
        except BleDependencyError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    elif args.command == "decode":
        cmd_decode(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
