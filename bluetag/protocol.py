"""
协议编解码模块 — 帧组装、分包

无外部 BLE 依赖，可在任何平台使用。
生成的帧数据可通过任意 BLE 库发送。
"""

import struct
import lzokay

from bluetag.image import BPP2_SIZE

L1_RAW_SIZE = 20480
L2_RAW_SIZE = 4480
MAX_PAYLOAD = 160  # 每个 BLE 包最大 payload


def _lzo_compress(data: bytes) -> bytes:
    """LZO1X-1 压缩 (raw, 无 header)"""
    return lzokay.compress(data)


def build_frame(mac_suffix: bytes, image_data_2bpp: bytes) -> bytes:
    """
    组装完整的 BLE 协议帧。

    Args:
        mac_suffix: BLE MAC 地址后 4 字节 (如 b'\\xeb\\xb9\\xd7\\x6b')
        image_data_2bpp: 24960 bytes 的 2bpp 图像数据

    Returns:
        bytes: 完整帧数据，可直接传给 packetize() 分包
    """
    assert len(image_data_2bpp) == BPP2_SIZE
    assert len(mac_suffix) == 4

    # 分段压缩
    raw_l1 = image_data_2bpp[:L1_RAW_SIZE]
    raw_l2 = image_data_2bpp[L1_RAW_SIZE:]

    lzo_l1_full = _lzo_compress(raw_l1)
    lzo_l2_full = _lzo_compress(raw_l2)

    # 分离 method byte
    lzo_l1_method = lzo_l1_full[0:1]
    lzo_l1 = lzo_l1_full[1:]
    lzo_l2_method = lzo_l2_full[0:1]
    lzo_l2 = lzo_l2_full[1:]

    # Checksum (含 method byte)
    l1_checksum = sum(lzo_l1_full) & 0xFFFF
    l2_checksum = sum(lzo_l2_full) & 0xFFFF

    # L2 meta (嵌入 L1 block 末尾)
    # l2_full_size <= 255: [0x00 sep] [size 1B] [checksum 2B] [method 1B]
    # l2_full_size > 255:  [size 2B] [checksum 2B] [method 1B]
    l2_full_len = len(lzo_l2_full)
    if l2_full_len <= 255:
        l2_meta = (
            b"\x00"
            + struct.pack(">B", l2_full_len)
            + struct.pack(">H", l2_checksum)
            + lzo_l2_method
        )
    else:
        l2_meta = (
            struct.pack(">H", l2_full_len)
            + struct.pack(">H", l2_checksum)
            + lzo_l2_method
        )

    layer1_block = lzo_l1 + l2_meta
    layer2_block = lzo_l2 + b"\x00\x00\x00\x00\x00"

    l1_size = len(layer1_block)
    l2_size = len(layer2_block)

    # Image header (15 bytes)
    img_header = bytes([0x0C, 0xAA, 0x02])
    img_header += struct.pack(">HH", l1_size, l2_size)
    img_header += bytes([0x00, 0x04, 0x00, 0x04, 0x00, 0x04, 0x00, 0x04])

    # Image meta (5 bytes)
    img_meta = struct.pack(">H", len(lzo_l1_full))
    img_meta += struct.pack(">H", l1_checksum)
    img_meta += lzo_l1_method

    # 拼接 + 11 bytes padding
    image_part = img_header + img_meta + layer1_block + layer2_block + b"\x00" * 11

    # Session = checksum of image_part (含 padding)
    session_val = sum(image_part) & 0xFFFF

    # MAC header (10 bytes)
    mac_header = b"\xaa\xaa" + mac_suffix
    mac_header += struct.pack(">H", len(image_part))
    mac_header += struct.pack(">H", session_val)

    return mac_header + image_part


def packetize(frame: bytes) -> list[bytes]:
    """
    将完整帧分成 BLE 数据包。

    包格式: [idx 1B] [0x00] [payload_len 1B] [payload] [checksum 1B]
    首包: [0x00, 0x00]  末包: [0xFF, 0xFF]

    Args:
        frame: build_frame() 的输出

    Returns:
        list[bytes]: BLE 数据包列表，按顺序发送
    """
    packets = [b"\x00\x00"]

    idx = 1
    offset = 0
    while offset < len(frame):
        chunk = frame[offset : offset + MAX_PAYLOAD]
        checksum = sum(chunk) & 0xFF
        pkt = bytes([idx, 0x00, len(chunk)]) + chunk + bytes([checksum])
        packets.append(pkt)
        idx += 1
        offset += MAX_PAYLOAD

    packets.append(b"\xff\xff")
    return packets


def parse_mac_suffix(device_name: str) -> bytes:
    """
    从设备名提取 MAC 后缀。

    Args:
        device_name: BLE 设备名 (如 "EPD-EBB9D76B")

    Returns:
        bytes: 4 字节 MAC 后缀
    """
    hex_str = device_name
    for prefix in ("EPD-", "EDP-"):
        if device_name.startswith(prefix):
            hex_str = device_name.removeprefix(prefix)
            break

    if len(hex_str) == 8:
        return bytes.fromhex(hex_str)
    raise ValueError(f"无法从设备名 '{device_name}' 提取 MAC 后缀")
