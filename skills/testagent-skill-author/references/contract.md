# v1 框架约定

## 包结构

- `skill.json`：`id`（小写字母、数字、下划线、连字符）、`name`、`version`。
- `SKILL.md`：供 Agent 阅读的流程说明，包含标准 Skill frontmatter。
- `contract.json`：结构化展示与检查约定。
- `scripts/`：UTF-8 配套脚本。包内文件仅支持文本，二进制部署附件应从任务文件区提供。

ZIP 可以直接包含上述文件，也可以包含唯一顶层目录。禁止绝对路径、父目录跳转和符号链接，最大 32 MiB。

## contract.json 示例

```json
{
  "schema_version": 1,
  "roles": [{"id": "controller", "name": "目标控制器"}],
  "parameters": {
    "type": "object",
    "properties": {"label": {"type": "string", "title": "测试标记"}},
    "required": ["label"]
  },
  "steps": [{"id": "inspect", "name": "环境检查", "required": true}],
  "checks": [{"id": "marker", "step_id": "inspect", "required": true, "assertion": {"contains": "READY"}}],
  "critical_actions": [{"id": "enter-diagnostic", "step_id": "inspect", "description": "进入诊断视图，保持其他配置不变"}],
  "scripts": [],
  "recovery": "根据变更前备份和实际执行记录提出恢复；核对现场后执行并验证。不确定的原值交给用户确认。"
}
```

steps、checks 必须明确 required。检查和关键操作引用有效 step_id。scripts 中的路径必须存在。`assertion` 支持 `contains` 字符串，或 `field` 点分隔字段加 `equals` 精确值；缺省 assertion 表示 Agent 根据工具证据判断，不能宣称为确定性验证。

## 统一 testagent 工具

调用格式为一个 JSON 对象，`action` 指定操作；设备相关操作填写 `role`，关键操作填写 `action_id`。

| action | 主要参数 | 结果 |
|---|---|---|
| skill_read | path，默认 SKILL.md | 包内文件内容 |
| files / file_read | file_read 需 file_id | 任务附件清单或文本 |
| artifact_write | name、text | 输出文件 ID |
| exec | role、command、timeout | operation_id、stdout、stderr、exit_code |
| script | role、path 或 file_id、args、timeout | 脚本执行结果；Linux 需 sh 和 setsid |
| shell_open | role、expect、timeout | session_id、提示符匹配结果 |
| shell_send | role、session_id、text、expect、timeout；预期重启可用 expect_disconnect=true 替代 expect | 同一交互视图的输出 |
| shell_close | role、session_id | 关闭终端，不等同终止远端后台进程 |
| remote_read | role、path | UTF-8 文件内容 |
| remote_write | role、path、text | 回读验证及变更前备份 file_id |
| upload | role、file_id、path | 上传与回读验证 |
| download | role、path、name（可选） | 任务输出 file_id |
| wait_connected | role、timeout | 新连接成功；仍需验证重启后业务状态 |
| step | step_id | 步骤完成 |
| check | check_id、evidence_operation、passed | 检查结果；有 assertion 时由框架判定 |
| ask | text | 等待用户回答，没有自动超时 |
| finish | summary | 请求结束；必要检查未通过时框架拒绝成功 |

每个设备操作返回 operation_id。check 的 evidence_operation 必须引用当前任务已经结束的工具操作。终端命令没有可靠退出码时使用提示符/输出验证，不填虚假的 0。

框架会读取 Skill，提供固定角色、参数和文件清单；不需要模型知道密码、设备库结构或本地数据库路径。

## 变更预览

框架要求每个任务先调用 `preview`，传入 `summary`、`impact`、`verification` 和 `operations`。
`operations` 是准备执行的设备工具参数对象数组，最多 200 项。所有环境都必须等待用户确认。
平台逐项核对命令、角色和顺序。只有由 `shell_open` 返回的 `session_id` 可在执行时补入。
需要先探测再决定配置时，先预览探测阶段；取得结果后再提交配置阶段预览。不得用通配命令代替实际操作。
收到用户新要求后，原预览失效；必须重新提交。任务完成前应执行完当前预览列出的操作。

## 实时拓扑与配置对比（可选）

在 `contract.json` 中增加如下字段。拓扑表示 Skill 声明的连接关系，平台不会推断物理接线。

```json
{
  "topology": [{"from": "controller", "to": "switch", "label": "管理网络"}],
  "comparisons": [{
    "id": "switch-config",
    "name": "交换机当前配置",
    "role": "switch",
    "operation": {"action": "shell_send", "text": "display current-configuration", "expect": "(?m)^<[^>]+>\\s*$", "timeout": 60}
  }]
}
```

连接两端和采集角色必须已在 `roles` 中声明。采集方法支持 `exec`、`shell_send`、`remote_read`。
业务作者必须保证采集命令只读、输出完整，按目标设备处理分页和提示符。
变更前后分别调用声明的 operation，额外添加 `role`、`capture_id`（如 switch-config）、
`capture_phase`（before 或 after），交互命令还需实际 session_id。这些采集操作同样列入 preview。
平台直接保存工具返回文本和 operation_id，输出逐行差异；每个阶段只保存一次，原始证据不可覆盖。
缺少任一阶段时展示无法对比。文本相同仅代表两次采集结果一致，不能替代业务检查点。

## 持续负载与性能

平台支持 workloads 声明、tool_list/tool_deploy、load_start/load_status/load_wait/load_stop，以及 tune_apply/tune_restore。具体参数、执行语义、指标与工具包格式见 [性能场景约定](performance.md)。所有远端动作仍通过上述预览与授权链；负载实际结束、调优恢复后才能完成任务。
