# 网卡场景执行约定

## 环境与工具

1. 先预览 exec 探测：`uname -m`、`command -v python3`、`command -v setsid`。Linux 需要 Python >=3.8 和 /proc。对各接口读取 ip 地址/路由、ethtool 速率与驱动、PCI 设备路径、NUMA/local_cpulist、IRQ affinity 与 irqbalance 状态。可使用 scripts/inspect.sh，但首次探测工具缺失时应先部署，不能把缺失误当性能不达标。
2. 用 tool_list 选择架构匹配的内置 iperf3；preview 中列入 tool_deploy（role、tool_id）。使用返回 path 作为所有负载的 tool_path。部署会运行 --version；不同版本不得默认为参数兼容。自带 3.21 支持实时 --json-stream。
3. 对两个测试 IP 使用 `ip route get <peer> from <source>` 核对 dev（IPv6 加 -6）。地址需属于指定接口。两角色绑定不同设备，禁止同机回环代替硬件测试。所有 shell 参数必须使用 POSIX 引号转义。
4. 整卡模式探测两个端口，依据 PCI 和卡件信息确认被测卡归属；分别核对两对路由，使用不同监听端口。当前预设为同一被测主机与同一对端设备；不同对端拓扑由内网场景扩展角色声明。

## 一轮测试

为每方向准备一次具体预览，按以下顺序列出全部操作。name 每轮不同，如 f1-s1/f1-s2/f1-c1/f1-c2；role 为 server/client。启动操作带 action_id=load。

1. 服务端 load_start，每对一个：
```json
{"action":"load_start","role":"server","action_id":"load","load":{"name":"f1-s1","driver":"iperf3","duration":180,"tool_path":"/tmp/实际部署目录/iperf3","iperf":{"server":true,"bind":"192.0.2.2","port":5201}}}
```
服务端 duration 至少为测量秒数+预热+90；其 timeout 由平台远端监督进程负责。示例地址和路径必须替换为真实值。整卡第二套服务监听第二个 IP、port2。
2. 每个服务调用 load_wait，expect 为 `Server listening on`，timeout 15；真实输出确认监听后才能启动客户端。
3. 客户端 load_start，每对一个：
```json
{"action":"load_start","role":"client","action_id":"load","load":{"name":"f1-c1","driver":"iperf3","duration":60,"tool_path":"/tmp/实际部署目录/iperf3","iperf":{"bind":"192.0.2.1","peer":"192.0.2.2","port":5201,"parallel":4,"warmup":3,"reverse":false}}}
```
整卡连续启动两个客户端，然后才 wait，平台让两者并行运行。禁止先等待第一对完成再启动第二对。
4. 分别 load_wait 客户端，timeout 至少测量秒数+预热+45；再 load_wait 服务端确认单次服务退出。客户端或服务端失败均记录失败，核对停止残留负载。
5. 反向使用新名称和 reverse=true 重复完整一轮。正反向按时间分别测，不把两方向速率相加。短时启动差异需在报告中披露。

## 指标与整卡结果

平台解析实时 interval 样本，保留 source、单位、采集时间和 sender 标记；预热 omitted 区间不计入。客户端结束事件中的 end.sum_received.bits_per_second 是整轮接收均值；原始输出在负载日志与报告中。失败或缺字段不能记作 0。

整卡分别列出两端口结果。只有原始 interval 数据证明有效测量重叠不少于较短测量的 90%，才能报告“并发两端口平均带宽之和”；严格共同窗口需要按真实时间对齐的区间计数加总，不能把采集时间视为设备同步时间。重叠无法确认时展示分端口结果与未验证原因，禁止整卡达标结论。单端口退化也必须披露。

## 调优与恢复

建议先排查链路/对端条件，再根据 CPU 与吞吐证据考虑每对并发 4→8→16。NUMA/CPU 证据充分时建议 load.cpus；它是本次负载的进程亲和性，退出即结束。更改 IRQ 需 tune_apply（action_id=affinity），参数 name、interface、irqs（IRQ 到 CPU 列表的映射）、pause_irqbalance。平台检查网卡所属 IRQ、在线 CPU、保存原值并回读；irqbalance 运行时须预览全机影响并明确暂停授权。恢复用 tune_restore name，操作也列入预览。

保持其他条件一致，只改变一个因素。每轮结果比较平均带宽、波动、重传和 CPU 证据；因环境波动可能出现假改善，应说明复测限制。用户确认后才执行下一轮。

## 检查与报告

environment 引用有效环境探测结果；measurement 引用完成的客户端 load_wait，综合核对两方向、整卡两对证据；cleanup 引用最终退出/恢复结果。steps 为 inspect、baseline、report。报告写入 artifact_write。finish 前平台还会检查未结束负载和未恢复调优记录。

官方参数依据：https://software.es.net/iperf/invoking.html 。内网维护者按部署版本的实际 --help 更新参数兼容性说明。
