# 诊断模拟样例

这是开发测试夹具，不是可安装的用户 Skill，也不包含 VXLAN 内部流程。
服务将本文件内容作为能力包 SKILL.md 导入，用于演示约定、授权和检查证据。

保持同一会话，发送 diagnose，再发送 show test-config。
仅当工具结果包含 flag=enabled 时通过 flag-present 检查。
模拟器并不调用 AI，不建立 SSH 连接。
