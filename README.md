# Pangea Testagent

独立的本地环境配置工作台，支持 Skill、SSH、OpenAI 兼容 API 和 ACP。

## Windows 运行

从 Actions 构建产物下载便携包，解压后双击 `Start-Testagent.cmd`。无需安装 Python、Node.js、WSL 或 Docker。

## 开发与测试

```bash
python -m pip install -r requirements.txt
PYTHONPATH=src python -m testagent.launcher
PYTHONPATH=src python -m unittest discover -s tests -v
npm ci
node scripts/test-ui.cjs
```

Windows 构建：`scripts/build-windows.ps1`。真实设备及 ACP 产品兼容性需要在目标环境验收。
