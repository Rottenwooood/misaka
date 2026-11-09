"""External-input acceptance test. Run with the project's Python, no pytest required.

Uses public CLI commands, real files/SQLite, real PTYs, libghostty and the real
Panel daemon protocol. No mocks of snapshot/layout/runtime implementation.
The running marker is injected persistence input; no model/worker execution is claimed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAMES = []
CHECKS = []


def check(label):
    CHECKS.append(label)
    print("PASS " + label, flush=True)


def digest(roots):
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for root in roots
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith("-shm")
    }


def main(report_path=None):
    if os.name != "posix":
        raise SystemExit("PTY acceptance currently requires POSIX.")
    import fcntl
    import termios
    from types import SimpleNamespace

    from misaka.ui.panel import ghostty, host_input, pane_input
    from misaka.ui.panel.daemon import input_state

    with tempfile.TemporaryDirectory(prefix="mv-") as temp:
        base = Path(temp)
        project = base / "P1"
        other = base / "P2"
        empty = base / "empty"
        for p in (project, other, empty):
            p.mkdir()
        data_home = base / "home"
        env = {
            **os.environ,
            "MISAKA_HOME": str(data_home),
            "PYTHONPATH": str(ROOT),
            "GIT_AUTHOR_NAME": "MISAKA E2E",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "MISAKA E2E",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        # No provider credentials are needed by any command below.
        for key in list(env):
            if key.endswith(("_API_KEY", "_AUTH_TOKEN")):
                env.pop(key)

        def cli(*args, cwd=project, code=0):
            out = subprocess.run(
                [sys.executable, "-m", "misaka", *args],
                cwd=cwd,
                check=False,
                env=env,
                capture_output=True,
                text=True,
                timeout=25,
            )
            assert out.returncode == code, (
                args,
                out.returncode,
                out.stdout,
                out.stderr,
            )
            return out.stdout

        def snapshot(*args, code=0):
            return json.loads(cli("view", "tasks", *args, code=code))

        initial = snapshot("--json", "--project", str(empty))
        assert not initial["projects"][0]["nodes"]
        assert not data_home.exists() and not list(empty.iterdir())
        assert "尚无卡片" in cli(
            "view", "tasks", "--project", str(empty), "--snapshot", "100", "24"
        )
        assert not data_home.exists()
        check("未初始化目录：空白状态明确，查看不创建 home、卡片或数据库")

        cli("create", "test-sister", "--desc", "offline acceptance")
        body = "## goal\nTest\n## boundaries\nTemporary project only\n## acceptance criteria\n- A visible output exists.\n"

        def add(title, needs=(), cwd=project):
            args = ["task", "add", title, "--to", "test-sister", "--body", body]
            for parent in needs:
                args.extend(["--needs", parent])
            out = cli(*args, cwd=cwd)
            return re.search(r"Added (t_\w+):", out)[1]

        a = add("调查现有接口")
        b = add("实现中文导出", [a])
        c = add("编写使用说明", [a])
        d = add("集成验收", [b, c])
        e = add("独立交付")
        f = add("等待外部数据", cwd=other)
        database = data_home / "state" / "board.db"

        def runtime(tid, **fields):
            # Simulate the external owner writing its durable runtime state, never call
            # viewer internals or mock their return values.
            with sqlite3.connect(database) as con:
                con.execute(
                    "UPDATE tasks SET "
                    + ",".join(k + "=?" for k in fields)
                    + " WHERE id=?",
                    [*fields.values(), tid],
                )

        def replace(tid, old, new, cwd=project):
            path = cwd / "cards" / (tid + ".md")
            content = path.read_text()
            assert old in content, (old, content)
            pending = path.with_suffix(".tmp")
            pending.write_text(content.replace(old, new), encoding="utf-8")
            pending.replace(path)

        replace(a, "status: ready", "status: done")
        replace(e, "status: ready", "status: done")
        replace(f, "status: ready", "status: blocked", cwd=other)
        runtime(a, status="done")
        runtime(e, status="done")
        runtime(b, status="running", worker_pid=os.getpid())
        runtime(f, status="blocked", block_reason="等外部数据")
        multi = [
            "--project",
            str(project),
            "--project",
            str(other),
            "--project",
            str(empty),
        ]
        before = digest([data_home, project, other, empty])
        data = snapshot("--json", *multi)
        p1 = next(p for p in data["projects"] if p["name"] == "P1")
        nodes = {n["id"]: n for n in p1["nodes"]}
        assert len(p1["groups"]) == 2 and nodes[b]["running"] is True
        assert set(nodes[d]["needs"]) == {b, c}
        group = next(g for g in p1["groups"] if d in g["nodes"])
        assert group["ranks"][a] < group["ranks"][b] < group["ranks"][d]
        assert len(data["projects"]) == 3
        cli("view", "tasks", "--snapshot", "110", "40", *multi)
        after = digest([data_home, project, other, empty])
        assert after == before, {
            key: (before.get(key), after.get(key))
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        }
        check(
            "公开 CLI 创建真实卡片：多项目、菱形依赖、完成与受阻状态；读取前后业务文件哈希不变（SQLite 协调共享内存除外）"
        )

        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
        saved = termios.tcgetattr(slave)
        proc = subprocess.Popen(
            [sys.executable, "-m", "misaka", "view", "tasks", *multi],
            cwd=project,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.set_blocking(master, False)
        term = ghostty.Terminal(
            110, 40, on_write_pty=lambda data: os.write(master, data)
        )

        def frame(label, contains):
            deadline = time.monotonic() + 7
            text = ""
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        raw = os.read(master, 262144)
                    except BlockingIOError:
                        continue
                    term.write(raw)
                    text = term.recent_text(term.rows)
                    if contains in text and raw.endswith(b"\x1b[?2026l"):
                        FRAMES.append({"label": label, "text": text})
                        return text
                assert proc.poll() is None, ("viewer exited", proc.returncode, text)
            raise AssertionError((label, contains, text))

        def key(value):
            raw = pane_input.encode_key(
                host_input.Key(value), input_state(SimpleNamespace(term=term))
            )
            assert raw
            os.write(master, raw)

        def mouse(kind, x, y):
            raw = pane_input.encode_mouse(
                host_input.Mouse(kind, 0, x, y),
                x,
                y,
                input_state(SimpleNamespace(term=term)),
            )
            assert raw
            os.write(master, raw)

        try:
            text = frame("真实卡片初始画布", "实现中文导出")
            assert "● 执行中" in text
            assert input_state(SimpleNamespace(term=term))["mouse_encoding"] == "sgr"
            key("right")
            frame("方向键平移", "视口 x=6 y=0")
            mouse("press", 100, 8)
            mouse("drag", 92, 8)
            mouse("release", 92, 8)
            frame("空白鼠标拖动", "视口 x=14 y=0")
            key("g")
            frame("回到原点", "视口 x=0 y=0")
            check("真实 PTY + libghostty：原 Panel 输入编码可操作画布与空白拖动")

            key("a")
            active_frame = frame("定位执行中卡片", f"选中 {b}")
            viewport = re.search(r"视口 x=\d+ y=\d+", active_frame)[0]
            runtime(b, status="ready", worker_pid=None)
            replace(b, "实现中文导出", "导出功能已更新")
            text = frame("外部文件和运行状态刷新", "导出功能已更新")
            assert "● 执行中" not in text
            assert f"选中 {b}" in text and viewport in text
            check("运行中自动刷新外部文件与 SQLite；选中卡片保持不变")

            # Make dependencies editable externally while the viewer is already open.
            path = project / "cards" / (b + ".md")
            original_b = path.read_text()
            replace(b, f"- {a}", "- t_missing")
            frame("外部依赖变更", "缺少依赖")
            missing = snapshot("--json", code=1)
            assert next(n for n in missing["projects"][0]["nodes"] if n["id"] == b)[
                "missing_needs"
            ] == ["t_missing"]
            path.write_text(original_b)
            frame("依赖恢复", f"选中 {b} / 导出功能已更新 / 待处理")
            path.write_text("---\nid: [broken\n---\n")
            text = frame("坏文件保留旧快照", "卡片读取失败")
            assert "导出功能已更新" in text and "卡片读取失败" in text
            path.write_text(original_b)
            frame("坏文件修复恢复", f"选中 {b} / 导出功能已更新 / 待处理")
            check("外部修改依赖即时生效；坏卡片可见且保留旧快照，修复后恢复")

            # A database read failure must not retain an old running marker.
            db_backup = database.with_suffix(".saved")
            database.rename(db_backup)
            database.write_text("not a database")
            frame("运行索引故障", "运行索引读取失败")
            error = snapshot("--json", code=1)
            assert error["runtime_state"] == "error"
            assert all(
                n["running"] is None for p in error["projects"] for n in p["nodes"]
            )
            database.unlink()
            db_backup.rename(database)
            key("g")
            frame("索引恢复", "视口 x=0 y=0")
            check("索引故障显示未知执行状态；修复后继续刷新")

            # Resize a running viewer through the actual terminal interface.
            term.resize(61, 22)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 22, 61, 0, 0))
            os.kill(proc.pid, signal.SIGWINCH)
            text = frame("中文窄窗口", "61x22")
            assert "\ufffd" not in text
            term.resize(130, 44)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 44, 130, 0, 0))
            os.kill(proc.pid, signal.SIGWINCH)
            frame("恢复大窗口", "130x44")
            before_quit = digest([data_home, project, other, empty])
            key("q")
            assert proc.wait(timeout=4) == 0
            assert termios.tcgetattr(slave) == saved
            assert digest([data_home, project, other, empty]) == before_quit
            check("PTY resize、中文渲染及 q 退出；退出恢复 termios 且不修改任务数据")
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=4)
            term.close()
            os.close(master)
            os.close(slave)

        # A normal external termination must restore raw mode as well.
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        saved = termios.tcgetattr(slave)
        proc = subprocess.Popen(
            [sys.executable, "-m", "misaka", "view", "tasks"],
            cwd=project,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.set_blocking(master, False)
        term = ghostty.Terminal(100, 24)
        try:
            frame("外部终止前", "任务画布")
            proc.terminate()
            assert proc.wait(timeout=4) == 128 + signal.SIGTERM
            assert termios.tcgetattr(slave) == saved
            check("外部 SIGTERM 退出也恢复终端属性")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=4)
            term.close()
            os.close(master)
            os.close(slave)

        # Invalid graph diagnostics are tested through the CLI, not graph helpers.
        replace(a, "status: done", f"status: done\nneeds:\n- {d}")
        data = snapshot("--json", code=1)
        assert any(g["cycle_or_downstream"] for g in data["projects"][0]["groups"])
        replace(a, f"status: done\nneeds:\n- {d}", "status: done")
        replace(b, "generation: 1", "generation: 2")
        data = snapshot("--json", code=1)
        assert (
            next(n for n in data["projects"][0]["nodes"] if n["id"] == b)["running"]
            is None
        )
        replace(b, "generation: 2", "generation: 1")
        check("环及其下游有明确诊断；文件/索引代次不一致不显示执行中")

        for path in project.glob("cards/t_*.md"):
            text = path.read_text()
            text = re.sub(r"^status: \w+", "status: done", text, flags=re.MULTILINE)
            path.write_text(text)
        completed = cli("view", "tasks", "--snapshot", "110", "40")
        assert "全部完成" in completed
        check("整组完成后保留可查看的任务树")

        # The existing daemon actually hosts our child program: no replacement panel.
        # An absent daemon must not be started implicitly by --pane.
        cli("view", "tasks", "--pane", code=1)
        assert not (data_home / "run" / "net.sock").exists()
        daemon_log = (base / "daemon.log").open("wb")
        daemon = subprocess.Popen(
            [sys.executable, "-m", "misaka.ui.panel.daemon"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=daemon_log,
            stderr=daemon_log,
            start_new_session=True,
        )
        sock_path = str(data_home / "run" / "net.sock")

        def rpc(method, params=None):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                client.connect(sock_path)
                client.sendall(
                    (
                        json.dumps({"id": 1, "method": method, "params": params or {}})
                        + "\n"
                    ).encode()
                )
                reply = b""
                while not reply.endswith(b"\n"):
                    chunk = client.recv(1048576)
                    assert chunk, (method, reply)
                    reply += chunk
                out = json.loads(reply)
                assert not out.get("error"), out
                return out["result"]

        try:
            deadline = time.monotonic() + 12
            while True:
                try:
                    rpc("ping")
                    break
                except OSError:
                    assert time.monotonic() < deadline and daemon.poll() is None
                    time.sleep(0.1)
            pane = json.loads(cli("view", "tasks", "--pane", *multi))["pane_id"]
            rpc("pane.resize", {"id": pane, "rows": 40, "cols": 110})
            deadline = time.monotonic() + 8
            while True:
                screen = rpc("pane.read", {"id": pane, "strip": True})
                if "任务画布" in screen["text"] and "导出功能已更新" in screen["text"]:
                    break
                assert time.monotonic() < deadline, screen
                time.sleep(0.1)
            panes = rpc("panes.list")["panes"]
            entry = next(p for p in panes if p["id"] == pane)
            assert not entry.get("card") and entry["alive"]
            rpc("pane.input", {"id": pane, "data": base64.b64encode(b"q").decode()})
            deadline = time.monotonic() + 4
            while rpc("pane.read", {"id": pane})["alive"]:
                assert time.monotonic() < deadline
                time.sleep(0.1)
            rpc("ping")
            check(
                "真实原 Panel daemon 托管 --pane，接收输入并退出；查看器不占用 card，退出不关闭 daemon"
            )
        finally:
            try:
                rpc("server.stop")
            except (OSError, AssertionError):
                daemon.terminate()
            daemon.wait(timeout=8)
            daemon_log.close()

    result = {
        "passed": len(CHECKS),
        "checks": CHECKS,
        "frames": FRAMES,
        "scope": "CLI + external files/SQLite + real PTY + libghostty + original Panel daemon; no model execution",
    }
    if report_path:
        Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {"passed": len(CHECKS), "scope": result["scope"]}, ensure_ascii=False
        )
    )


def test_external_task_view():
    import pytest

    from misaka.ui.panel import ghostty

    if os.name != "posix":
        pytest.skip("PTY acceptance requires POSIX")
    if reason := ghostty.unavailable():
        pytest.skip(reason)
    main()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
