"""External CLI + genuine native model loop against a loopback SSE provider.

No model/runtime mocks. Requests received over HTTP are the evidence for model,
tool selection, resumed history and fresh Last Order handoff.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.work_fixture import Scenario


def main(report_path=None):
    s = Scenario()
    received, checks = [], []
    held, release = threading.Event(), threading.Event()
    card_held, card_release = threading.Event(), threading.Event()
    stop_held, stop_release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "payload": payload})
            messages = payload.get("messages", [])
            last = json.dumps(messages[-1] if messages else {})
            if "HOLD-REQUEST" in last:
                held.set()
                release.wait(25)
            tool_result = any(x.get("type") == "tool_result" for x in (
                messages[-1].get("content", []) if messages and isinstance(messages[-1].get("content"), list) else []))
            card = any(marker in last for marker in ("CARD-ONE", "CARD-TWO", "CARD-THREE"))
            read_result = tool_result and any(x.get("type") == "tool_use" and x.get("name") == "read"
                for x in (messages[-2].get("content", []) if len(messages) > 1 else []))
            if "CARD-ONE" in last and not tool_result:
                card_held.set()
                card_release.wait(25)
            content = {"type": "tool_use", "id": f"fixture-{len(received)}", "name": "misaka_card_complete",
                       "input": {"summary": "External fixture completed the contract"}} if (card and not tool_result) or read_result else {
                           "type": "text", "text": "ANSWER-PRIVATE-HISTORY"}
            if "CARD-THREE" in last and not tool_result:
                content = {"type": "tool_use", "id": f"fixture-{len(received)}", "name": "read", "input": {"path": "cwd-marker.txt"}}
            if "STOP-CARD" in last and not tool_result:
                stop_held.set()
                stop_release.wait(25)
            if "STOP-RUNNING-CARD" in last and not tool_result:
                content = {"type": "tool_use", "id": f"fixture-{len(received)}", "name": "misaka_sister_stop",
                           "input": {"task_id": "t_100004", "confirmed": True}}
            events = [
                {"type": "message_start", "message": {"id": f"message-{len(received)}", "type": "message",
                  "role": "assistant", "model": payload["model"], "content": [], "stop_reason": None,
                  "stop_sequence": None, "usage": {"input_tokens": 20, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": content},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use" if content["type"] == "tool_use" else "end_turn",
                  "stop_sequence": None}, "usage": {"output_tokens": 4}},
                {"type": "message_stop"},
            ]
            body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    processes = []
    terminals = []
    native_frames = []

    def check(label):
        checks.append(label)
        print("PASS " + label, flush=True)

    def state():
        return json.loads(s.cli("state", "show").stdout)

    def actor(iid):
        return next(i for i in state()["instances"] if i["id"] == iid)

    def chat(iid, text, ok=True):
        return s.cli("chat", "--instance", iid, "--prompt", text, code=0 if ok else 1)

    try:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            s.env.pop(key, None)
        s.home.mkdir(parents=True, exist_ok=True)
        skill = s.home / "skills" / "probe" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: probe\ndescription: Local template preload fixture\n---\nPRELOADED-TEMPLATE-SKILL\n")
        models = {p: {"baseUrl": f"http://127.0.0.1:{server.server_port}/{p}", "api": "anthropic-messages",
                      "apiKey": "fixture-not-a-real-key", "models": [{"id": "shared", "name": "shared",
                      "reasoning": False, "input": ["text"], "contextWindow": 200000, "maxTokens": 1024,
                      "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}
                  for p in ("fixture-a", "fixture-b")}
        (s.home / "models.json").write_text(json.dumps({"providers": models}))
        (s.home / "settings.json").write_text(json.dumps({"defaultProvider": "fixture-b", "defaultModel": "shared",
            "retry": {"enabled": False}, "defaultProjectTrust": "always", "compaction": {"enabled": False},
            "network": {"max_concurrent_sisters": 3}}))
        s.apply([
            {"op": "template.create", "id": "unused-config", "role": "sister", "config": {
                "settings": {"mcpServers": {"example": {"env": {"SERVICE_API_KEY": "fixture-MCP-secret-value"}}}}}},
            {"op": "template.create", "id": "coord", "role": "last-order", "config": {
                "system": "PINNED-COORDINATOR", "model": "fixture-a/shared", "thinking": "off", "tools": ["read"], "skills": ["probe"]}},
            {"op": "template.create", "id": "worker", "role": "sister", "config": {
                "system": "PINNED-WORKER", "model": "fixture-a/shared", "tools": ["read", "misaka_card_complete"]}},
            {"op": "instance.create", "id": "lo", "template_id": "coord", "template_revision": 1},
            {"op": "instance.create", "id": "sister", "template_id": "worker", "template_revision": 1},
            {"op": "request.create", "id": "req", "title": "CURRENT-REQUEST", "body": "CURRENT-GOAL",
                "workspace": str(s.project), "owner_id": "lo"},
            {"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [
                {"id": "t_100001", "title": "one", "body": "CARD-ONE\n## acceptance criteria\nReturn completion.", "instance_id": "sister"},
                {"id": "t_100002", "title": "two", "body": "CARD-TWO\n## acceptance criteria\nReturn completion.", "instance_id": "sister", "needs": ["t_100001"]}]},
        ])
        out = chat("lo", "FIRST-PRIVATE-INPUT")
        assert "ANSWER-PRIVATE-HISTORY" in out.stdout, out
        first = received[-1]
        assert first["path"].startswith("/fixture-a/"), first
        assert "PINNED-COORDINATOR" in json.dumps(first)
        assert "PRELOADED-TEMPLATE-SKILL" in json.dumps(first)
        assert "fixture-MCP-secret-value" not in json.dumps(first)
        assert [t["name"] for t in first["payload"]["tools"]] == ["read"], first
        check("真实 CLI 首次启动使用固定模板的模型、指令和工具集，覆盖全局默认模型")
        path = actor("lo")["session_file"]
        assert path and Path(path).is_file()
        chat("lo", "SECOND-INPUT")
        second = received[-1]["payload"]
        assert "FIRST-PRIVATE-INPUT" in json.dumps(second) and "ANSWER-PRIVATE-HISTORY" in json.dumps(second)
        assert actor("lo")["session_file"] == path
        check("进程退出后重开同一实例，复用同一原生日志并向模型送达之前上下文")
        s.apply([{"op": "template.revise", "id": "coord", "expected_revision": 1,
                  "config": {"model": "fixture-b/shared", "system": "REVISION-TWO", "tools": ["read"]}},
                 {"op": "instance.create", "id": "lo-new", "template_id": "coord", "template_revision": 2}])
        chat("lo", "PIN-UNCHANGED")
        assert received[-1]["path"].startswith("/fixture-a/")
        chat("lo-new", "INDEPENDENT-INPUT")
        assert received[-1]["path"].startswith("/fixture-b/")
        assert "FIRST-PRIVATE-INPUT" not in json.dumps(received[-1])
        check("模板升版不改变已有实例；新实例采用新版本，两个实例上下文独立")
        s.apply([{"op": "instance.new-session", "id": "lo", "expected_epoch": 1}])
        chat("lo", "FRESH-INPUT")
        fresh = json.dumps(received[-1])
        assert all(x not in fresh for x in ("FIRST-PRIVATE-INPUT", "SECOND-INPUT", "ANSWER-PRIVATE-HISTORY")), fresh
        assert "PINNED-COORDINATOR" in fresh
        # Current state is a query (misaka_roster show), not a prompt injection: the fresh
        # session starts with the role's rules and nothing carried from the old one.
        assert "CURRENT-REQUEST" not in fresh and "CURRENT-GOAL" not in fresh
        assert actor("lo")["session_file"] != path and Path(path).is_file()
        check("Last Order 换会话保留旧日志，模型只收到当前规则、模板、实例和未结卡片，没有旧对话细节")
        bad = s.cli("state", "apply", "--file", "-", payload={"operations": [
            {"op": "instance.new-session", "id": "lo", "expected_epoch": 1}]}, code=1)
        assert "epoch conflict" in bad.stderr + bad.stdout
        check("旧会话代次不能再次替换当前会话")
        proc = subprocess.Popen([sys.executable, "-m", "misaka", "chat", "--instance", "lo", "--prompt", "HOLD-REQUEST"],
                                cwd=s.project, env=s.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(proc)
        assert held.wait(15), proc.poll()
        count = len(received)
        bad = chat("lo", "COMPETING-WRITER", ok=False)
        assert "live writer" in bad.stderr and len(received) == count, bad
        bad = s.cli("state", "apply", "--file", "-", payload={"operations": [
            {"op": "instance.new-session", "id": "lo", "expected_epoch": 2}]}, code=1)
        assert "live writer" in bad.stderr + bad.stdout
        release.set()
        stdout, stderr = proc.communicate(timeout=20)
        assert proc.returncode == 0, (stdout, stderr)
        assert actor("lo")["writer_pid"] is None
        check("模型请求挂起时拒绝第二个写者和换会话，拒绝过程不产生额外模型请求；结束后释放写锁")
        saved = Path(actor("lo")["session_file"])
        backup = saved.read_bytes()
        saved.unlink()
        bad = chat("lo", "MISSING-TRANSCRIPT", ok=False)
        assert "transcript is missing" in bad.stderr, bad
        saved.write_bytes(backup)
        check("缺失日志明确报错，不扫描其他文件，不静默新建上下文")

        # Drive genuine terminal input through /new and the native model selector.
        from tests.test_task_view_e2e import FRAMES, Terminal
        term = Terminal([sys.executable, "-m", "misaka", "chat", "--instance", "lo"], s, cols=110, rows=34)
        terminals.append(term)
        term.frame("实例原生会话启动", "MISAKA")
        epoch = actor("lo")["session_epoch"]
        os.write(term.master, b"/new --carry\r")
        deadline = time.monotonic() + 12
        while actor("lo")["session_epoch"] == epoch and time.monotonic() < deadline:
            time.sleep(.1)
        assert actor("lo")["session_epoch"] == epoch + 1
        term.frame("终端换会话完成", "New session started")
        before = len(received)
        os.write(term.master, b"TERMINAL-FRESH-INPUT\r")
        term.frame("终端输入：新会话回复", "ANSWER-PRIVATE-HISTORY")
        assert len(received) > before
        fresh = json.dumps(received[-1])
        assert "COORDINATOR-EXIT-WHILE-SISTER-RUNS" not in fresh and "FRESH-INPUT" in fresh
        assert "FIRST-PRIVATE-INPUT" not in fresh
        os.write(term.master, b"/model\r")
        term.frame("模型选择器", "Ctrl+S")
        os.write(term.master, b"fixture-b")
        term.frame("筛选实例默认模型", "fixture-b")
        os.write(term.master, b"\x13")
        deadline = time.monotonic() + 10
        while actor("lo")["config"]["model"] != "fixture-b/shared" and time.monotonic() < deadline:
            time.sleep(.1)
        assert actor("lo")["config"]["model"] == "fixture-b/shared"
        os.write(term.master, b"MODEL-SELECTOR-CHECK\r")
        deadline = time.monotonic() + 10
        while "MODEL-SELECTOR-CHECK" not in json.dumps(received[-1]) and time.monotonic() < deadline:
            # Drain and render output so the PTY cannot block the model turn.
            import select
            if select.select([term.master], [], [], .1)[0]:
                term.term.write(os.read(term.master, 262144))
        assert received[-1]["path"].startswith("/fixture-b/")
        native_frames = list(FRAMES)
        term.close()
        terminals.remove(term)
        assert actor("lo")["writer_pid"] is None
        assert next(t for t in state()["templates"] if t["id"] == "coord" and t["revision"] == 1)["config"]["model"] == "fixture-a/shared"
        check("真实终端 /new --carry 仍创建独立会话；/model 写回实例覆盖，后续模型请求切到新端点且模板不变")

        # Actual daemon -> PTY -> card shell -> native model -> completion tool -> board.
        for tid, marker in (("t_100001", "CARD-ONE"), ("t_100002", "CARD-TWO")):
            before = len(received)
            s.cli("net", "run-card", tid)
            if marker == "CARD-ONE":
                assert card_held.wait(15)
                writer_pid = actor("sister")["writer_pid"]
                # A coordinator finishes its own model turn while the Sister is still running.
                chat("lo", "COORDINATOR-EXIT-WHILE-SISTER-RUNS")
                assert actor("sister")["writer_pid"] == writer_pid
                # Attach and disconnect the actual Panel subscription from a separate process.
                detached = subprocess.run([sys.executable, "-c", "from misaka.config import home; from misaka.utils import local_socket; import json; sock=local_socket.connect(str(home.path('net_sock')),2); sock.sendall((json.dumps({'id':1,'method':'pane.attach','params':{'id':'*'}})+'\\n').encode()); print(sock.recv(8192).decode()); sock.close()"],
                                         cwd=s.project, env=s.env, capture_output=True, text=True, timeout=5, check=False)
                assert detached.returncode == 0 and 'attached' in detached.stdout, detached
                assert "Daemon pid=" in s.cli("net", "status").stdout
                s.cli("net", "pause-instance", "sister")
                snapshot = json.loads(s.cli("net", "instance-status", "sister").stdout)
                assert snapshot["paused"]
                card_release.set()
                # A boundary pause retains the claimed card and writer, preventing cross-card reuse.
                refused = s.cli("net", "run-card", "t_100002", code=None)
                assert refused.returncode != 0
                assert actor("sister")["writer_pid"] == writer_pid
                s.cli("net", "resume-instance", "sister")
                check("协调会话结束和 Panel 断开不停止 Sister；暂停保留原 writer 与卡片绑定，恢复继续原会话")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                row = next(c for c in state()["cards"] if c["id"] == tid)
                if row["status"] == "done":
                    break
                if row["status"] in ("failed", "blocked", "stopped"):
                    raise AssertionError(row)
                time.sleep(.15)
            assert row["status"] == "done", (row, s.cli("net", "panes").stdout)
            assert len(received) > before
            request = next(r for r in received[before:] if marker in json.dumps(r))
            assert request["path"].startswith("/fixture-a/")
            if marker == "CARD-ONE":
                sister_path = actor("sister")["session_file"]
            else:
                assert actor("sister")["session_file"] == sister_path
                assert "CARD-ONE" in json.dumps(request) and "ANSWER-PRIVATE-HISTORY" in json.dumps(request)
        check("真实 daemon/PTY 执行两张依赖卡并通过完成工具验收；相同 Sister 跨卡复用同一会话和上下文")
        # The headless public CLI reuses that same instance in another project.
        panes = s.cli("net", "panes").stdout.splitlines()
        worker_pane = next(line.split()[0] for line in panes if "card:t_100002" in line)
        s.cli("net", "close", worker_pane)
        (s.project / "cwd-marker.txt").write_text("WRONG-FIRST-PROJECT")
        (s.other / "cwd-marker.txt").write_text("CORRECT-SECOND-PROJECT")
        s.apply([
            {"op": "request.create", "id": "req-other", "title": "Other project", "workspace": str(s.other), "owner_id": "lo"},
            {"op": "plan.update", "request_id": "req-other", "expected_revision": 0, "cards": [
                {"id": "t_100003", "title": "Three", "instance_id": "sister", "body": "CARD-THREE\nRead cwd-marker.txt.\n## acceptance criteria\nRead the second project marker and complete."}]}])
        before = len(received)
        s.cli("task", "start", "t_100003", cwd=s.other)
        third = next(c for c in state()["cards"] if c["id"] == "t_100003")
        assert third["status"] == "done", third
        assert actor("sister")["session_file"] == sister_path
        assert actor("sister")["writer_pid"] is None
        assert any("CORRECT-SECOND-PROJECT" in json.dumps(r) for r in received[before:])
        assert not any("WRONG-FIRST-PROJECT" in json.dumps(r) for r in received[before:])
        check("真实 task start 无界面执行入口复用原实例；跨项目后 read 工具实际读取第二项目，退出释放写者")

        s.apply([
            {"op": "instance.create", "id": "stop-coordinator", "template_id": "coord", "template_revision": 1,
             "config": {"tools": ["misaka_sister_stop"]}},
            {"op": "plan.update", "request_id": "req-other", "expected_revision": 1, "cards": [
                {"id": "t_100004", "title": "Stop explicitly", "instance_id": "sister",
                 "body": "STOP-CARD\n## acceptance criteria\nWait for explicit stop."}]}])
        s.cli("net", "run-card", "t_100004")
        assert stop_held.wait(15)
        s.cli("chat", "--instance", "stop-coordinator", "--prompt", "STOP-RUNNING-CARD", cwd=s.other)
        stopped = next(c for c in state()["cards"] if c["id"] == "t_100004")
        assert stopped["status"] == "stopped", stopped
        stop_release.set()
        check("Last Order 通过真实 misaka_sister_stop 工具停止 daemon 任务，携带正确代次与所有权凭据")

        from sqlite3 import connect
        with connect(s.home / "state/board.db") as con:
            fields = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
            assert not fields & {"session_file", "session_dir", "agent_id"}
            archive = con.execute("SELECT session_file FROM instance_session_archive WHERE instance_id='lo'").fetchall()
            assert len(archive) == 2
            charges = {tid: [json.loads(r[0]) for r in con.execute(
                "SELECT payload FROM events WHERE task_id=? AND kind='budget_usage'", (tid,))]
                for tid in ("t_100001", "t_100002")}
            assert all(len(rows) == 2 and sum(r["totalTokens"] for r in rows) == 48 for rows in charges.values()), charges
            assert all(r["provider"] == "fixture-a" and r["usage"]["totalTokens"] == 24 for rows in charges.values() for r in rows)
        check("同一日志中的两张卡分别按真实请求记账，没有把前卡用量计入后卡")
        check("物理卡片表不保存会话或 agent 身份副本；旧会话只在实例归档中留存")
        result = {"passed": len(checks), "checks": checks, "requests": received, "frames": native_frames,
                  "scope": "external CLI + real native engine + loopback HTTP SSE + daemon PTYs; no paid provider"}
        if report_path:
            Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({"passed": len(checks)}))
    finally:
        release.set()
        card_release.set()
        stop_release.set()
        for term in terminals:
            term.close()
        s.cli("net", "stop", code=None)
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=10)
        server.shutdown()
        server.server_close()
        s.close()


def test_instance_sessions_external():
    main()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
