# 第一阶段：真实卡片的只读任务画布

基线：上游 `v0.18.11`，提交 `3bcf7a33fb33109d03e368873570d0a7b402b210`。开发分支：`codex/personal-stage1`。

## 已实现

- `python -m misaka view tasks`：在终端读取当前项目的真实卡片，以依赖图展示。
- `--project DIR` 可重复，多个项目和多个依赖组共同出现在可平移的画布中。
- 方向键／hjkl 平移；拖动空白平移；Tab 选卡；n/p 切换依赖组；a 定位执行中卡片；g 回起点；q 退出。卡片与依赖均不在画布上编辑。
- 每 0.5 秒重新读取。状态变化保留选中卡片和视口；依赖变化重新布局。新增、删除和文件读取错误均通过现有数据反映。
- 创建卡片前显示空白状态；完成的依赖组保留显示；失败、受阻、等依赖和未知状态可区分。
- 文件损坏保留进程内最后一次成功读取的数据并标明“旧快照”。索引异常或代次不一致显示未知执行状态，不沿用旧的执行中标记。
- 缺失依赖和含环的依赖链有明确诊断，不将其默认为可执行 DAG。
- `--pane` 通过现有 Panel daemon 的 `pane.create` 托管普通终端子程序，不创建模型会话。需要 daemon 已在运行；不会隐式启动它。
- `--json` 输出一次诊断快照；数据异常返回 1，未建索引的正常空白项目返回 0。
- `--snapshot COLS ROWS` 输出一次纯文本画面，适合日志和静态预览。

## 运行

在此仓库根目录，使用 Python 3.12+ 且已安装 MISAKA 依赖的环境：

```bash
python -m misaka view tasks --project /absolute/path/P1 --project /absolute/path/P2
python -m misaka view tasks --project /absolute/path/P1 --pane
python -m misaka view tasks --project /absolute/path/P1 --json
```

开发环境可以使用已有 MISAKA 捆绑的 Python，从本仓库运行 `-m misaka`；不需要覆盖安装目录。直接运行 PATH 中旧安装的 `misaka` 不会自动使用本分支。

交互画布目前支持 POSIX；JSON 和静态画面不依赖 POSIX 终端接口。

## 数据职责与范围

`cards/t_*.md` 提供标题、执行者、持久状态、代次和依赖；SQLite 通过只读 URI 提供执行状态及运行信息。观察器不会迁移数据库、重建索引、认领卡片、调度工作或调用模型，也不初始化项目、home 和模型凭据。

SQLite WAL 读者会参与标准共享内存协调；这是 SQLite 的锁区，不是卡片或业务索引修改。端到端测试比较卡片、配置、数据库和 WAL 文件内容，排除 `-shm` 协调内存。

当前依赖组是现有卡片的连通分量，不是最终设计中的“任务请求”实体。一项没有依赖连边的工作可能被显示成多个组。执行标记来自索引中 `running` 的观察结果，不代表进程存活认证，也不是未来实例生命周期的 `active`。

本阶段保留旧调度和协作流程。Sister 会话仍可通过既有 `chat --read-only --session ...` 查看；画布内打开会话、任务请求/实例持久模型、Last Order 新工具及 `/research` 的统一接入留给后续阶段。当前 polling 实现随后可切换到公共状态服务。

## 验收

```bash
python tests/test_task_view_e2e.py /tmp/misaka-task-view-result.json
pytest -q tests/test_task_add_start.py tests/test_home_governance.py tests/test_extension_boundary.py
```

端到端测试通过公开 CLI 创建 Sister 与卡片，使用隔离的 `MISAKA_HOME` 和临时项目；通过外部文件／SQLite 写入模拟状态变化，在真实 PTY、libghostty 和原 Panel daemon 中验收。覆盖空白项目、多项目菱形依赖、输入与平移、自动刷新、缺失依赖、坏卡片恢复、索引故障恢复、含环依赖、代次不一致、完成后保留、终端缩放和退出，以及真实 daemon 托管。

执行中记录由外部测试输入注入，未调用模型或执行 Sister 工作。Panel 验收范围是其真实 daemon、PTY、输入与终端屏幕；没有宣称模拟了人工操作完整 Panel 客户端。

## 文件级变更与目录树

```text
misaka-personal/
├── misaka/
│   ├── cli/
│   │   ├── app.py                  # 修改：view 命令与只读入口
│   │   └── view.py                 # 新增：输出模式与 Panel 托管
│   ├── core/platform/
│   │   └── task_snapshot.py        # 新增：只读投影、依赖分组与异常诊断
│   └── ui/task_view/
│       ├── __init__.py             # 新增：画布模块
│       └── canvas.py               # 新增：终端布局、输入、刷新与恢复
├── tests/
│   └── test_task_view_e2e.py        # 新增：外部输入端到端验收
└── docs/personal/
    └── stage-01-task-view.md        # 新增：阶段说明与边界
```

下阶段优先实现任务请求、卡片和实例的持久关系及 Last Order 操作入口，使调度和画布共享同一状态来源，再扩展多 Last Order 协作和 `/research` 接入。
