# Pangea Testagent

独立的本地环境配置工作台，支持 Skill、SSH、OpenAI 兼容 API 和 ACP。

## 任务执行

所有环境在设备操作前均需确认变更预览；执行器按确认的设备、命令和顺序执行。
任务页实时展示设备状态、日志与对话。Skill 可声明拓扑连线及配置前后采集方法，具体格式见
[Skill 框架约定](skills/testagent-skill-author/references/contract.md)。未声明采集方法或采集不完整时，页面会说明无法对比。

## Windows 运行

从 Actions 构建产物下载便携包，解压后双击 `Start-Testagent.cmd`。无需安装 Python、Node.js、WSL 或 Docker。

## 离线升级到 1.1.2

在旧版“设置与后端 → 离线升级”中导入 `pangea-testagent-1.1.2-windows-x64-patch.zip`，确认后重启。补丁复用现有运行时；若提示运行时不兼容，导入同版本的 `windows-x64-update.zip` 完整升级包。升级前完成活动任务并处理待恢复设备。

升级保留用户数据目录中的设备、凭据、环境、已导入 Skill 和历史任务。Skill 管理中预置“网卡极限带宽测试”；在“新建任务”的任务 Skill 列表选择该场景，绑定设备并填写测试参数即可使用。场景随平台升级提供，每次任务保存所用场景版本；已有 Skill 版本和历史任务快照保持原样。该预设支持单端口和整卡双端口并发，执行端需准备 iperf3、Python 3 等已声明依赖。

## 开发与测试

```bash
python -m pip install -r requirements.txt
PYTHONPATH=src python -m testagent.launcher
PYTHONPATH=src python -m unittest discover -s tests -v
npm ci
node scripts/test-ui.cjs
```

Windows 构建：`scripts/build-windows.ps1`。真实设备及 ACP 产品兼容性需要在目标环境验收。

## 持续负载与性能（1.1）

平台通过 `load_start / load_wait / load_status / load_stop` 管理 Linux 远端负载，保存运行状态、采样与原始日志。关闭网页不停止任务；平台重启后保留任务和负载记录，核实远端状态。未知状态保持设备占用，可在任务页查询或停止。运行结束后导出报告包含负载参数、阶段、性能样本和原始日志。

设置页的离线工具库支持校验并导入 Linux x86_64 / ARM64 工具包。发布包内含两种架构的 iperf3，原生 Linux 构建时校验官方源码 SHA-256，使用静态链接并保留许可证。Vdbench 支持直接导入内网提供的原始 ZIP（填写实际版本，架构自动识别或手动选择），自动生成工具清单，兼容单一外层目录；目标设备需准备 Java；自研工具可声明前台进程或外部 start/status/stop 生命周期。

任务页展示分负载性能曲线、阶段记录和退出状态。负载计划支持固定、方波、浪涌、随机变化；分段重启有切换间隔，记录实际发生时间。IRQ 调优保存原值并回读，恢复验证后任务才能正常完成。自研场景的配置、连接、设备发现与业务判断由内网 Skill 定义，编写方法见 [性能场景约定](skills/testagent-skill-author/references/performance.md)。

## 内网验收清单

1. 在备份后的旧版安装上导入补丁，检查设备凭据、环境、自定义 Skill、历史任务与附件；运行时不匹配时使用完整升级包。
2. 分别选 x86_64、ARM64 主机或阵列，验证 Python 3.8+、/proc、SSH 权限和工具兼容性。CentOS、RHEL、Ubuntu、Euler 的目标版本逐项记录。
3. 网卡预设分别运行单端口、整卡双端口，核对测试 IP/路由、两对实际重叠、正反向数据及目标判定。与 iperf3 原始输出对照曲线。
4. 在隔离的可写测试盘上验证 Vdbench 参数、IOPS/带宽/时延；检查方波、浪涌、随机计划的真实阶段及切换间隔。
5. 用内部命令生成 iSCSI、NVMe TCP、NAS 场景，验证配置、连接、唯一目标发现、启动/状态/停止、单位换算和报告。NAS 外部模式要确认 stop 命令只影响本任务。
6. 验证停止、SSH 断连、平台重启后的负载核实；未确认退出时保持未知和占用。核对无误后再执行恢复或人工释放。
7. 在允许调整的测试环境确认绑核与 IRQ 方案，比较各轮结果，核对原值恢复与 irqbalance 状态。设备重启后重新核对 IRQ。

真实设备、内部命令和性能结果由内网验收。本机测试、构建和包校验不代表这些场景已通过。
