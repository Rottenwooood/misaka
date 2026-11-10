# 第二阶段：统一持久状态

分支：`codex/personal-stage2`。基线：`e378654`。

## 这一阶段完成什么

任务请求、卡片、依赖、模板版本和实例身份统一保存在现有 board 数据库中。旧卡片文件权威和“文件 + 运行索引”拼接路径已经移除，没有双写、旧格式解释器、自动迁移或回退分支。

- `templates(id, revision)`：不可变的配置版本，角色为 Last Order 或 Sister；修改产生新版本并检查预期版本。
- `instances(id)`：引用确定的模板版本，保存覆盖配置与标签。已有实例不随模板更新改变配置。
- `work_requests(id)`：人的任务请求，明确项目、负责的 Last Order 实例、目标和计划版本。
- `tasks(id)`：卡片合同、当前状态、执行实例及执行代次。每张卡必须属于一个请求并绑定一个 Sister 实例。
- `task_dependencies(child_id, parent_id)`：卡片依赖。只允许同一请求内的有向无环图。
- `card_state`：只读数据库视图。执行路由所需的模板信息由实例关系派生，不重复存储在卡片中。
- 卡片日志使用现有事件表，URL 引用进入数据库；附件和交付材料仍是普通文件。

画布按真实请求身份分组。同一请求中的独立节点仍在同一张任务图中；未拆分的请求也能显示；完成后的任务图保留。

## 变更与并发语义

`plan.update` 在一个事务中提交一组卡片与边，再校验完整 DAG；支持同批新节点前向引用和整批换边。失败时卡片、实例、模板、事件及计划版本一起回滚。

修改计划必须提供 `expected_revision`。并发写者只能有一个提交成功，另一方收到版本冲突。修改未开工卡片时推进卡片代次，并在事件中保留修改前的合同和绑定关系；执行器持有旧内容时不能认领新合同。

同一实例最多有一张 `running` 卡片，由认领事务和数据库唯一约束共同保证。这里的 `running` 是卡片执行状态，不是完整实例生命周期。取消保留卡片和依赖；仍被引用的卡片不能直接删除。

运行中和已完成的合同不能由持久状态命令静默改写。需要生命周期动作或新的后续卡片；直接暂停、恢复和连续上下文将在第三阶段实现。

## 公共入口

```bash
.venv/bin/python -m misaka state show
.venv/bin/python -m misaka state apply --file operations.json
.venv/bin/python -m misaka state apply --file -
.venv/bin/python -m misaka task add TITLE --request REQUEST --instance INSTANCE --body-file contract.md
.venv/bin/python -m misaka task export CARD_ID
```

`state show` 和画布只读，不初始化项目或状态库。`state apply` 输入为 `{"operations": [...]}`，同一批操作作为一个事务提交。支持：

| 操作 | 必填字段 |
|---|---|
| `template.create` | `id`, `role`, `config` |
| `template.revise` | `id`, `expected_revision`, `config` |
| `instance.create` | `id`, `template_id`, `template_revision`；可选 `overrides`, `label` |
| `request.create` | `id`, `title`, `workspace`, `owner_id`；可选 `body` |
| `request.update` | `id`, `expected_revision`；可改 `title`, `body`, `owner_id` |
| `plan.update` | `request_id`, `expected_revision`, `cards` |

新卡片需要 `id`（`t_` 后六位十六进制）、`title`, `body`, `instance_id`；可设置 `needs`, `priority`, `model`, `reviewer`。合同必须包含可检验的 `## acceptance criteria`。未开工卡片还可设置 `cancel: true`。

Last Order 的 `misaka_state` 工具与 CLI 共用这些操作；原建卡工具改为显式请求和实例身份，使用同一计划服务。研究流程创建请求并通过同一服务建卡、创建实例及保存依赖；完整研究驱动的重构仍在后续研究阶段。

Markdown 只由显式 `task export` 生成，不自动维护。修改、损坏或删除导出都不会改变状态。查询不再“重建索引”或复活已删除卡片。

## 格式与开发运行环境

本分支只接受 board 格式 10、Panel 协议 55。旧格式明确拒绝且不改写；协议不匹配时不停止或替换现有 daemon。验收使用独立 `MISAKA_HOME`，不改用户现有项目数据。

开发解释器位于本仓库 `.venv/bin/python`，已以 editable 方式安装当前源码。依赖复用原解释器已有包，未覆盖原安装。子进程即使清理 `PYTHONPATH`，也仍使用当前分支，避免误加载已安装旧版。

第三阶段尚待实现：将模板配置装配到运行会话，激活、暂停和恢复实例，跨卡复用上下文，以及 Last Order 会话替换。当前完成的是身份、配置版本和绑定关系的持久化，不把这些记录等同于已经完成运行生命周期。

## 外部输入验收

```bash
.venv/bin/python tests/test_work_state_e2e.py /tmp/misaka-state-result.json
.venv/bin/python tests/test_task_view_e2e.py /tmp/misaka-view-result.json
.venv/bin/python tests/test_task_view_e2e.py --interactive
```

第三条先做隔离检查，然后打开真实 Panel。`Ctrl+B` 后按 `t` 开图；方向键或空白拖动平移；`q` 关闭画布；`Ctrl+B` 后按 `d` 退出并清理。

验收从外部 CLI 创建真实持久对象，跨进程读取并制造并发写入、坏导出、坏数据库与旧协议响应，再检查真实 PTY、libghostty 和完整 Panel 输出。测试窗格和执行中状态为模拟输入，不调用模型。

保留了执行、消息和研究入口的有效回归测试，并用显式实体夹具替换旧构造方式。删除了“文件覆盖数据库”的测试，以及要求旧发布数据自动打开的兼容测试、旧样本和生成器；拒绝旧格式且保持其内容不变由新端到端场景覆盖。

文件级修改和目录树见本阶段 HTML 报告；测试记录与图像帧均来自实际运行。
