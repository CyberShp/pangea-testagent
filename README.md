# Pangea Testagent

独立的本地环境配置工作台，支持 Skill、SSH、OpenAI 兼容 API 和 ACP。

## 任务执行

所有环境在设备操作前均需确认变更预览；执行器按确认的设备、命令和顺序执行。
任务页实时展示设备状态、日志与对话。Skill 可声明拓扑连线及配置前后采集方法，具体格式见
[Skill 框架约定](skills/testagent-skill-author/references/contract.md)。未声明采集方法或采集不完整时，页面会说明无法对比。

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
