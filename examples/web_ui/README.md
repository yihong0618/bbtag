# Web UI

本地 Web 界面（纯 HTML + 原生 JS，零构建），覆盖扫描设备 / 推送文字 / 上传图片 / 预览。

## 安装依赖

```bash
uv sync --extra server
```

## 配置环境变量（写入项目根目录的 `.env`）

```
BLUETAG_API_TOKEN=your-secret
```

## 启动

```bash
uv run examples/run_web_ui.py
```

浏览器打开 <http://127.0.0.1:8090/>，token 填到右上角输入框。完整 REST 文档见 <http://127.0.0.1:8090/docs>。
