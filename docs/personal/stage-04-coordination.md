# 阶段 04：计划与显式派工

Last Order 用 `misaka_task` 查询自己的工作、保存请求和卡片 DAG、显式启动卡片。它用 `misaka_roster` 查看、建立和修订模板，或者登记绑定到某一模板版本的实例。

## 调用边界

- `misaka_task` 的参数是 `{"command": {"action": "show" | "save" | "start", ...}}`。`show` 不传请求编号时列出这个 LO 负责的请求，指定 `request_id` 则返回该请求和卡片。创建请求时传项目目录和标题；修改已有请求时传 `expected_revision`。新建卡片和修改依赖一起提交，拒绝成环和跨请求依赖。
- `save` 不启动模型。是否向用户展示计划、展示成什么样、是否等回复，遵循用户指令。产品不提供固定的人机交互格式或计划确认开关。
- `start` 要显式列出卡片编号与卡片代次，例如 `{"command":{"action":"start","cards":[{"id":"t_a12345","generation":2}]}}`。它不接受模型填写的 `confirmed`。服务端检查调用身份、请求归属、卡片代次和就绪状态；Panel 后台在认领任务时再次检查。同一个 LO 可以从自己的会话启动另一个项目的卡片，执行目录使用该卡片登记的项目。
- `misaka_roster` 也使用 `command`，动作是 `show`、`create_template`、`revise_template`、`create_instance`。修订模板产生新版本，不覆盖旧实例；查询结果不把本机连接配置送回模型。Sister 的工具注册表不包含这两个编排入口。
- Research 驱动保留原有计划与执行流程。普通任务的保存和派工不能接管研究驱动持有的卡片。

旧的 `misaka_board`、`misaka_state`、`misaka_card`、`misaka_card_link`、`misaka_dispatch`、`misaka_sister` 已从模型工具注册表删除。人的 `misaka state` 仍使用同一 SQLite 业务服务，可用于从空状态登记第一个 LO。历史阶段文档里的工具名以当时版本为准。

Panel 协议升到 57。运行中的旧后台不会被自动替换；可用独立 `MISAKA_HOME` 验收，或等旧后台的任务结束后显式停止它。

## 从外部验收

在项目根目录运行：

```sh
.venv/bin/python -m tests.test_coordination_e2e
```

验收从真实 `misaka chat` CLI 输入开始，经过原生模型循环、回环 HTTP/SSE、真实 Panel 后台和 Sister PTY。在临时项目内分别模拟「先看计划，收到下一条输入再派工」和「同一轮存计划、派工」，并检查跨项目工作目录、卡片代次、请求所有权、工具可见性和模板版本。脚本提供可重复的模型响应；这验证运行路径，不用于评估真实模型的指令遵从率。

本阶段的交付是计划、派工和模板版本入口。正式结果与 LO 验收、等待与恢复、统一通信、完整上下文查看仍需后续验收。
