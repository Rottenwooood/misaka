# 第三阶段：实例配置、会话与执行归属

基线：`7215d13`。分支：`codex/personal-stage3`。

本次交付可运行的实例会话基础。模板与实例不再只是元数据：前台聊天、daemon 卡片窗格、无界面卡片和联系会话都从实例装配配置。Last Order 内嵌的第二套 Sister 进程宿主已经删除，普通派活统一交给 daemon。Research 的专用规划、分叉和阶段工具尚未完成整体重构。

## 配置只有一个来源

模板版本不可变；实例固定版本，覆盖配置保存在同一数据库。配置字段为：

| 字段 | 含义 |
| --- | --- |
| `system` | 实例的指令／身份文本；与共享规则和角色职责一起装配 |
| `model` | 完整 `provider/model` 引用 |
| `thinking` | 引擎支持的思考等级 |
| `tools` | 显式活动工具名列表；省略时使用角色工具集 |
| `skills` | 会话启动时预加载的 Skill 名称 |
| `settings` | 实例自己的 `mcpServers`、`web` 配置节 |

覆盖按顶层字段替换，列表也整体替换。未知字段和错误类型在写入时拒绝；工具名在会话装配时校验。模型必须在实际引擎注册表中存在且有配置好的认证。

实例资源锚点位于 `cache/instances/<id>/profiles/...`，只适配现有引擎对资源目录及角色路径的要求。那里没有第二份 settings 或 SOUL 权威；配置与身份读取直接查询数据库。锚点可删除后重建。项目和共享 Skill 仍走原资源装配，卡片使用只读快照。

模型菜单的 Enter 仅改变当前会话；Ctrl+S 保存到实例覆盖，不改模板版本。思考等级默认值也保存到实例。已注册的 Sister 不装配通用 Agent 子进程创建工具，需要额外实例时交给 Last Order。

## 会话属于实例

`instances` 保存当前会话文件、会话代次、外部传输的会话标识及写者进程身份。物理 `tasks` 表已删除 `agent_id`、`session_file`、`session_dir`；`card_state` 从实例派生当前地址，`task_runs` 保留实际执行尝试的会话引用。

会话目录是 `state/sessions/instances/<instance-id>/`。启动使用数据库当前指针，删除了“扫描目录里最新 JSONL”的推测路径。指针缺失的旧日志明确报错；不会静默另开会话。跨卡恢复复用同一原生日志，跨项目执行会把工具的当前目录切到新卡片项目。

独占文件锁覆盖选择、加载、执行和退出清理；数据库同时公布写者 PID、进程身份和随机所有权标记。第二个写者在模型请求前被拒绝。已开工卡片在等待输入、分诊或复核时也保留实例名额；不会因暂时离开 running 状态而被其他卡片占用。异步清理在受保护的子任务中释放进程锁，打开会话的任务负责恢复自己的上下文栈。

旧会话替换记录进入 `instance_session_archive`，旧文件保留。显式状态命令需要预期代次：

```json
{"operations":[{"op":"instance.new-session","id":"lo","expected_epoch":1}]}
```

该命令要求实例没有活写者。正在打开的聊天使用 `/new`；实例的 `/new --carry` 同样创建干净会话，不复制旧上下文或父会话链。

Last Order 每次调用模型时获得当前状态快照：模板、实例配置、自己负责的请求，以及未结束卡片的合同、状态和依赖。该快照不读取旧对话、旧日志事件或旧会话内容，自动快照省略可能包含连接凭据的 settings 值，只标记其存在；规则文本继续经过现有凭据过滤器。完整配置仍保存在数据库，可由本机 state show 读取。当前消息和通知仍走现有交付机制，其完整订阅与归属重构属于后续消息阶段。

## 执行和查看

Last Order 的 SisterRuntime 现在只是 daemon 客户端，不持有 Sister 模型进程、运行句柄或卡片租约。关闭协调会话不会取消已派发的 Sister。Panel 最后一个客户端断开也不再关闭 daemon；明确执行 `misaka net stop` 才停止 daemon 及其窗格。

TUI 仍是原 herdr 移植的 BSP／PTY 框架。任务画布继续作为普通 PTY 窗格运行。历史会话点击打开查看原会话；有活写者时可附着原写者，无须创建第二个引擎。没有改成自定义原生标签页。

```bash
.venv/bin/python -m misaka chat --instance lo
.venv/bin/python -m misaka chat --instance lo --prompt '继续推进当前项目'
.venv/bin/python -m misaka net pause-instance sister
.venv/bin/python -m misaka net instance-status sister
.venv/bin/python -m misaka net resume-instance sister
.venv/bin/python -m misaka task start CARD_ID
.venv/bin/python -m misaka net stop
```

`--prompt` 运行显式输入的一轮，后台通知保留给交互会话处理；联系会话由自己的消息领取循环交付输入。只有一个 Last Order 时，`chat` 可省略实例；多个时需明确选择。旧 `chat --as`、自动选择最近记录的 `-c` 和可写历史选择器已移除。

暂停在请求或工具边界等待；已经发出的请求可能完成。它保留 writer、原会话和运行卡片的绑定，恢复继续原工作。关闭一个执行窗格仍是明确停止该进程；断开 Panel 是保留后台运行。两者语义不同。

共享日志不再作为卡片计费范围。每次实际模型请求的模型、用量和费用随原有 `budget_usage` 收据归属到明确卡片，Research 卡片费用按这些收据汇总，避免后卡重复计入前卡历史。

## 外部输入验收

```bash
.venv/bin/python tests/test_instance_sessions_e2e.py ../misaka-stage3-session-e2e.json
.venv/bin/python tests/test_work_state_e2e.py ../misaka-stage3-state-e2e.json
.venv/bin/python tests/test_task_view_e2e.py ../misaka-stage3-tui-e2e.json
```

第一条在隔离目录启动本机 HTTP/SSE 模型端点，实际运行 CLI、原生引擎、工具、daemon 和 PTY。检查真实请求中的配置与上下文；直接向真实终端输入 `/new --carry` 和模型菜单 Ctrl+S；让两张依赖卡使用同一 Sister 完成；最后用无界面 `task start` 跨到另一项目，调用 read 读取该项目独有的内容。没有调用付费模型。

后两条复验正式状态模型及完整原 Panel 的任务图、键鼠、窗口缩放、故障恢复和正常退出。每次测试停止隔离 daemon，不修改用户正在使用的 MISAKA_HOME。

## 当前边界

- 当前格式是 board 11、Panel 协议 56；拒绝旧格式，不迁移，不替换旧 daemon。
- 本阶段完成会话基础及普通执行归属，不等同于整个 MISAKA 改造完成。
- 单个 Last Order 跨项目派工的工具作用域仍需下一阶段调整；当前协调工具保留项目检查。执行器跨项目复用上下文已经实测，不代表完整多项目协调已完成。
- 管理模板／实例的专用 UI，以及完整管理工具、旧 profile 管理入口的替换，仍需后续阶段；目前正式对象使用 `state show/apply`。
- 专用 Research 节点的会话树、阶段作用域及其规划／汇合／报告流程仍待接入。普通 Research 卡片已使用实例执行基础，完整 `/research` 不能宣称完成。
- 消息身份、订阅和多 Last Order 通知归属，后续集中重构。完整跨会话检索和 LCM 记忆整理策略也未在本阶段新增。
- 当前验收在 Linux 完成，没有把结果外推为 macOS 或 Windows 实测。

文件级变更、完整目录树、终端帧、请求记录与回归清单见仓库外的 `outputs/misaka-stage3.html`。
