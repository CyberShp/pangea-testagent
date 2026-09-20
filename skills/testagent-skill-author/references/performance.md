# 性能场景编写约定

平台负责工具部署、受控负载、并发、指标存储、图表、停止和记录。内网 Skill 负责主机/阵列配置、协议连接、目标发现、业务命令、达标标准和恢复语义。iSCSI、NVMe TCP、NAS 共用此约定；凭据仍由设备库提供。

## 任务流程

按实际业务声明检查环境、配置、连接、目标发现、负载、监控、停止、恢复和报告步骤。用 preview 分阶段确认实际命令，探测结果决定后续操作时重新预览。异步负载通过 load_start 启动，全部并发成员启动后再 load_wait；不要串行等待各成员后伪称并发。

平台内置网卡场景可直接在任务中心使用。内部业务场景生成可导入包后作为自定义任务使用。导入校验检查声明格式，命令正确性与设备业务效果必须在内网验收。

## contract.json 扩展

可选 workloads 数组，每项：id、role、driver（iperf3/vdbench/custom）、recovery。custom 还需 start、status、stop 文本，解释具体调用方法与目标范围。JSONL 指标还需 parser=jsonl、metrics。

```json
{"workloads":[{"id":"nas-io","role":"client","driver":"custom","parser":"jsonl","metrics":[{"name":"iops","unit":"iops","field":"io.iops"},{"name":"bandwidth","unit":"B/s","field":"io.mib_per_sec","scale":1048576},{"name":"latency","unit":"ms","field":"io.latency_ms"}],"start":"根据内部命令集生成限定目标的启动命令","status":"输出 running 布尔值和 io 指标的 JSON","stop":"仅停止本任务目标，允许重复调用，并核对状态","recovery":"停止后核对目标状态和原配置"}]}
```

上例的描述需要用内网真实知识补齐，不能直接用描述代替命令执行。角色必须在 roles 中声明。指标支持单位 iops、B/s、bit/s、ms、us、%、count；scale 用于单位换算，不能用来弥补缺失值。timestamp 为 Unix 秒；缺省使用平台采集时间，并标注该时间来源。

## testagent 新动作

所有远端动作仍列入 preview，携带 role。关键操作 action_id 引用 critical_actions。

- tool_list：本机只读，列出工具 id、架构、版本、驱动。
- tool_deploy：tool_id；检查 uname -m、校验传输，返回可执行 path。目标需 Python >=3.8、Linux /proc。Vdbench 部署检查 Java；本地库兼容性通过实际负载验收。
- load_start：load 对象。name 在当前任务唯一，driver 与角色需有 workloads 声明；duration 为 1–86400 秒。保存记录后启动远端监督进程，返回不等待整个负载结束。
- load_status：name；核对远端状态并采集数据。
- load_wait：name、timeout；默认等到确认退出，可用 expect 等待真实输出中的就绪标记。轮询范围已包含在这次操作中。
- load_stop：name；停止本任务负载并核实退出。失败或不明状态保留设备占用。
- tune_apply：tuning={name,interface,irqs,pause_irqbalance}；irqs 为 IRQ 编号到 CPU 列表映射。先保存原值，再执行与回读。
- tune_restore：name；恢复本任务或授权恢复任务的原始 IRQ 值及 irqbalance 状态，设备重启后不套用旧编号。

load.cpus 为 Linux CPU 列表，例如 4-7，用 taskset 约束此负载。根据探测的在线 CPU、NUMA 和网卡对应关系生成，不硬编码成通用值。调整中断与暂停 irqbalance 要明确影响范围。

## iperf3

load.driver=iperf3，tool_path 使用部署返回的路径，iperf 包含 bind、port、server（服务端 true）、one_off（默认 true，分段多连接时明确设为 false 并安排 load_stop），客户端还包括 peer、parallel、warmup、reverse、bitrate（整对 bit/s）。平台将整对目标换算成 iperf3 的每流限速。自带 3.21 使用实时 JSON，其他版本先核对能力。

服务端使用单次连接并由监督进程限定最长存活时间。先等待全部服务端就绪，再启动全部客户端，最后等待完成。统计时区分发送/接收、瞬时/整轮均值和正反向。多路合计必须说明时间对齐、共同窗口和时钟精度，不能把采集时刻当设备精确同步时刻。

## Vdbench

load.driver=vdbench，tool_path 使用内部提供的可执行路径；vdbench 参数：targets（/dev 下设备路径数组）、read_pct、seek_pct、block_kib、threads、rate（IOPS）。read_pct<100 还必须 write_confirmed=true，并在预览清晰说明写盘及目标数据影响。

先通过内网流程发现映射，核对唯一标识、容量、协议、路径、挂载和多路径关系，再选择设备。HUAWEI 名称与临时 /dev/sdX、/dev/nvmeXnY 名字只作为线索。系统盘、已挂载盘、业务数据盘不能作为未经确认的写入目标。

平台生成 sd/wd/rd 配置，保存实际参数与阶段记录。摘要输出按标准 interval/iops/MB/sec/response time 解析，带宽换算为 B/s，时延 ms。版本输出不同需提供样例更新解析或通过内部 JSONL 转换器接入；不得编造数据。原始日志始终保留。

## 自研工具

前台模式：load={name,driver:'custom',duration,command,parser:'jsonl',metrics:[...]}; command 为具体有界命令。进程组由平台跟踪。stdout 每行一个 JSON 时可直接采样；sample_file 可指定远端追加式 JSONL 文件，必须使用本轮独有文件，不能混入旧轮数据。

外部模式：增加 external=true、status_command、stop_command。command 启动后退出；status_command 返回单个 JSON 对象，必须含 running 布尔值，也可含指标。平台每秒执行状态命令，结束时执行 stop_command，再读取 running=false 验证停止。控制命令限时 15 秒。停止命令必须只针对本任务，并可安全重复执行。命令和目标在预览中完整展示。若设备断连，显示未知，恢复连接后继续核对；不会自动重启旧负载。

外部工具的波形由内部启动命令或脚本实现，平台记录与监控；前台工具可使用以下分段计划。

## 负载计划

load.plan 与 load.duration 一致：shape、duration、interval。constant 需 rate；square 需 low/high；surge 需 low/high/surge_start/surge_duration；random 需 low/high/seed。每计划最多 1000 段。iperf3 rate 单位 bit/s，Vdbench 为 IOPS，自研 command 用 {rate}、{duration} 代入已校验数值。

rate=0 对应空闲阶段。分段通过停止/退出本段、重启下一段实现，存在切换间隔；不能承诺无缝硬实时波形。每段真实启动、结束、退出码均保存。服务端配套流程需适配分段连接数，不能复用只接一次就退出的 iperf 服务端覆盖整条分段曲线。

## 离线工具包

ZIP 根目录 tool.json，含 name、version、os=linux、architecture=x86_64/aarch64、driver、entrypoint、executables、files。files 为每个载荷文件的相对路径到 SHA-256 映射；tool.json 本身不在映射内。禁止链接和越界路径，展开与压缩均限 64 MiB。保留第三方许可证和依赖说明。Vdbench 工具包由内部获得并导入；Java 可作为已安装依赖由场景检查。

## 内网验收

验证真实目标发现及配置、工具启动/停止、断连/重启后的核实、多负载并行、指标与工具原始输出一致、调优恢复、报表导出。跨发行版及架构逐类记录。没有实际设备证据时，只报告平台检查结果。
