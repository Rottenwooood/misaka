"""Real CLI -> formal state -> PTY -> original Panel acceptance; optional manual mode."""

import base64
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from misaka.ui.panel import ghostty, host_input, pane_input
from misaka.ui.panel.daemon import input_state
from tests.work_fixture import BODY, B, C, Scenario

if os.name == "posix":
    import fcntl
    import termios

FRAMES = []
CHECKS = []


def check(label):
    CHECKS.append(label)
    print("PASS " + label, flush=True)


class Terminal:
    def __init__(self, command, scenario, cols=110, rows=40):
        self.master, self.slave = os.openpty()
        fcntl.ioctl(
            self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )
        self.saved = termios.tcgetattr(self.slave)
        self.proc = subprocess.Popen(
            command,
            cwd=scenario.project,
            env=scenario.env,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
        )
        os.set_blocking(self.master, False)
        self.term = ghostty.Terminal(
            cols, rows, on_write_pty=lambda raw: os.write(self.master, raw)
        )

    def frame(self, label, needle):
        deadline = time.monotonic() + 8
        text = ""
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.1)[0]:
                try:
                    raw = os.read(self.master, 262144)
                except BlockingIOError:
                    continue
                self.term.write(raw)
                text = self.term.recent_text(self.term.rows)
                if needle in text and b"\x1b[?2026l" in raw:
                    FRAMES.append({"label": label, "text": text})
                    return text
            assert self.proc.poll() is None, (self.proc.returncode, text)
        raise AssertionError((label, needle, text))

    def key(self, value):
        raw = pane_input.encode_key(
            host_input.Key(value), input_state(SimpleNamespace(term=self.term))
        )
        assert raw
        os.write(self.master, raw)

    def prefix(self, value):
        os.write(self.master, b"\x02" + value.encode())

    def mouse(self, kind, x, y):
        raw = pane_input.encode_mouse(
            host_input.Mouse(kind, 0, x, y),
            x,
            y,
            input_state(SimpleNamespace(term=self.term)),
        )
        assert raw
        os.write(self.master, raw)

    def resize(self, cols, rows):
        self.term.resize(cols, rows)
        fcntl.ioctl(
            self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )
        os.kill(self.proc.pid, signal.SIGWINCH)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        assert termios.tcgetattr(self.slave) == self.saved
        os.close(self.master)
        os.close(self.slave)
        self.term.close()


class Daemon:
    def __init__(self, s):
        self.path = str(s.home / "run" / "net.sock")
        self.log = (s.base / "daemon.log").open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "misaka.ui.panel.daemon"],
            cwd=s.project,
            env=s.env,
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=self.log,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                self.rpc("ping")
                break
            except OSError:
                assert time.monotonic() < deadline and self.proc.poll() is None
                time.sleep(0.05)

    def rpc(self, method, params=None):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(self.path)
            client.sendall(
                (
                    json.dumps({"id": 1, "method": method, "params": params or {}})
                    + "\n"
                ).encode()
            )
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = client.recv(1048576)
                assert chunk
                reply += chunk
            result = json.loads(reply)
            assert not result.get("error"), result
            return result["result"]

    def close(self):
        if self.proc.poll() is None:
            try:
                self.rpc("server.stop")
            except OSError:
                self.proc.terminate()
            self.proc.wait(timeout=8)
        self.log.close()


def main(report_path=None, *, interactive=False):
    if os.name != "posix":
        raise SystemExit("Terminal acceptance requires POSIX")
    s = Scenario()
    try:
        assert s.snapshot()["state_status"] == "missing" and not s.home.exists()
        assert "尚无卡片" in s.cli("view", "tasks", "--snapshot", "100", "24").stdout
        check("未初始化项目空白状态明确；查看不建立状态库或 home")
        s.seed()
        s.demo_states()
        multi = [
            "--project",
            str(s.project),
            "--project",
            str(s.other),
            "--project",
            str(s.empty),
        ]
        snapshot = s.snapshot(*multi)
        assert len(snapshot["projects"]) == 3
        draft = next(p for p in snapshot["projects"] if p["name"] == "empty")
        assert len(draft["requests"]) == 1 and not draft["nodes"]
        check("多项目、多个真实请求和未拆分请求均有持久身份")
        view = Terminal([sys.executable, "-m", "misaka", "view", "tasks", *multi], s)
        try:
            text = view.frame("真实任务请求与实例", "实现中文导出")
            assert "req-export" in text and "i-2" in text and "● 执行中" in text
            view.key("right")
            view.frame("方向键平移", "视口 x=6 y=0")
            view.mouse("press", 100, 8)
            view.mouse("drag", 92, 8)
            view.mouse("release", 92, 8)
            view.frame("空白鼠标拖动", "视口 x=14 y=0")
            check("原 Pane 输入编码经真实 PTY 操作正式请求 DAG")
            view.key("a")
            before = view.frame("定位执行中卡片", f"选中 {B}")
            import re

            viewport = re.search(r"视口 x=\d+ y=\d+", before)[0]
            s.runtime(B, "ready")
            s.apply(
                [
                    {
                        "op": "plan.update",
                        "request_id": "req-export",
                        "expected_revision": 1,
                        "cards": [{"id": B, "title": "导出功能已更新"}],
                    }
                ]
            )
            text = view.frame("外部原子命令刷新画布", "导出功能已更新")
            assert viewport in text and f"选中 {B}" in text and "● 执行中" not in text
            check("外部状态命令更新后自动刷新，保留选中卡片和视口")
            s.apply(
                [
                    {
                        "op": "plan.update",
                        "request_id": "req-export",
                        "expected_revision": 2,
                        "cards": [
                            {"id": C, "needs": []},
                            {
                                "id": "t_777777",
                                "title": "同一请求的独立工作",
                                "body": BODY,
                                "instance_id": "i-3",
                                "needs": [],
                            },
                        ],
                    }
                ]
            )
            view.key("g")
            view.frame("原请求内修改依赖并新增独立节点", "/ v3 /")
            data = s.snapshot()
            assert len(data["projects"][0]["requests"]) == 2
            assert (
                "t_777777"
                in next(
                    r
                    for r in data["projects"][0]["requests"]
                    if r["id"] == "req-export"
                )["nodes"]
            )
            check("图的分组由请求身份决定；取消依赖不会拆成另一项任务")
            s.cli("task", "export", B)
            exported = s.project / "cards" / (B + ".md")
            exported.write_text("not a card")
            assert s.snapshot() == data
            exported.unlink()
            check("坏 Markdown 导出不改变状态，也不影响画布")
            backup = s.database.with_suffix(".saved")
            s.database.rename(backup)
            s.database.write_text("bad database")
            text = view.frame("状态库故障保留旧快照", "状态库暂不可用")
            assert "● 执行中" not in text
            data = s.snapshot(code=1)
            assert data["state_status"] == "error"
            s.database.unlink()
            backup.rename(s.database)
            view.key("g")
            view.frame("状态库恢复", "异常 0")
            check("数据库故障显示旧快照与未知执行状态；修复后恢复")
            view.resize(61, 22)
            text = view.frame("中文窄窗口", "61x22")
            assert "\ufffd" not in text
            view.resize(130, 44)
            view.frame("恢复大窗口", "130x44")
            view.key("q")
            assert view.proc.wait(timeout=5) == 0
            check("中文窗口缩放和正常退出，恢复终端属性")
        finally:
            view.close()
        s.execute("UPDATE tasks SET status='done'")
        assert "全部完成" in s.cli("view", "tasks", "--snapshot", "110", "40").stdout
        assert (
            "尚未拆分"
            in s.cli(
                "view", "tasks", "--project", str(s.empty), "--snapshot", "110", "24"
            ).stdout
        )
        check("完成后任务图保留；未拆分请求不会误显示为全部完成")
        s.cli("view", "tasks", "--pane", code=1)
        daemon = Daemon(s)
        try:
            pane = json.loads(s.cli("view", "tasks", "--pane", *multi).stdout)[
                "pane_id"
            ]
            daemon.rpc("pane.resize", {"id": pane, "rows": 40, "cols": 110})
            deadline = time.monotonic() + 8
            while (
                "任务画布"
                not in daemon.rpc("pane.read", {"id": pane, "strip": True})["text"]
            ):
                assert time.monotonic() < deadline
                time.sleep(0.05)
            daemon.rpc(
                "pane.input", {"id": pane, "data": base64.b64encode(b"q").decode()}
            )
            deadline = time.monotonic() + 5
            while daemon.rpc("pane.read", {"id": pane})["alive"]:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            lo = daemon.rpc(
                "pane.create",
                {
                    "argv": [
                        sys.executable,
                        "-u",
                        "-c",
                        "import signal; print('LO EXTERNAL FIXTURE',flush=True); signal.pause()",
                    ],
                    "cwd": str(s.project),
                    "title": "Last Order",
                },
            )["pane_id"]
            if interactive:
                s.runtime(B, "running")
                print(
                    "交互验证：Ctrl+B 后按 t 开图；q 关图；Ctrl+B 后按 d 退出清理。测试窗格不调用模型。",
                    flush=True,
                )
                result = subprocess.run(
                    [sys.executable, "-m", "misaka", "panel"],
                    cwd=s.project,
                    env=s.env,
                    check=False,
                )
                if result.returncode:
                    raise SystemExit(result.returncode)
                return
            panel = Terminal([sys.executable, "-m", "misaka", "panel"], s, 140, 44)
            try:
                panel.frame("完整 Panel 启动", "LO EXTERNAL FIXTURE")
                panel.prefix("t")
                text = panel.frame("Panel 内打开正式任务 DAG", "任务画布")
                assert "req-export" in text
                sidebar = "\n".join(line[:24] for line in text.splitlines())
                assert "Tasks" not in sidebar
                panel.prefix("t")
                panel.key("right")
                panel.frame("复用现有画布与按键转发", "视口 x=6 y=0")
                assert (
                    len(
                        [
                            p
                            for p in daemon.rpc("panes.list")["panes"]
                            if p["alive"] and "view" in p["argv"]
                        ]
                    )
                    == 1
                )
                panel.prefix("1")
                panel.frame("切回原窗格", "LO EXTERNAL FIXTURE")
                panel.prefix("t")
                panel.frame("重新聚焦任务", "视口 x=6 y=0")
                panel.mouse("press", 128, 36)
                panel.mouse("drag", 120, 36)
                panel.mouse("release", 120, 36)
                panel.frame("完整 Panel 鼠标转发", "视口 x=14 y=0")
                panel.key("q")
                panel.frame("关闭画布保留原窗格", "LO EXTERNAL FIXTURE")
                assert daemon.rpc("pane.read", {"id": lo})["alive"]
                panel.prefix("d")
                assert panel.proc.wait(timeout=8) == 0
                check(
                    "完整原 Panel：开图、重复聚焦、切换、键鼠转发、关闭；查看器不列为 Agent"
                )
            finally:
                panel.close()
        finally:
            daemon.close()
        result = {
            "passed": len(CHECKS),
            "checks": CHECKS,
            "frames": FRAMES,
            "scope": "formal SQL state -> real CLI/PTY/libghostty -> original Panel daemon and full client; no models",
        }
        if report_path:
            Path(report_path).write_text(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
        print(json.dumps({"passed": len(CHECKS)}, ensure_ascii=False))
    finally:
        s.close()


def test_external_task_view():
    import pytest

    if os.name != "posix" or ghostty.unavailable():
        pytest.skip("Requires POSIX and libghostty")
    main()


if __name__ == "__main__":
    if sys.argv[1:] == ["--interactive"]:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit("Interactive mode requires a terminal")
        main(interactive=True)
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else None)
