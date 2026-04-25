# BluETag

BT370R 蓝签电子墨水标签 BLE 图像推送库。

支持两类屏幕:

- `3.7inch` (`240×416`, 4 色, 设备名前缀 `EPD-`)
- `2.13inch` (`250×122`, 黑/白/红, 设备名前缀 `EDP-`)

CLI 会按 `--screen` 自动切换发送协议，并分别缓存到 `.device.3.7inch` / `.device.2.13inch`。默认屏幕是 `3.7inch`。

## 快速开始

```bash
# 安装
git clone <your-repo-url> && cd bbtag
uv sync
```

需要蓝牙适配器 (USB dongle 或内置蓝牙)。

## CLI

```bash
# 扫描设备
uv run bluetag scan

# 扫描 2.13 寸设备
uv run bluetag scan --screen 2.13inch

# 推送图片
uv run bluetag push photo.png

# 推送到 2.13 寸
uv run bluetag push photo.png --screen 2.13inch

# 推送文字 (自动排版)
uv run bluetag text "14:00 项目评审\n16:00 周会"

# 给 2.13 寸推送文字
uv run bluetag text "会议室A\n14:00-15:30" --screen 2.13inch

# 把 Codex usage 画成 /stats 风格并推到 2.13 寸
uv run examples/push_codex_usage.py

# 仅生成 Codex usage 预览图
uv run examples/push_codex_usage.py --preview-only

# 把 Kimi Code usage 画成 /stats 风格并推到 2.13 寸
uv run examples/push_kimi_usage.py

# 把 Kimi Code usage 画成 /stats 风格并推到 3.7 寸
uv run examples/push_kimi_usage_3.7.py

# 把 macOS 当天 App 使用时长榜单推到 3.7 寸
# 需要给终端 Full Disk Access 才能读取 Knowledge 数据库
uv run examples/push_macos_app_usage_3.7.py --preview-only

# 仅生成 Kimi usage 预览图
uv run examples/push_kimi_usage.py --preview-only

# 自定义标题和颜色
uv run bluetag text "会议室A 三楼" --title "指引" --title-color red

# 仅生成预览图，不推送
uv run bluetag text "测试内容" --preview-only

# 指定 3.7 寸设备
uv run bluetag push photo.png -d EPD-EBB9D76B

# 指定 2.13 寸设备
uv run bluetag push photo.png --screen 2.13inch -d EDP-F3F4F5F6

# 调整发送速度 (ms/包, 默认按屏幕选择)
uv run bluetag push photo.png -i 80
```

### text 子命令参数

| 参数 | 说明 |
|------|------|
| `body` (位置参数) | 正文内容，`\n` 换行 |
| `--title, -T` | 标题，默认当天日期，格式 `YYYY-MM-DD` |
| `--title-color` | 标题颜色: black / red / yellow |
| `--body-color` | 正文颜色: black / red / yellow |
| `--separator-color` | 分隔线颜色: black / red / yellow |
| `--align` | 正文对齐: left / center |
| `--font` | 自定义字体路径 |
| `--preview-only` | 仅生成预览图，不推送 |
| `--screen` | 屏幕尺寸: `3.7inch` / `2.13inch` |

文字排版会根据 `--screen` 自动切换画布尺寸和字号策略。标题尽量大 (最多 2 行)，正文自动缩小直到全部放得下。

## Python API

```python
import asyncio
from PIL import Image
from bluetag import quantize, pack_2bpp, build_frame, packetize, render_text
from bluetag.protocol import parse_mac_suffix
from bluetag.ble import scan, push

async def push_image():
    img = Image.open("photo.png")
    indices = quantize(img)
    data_2bpp = pack_2bpp(indices)

    devices = await scan()
    target = devices[0]
    mac = parse_mac_suffix(target["name"])
    frame = build_frame(mac, data_2bpp)
    packets = packetize(frame)
    await push(packets, device_address=target["address"])

async def push_text():
    img = render_text(body="Hello World", title="2026-03-30")
    indices = quantize(img)
    data_2bpp = pack_2bpp(indices)

    devices = await scan()
    target = devices[0]
    mac = parse_mac_suffix(target["name"])
    frame = build_frame(mac, data_2bpp)
    packets = packetize(frame)
    await push(packets, device_address=target["address"])

asyncio.run(push_image())
```

## Web UI

`examples/web_ui/` 提供一个本地 Web 界面（纯 HTML + 原生 JS），覆盖扫描设备 / 推送文字 / 上传图片 / 预览。

安装依赖：

```bash
uv sync --extra server
```

配置环境变量（写入项目根目录的 `.env`）：

```
BLUETAG_API_TOKEN=your-secret
```

启动：

```bash
uv run examples/run_web_ui.py
```

浏览器打开 <http://127.0.0.1:8090/>，token 填到右上角输入框。完整 REST 文档见 <http://127.0.0.1:8090/docs>。

## 项目结构

```
bbtag/
├── bluetag/              # Python 核心库
│   ├── image.py          #   图像量化、2bpp 编解码
│   ├── text.py           #   文字渲染、自动排版
│   ├── protocol.py       #   协议帧组装、LZO 压缩、分包
│   ├── ble.py            #   BLE 扫描/连接/发送 (bleak)
│   ├── screens.py        #   屏幕配置、设备名前缀、缓存文件规则
│   ├── transfer.py       #   2.13 寸图层发送协议
│   ├── server.py         #   REST API 服务 (FastAPI)
│   └── cli.py            #   命令行工具
├── examples/                     # 示例脚本
│   ├── web_ui/                   #   Web UI 静态资源 (index.html / app.js / styles.css)
│   ├── run_web_ui.py             #   启动 server + 挂载 Web UI
│   ├── push_image.py             #   推送图片示例
│   ├── push_text.py              #   推送文字示例
│   ├── push_codex_usage.py       #   Codex usage -> 2.13 寸
│   ├── push_codex_usage_3.7.py   #   Codex usage -> 3.7 寸
│   ├── push_kimi_usage.py        #   Kimi usage -> 2.13 寸
│   ├── push_kimi_usage_3.7.py    #   Kimi usage -> 3.7 寸
│   ├── push_macos_app_usage_3.7.py # macOS app usage -> 3.7 寸
│   └── push_crypto_binance_price.py # 币价 -> 2.13 寸
└── pyproject.toml
```
