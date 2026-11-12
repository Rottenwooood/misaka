"""Drive plan and dispatch through real CLI, model requests, daemon and card PTYs."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.work_fixture import Scenario

A, B, C = "t_a12345", "t_b12345", "t_c12345"
OLD = {"misaka_board", "misaka_state", "misaka_card", "misaka_card_link", "misaka_dispatch", "misaka_sister"}


def main(report_path=None):
    s = Scenario()
    requests = []
    checks = []
    worker_ready = threading.Event()

    def task(**command):
        return "misaka_task", {"command": command}

    def roster(**command):
        return "misaka_roster", {"command": command}

    def target(card, generation=1):
        return {"id": card, "generation": generation}

    def plan(rid, directory, cards):
        return task(action="save", request_id=rid, workspace=str(directory), title=rid, cards=cards)

    def card(cid, marker, instance, needs=()):
        return {"id": cid, "title": marker, "body": marker + "\n## acceptance criteria\nSubmit a result.",
                "instance": instance, "needs": list(needs)}

    routes = {
        "PLAN-ONLY": [plan("primary", s.project, [card(B, "WORKER-B", "worker-a", [A]),
                                                card(A, "WORKER-A", "worker-a")]), "PLAN-VISIBLE"],
        "START-CHILD-EARLY": [task(action="start", cards=[target(B)]), "REJECTED-EARLY"],
        "REVISE-PLAN": [task(action="save", request_id="primary", expected_revision=1,
                             cards=[{"id": A, "body": "WORKER-A-REVISED\n## acceptance criteria\nSubmit the revised result."}]),
                        "REVISED"],
        "START-STALE": [task(action="start", cards=[target(A)]), "REJECTED-STALE"],
        "USER-GO": [task(action="start", cards=[target(A, 2)]), "DISPATCHED-A"],
        "START-DEPENDENT": [task(action="start", cards=[target(B)]), "DISPATCHED-B"],
        "DIRECT": [plan("second", s.other, [card(C, "WORKER-C", "worker-b")]),
                   task(action="start", cards=[target(C)]), "DISPATCHED-C"],
        "SHOW-BOTH": [task(action="show"), "PROJECTS-VISIBLE"],
        "FOREIGN-READ": [task(action="show", request_id="primary"), "FOREIGN-REJECTED"],
        "FOREIGN-EDIT": [task(action="save", request_id="primary", expected_revision=2, title="illicit"), "EDIT-REJECTED"],
        "FOREIGN-START": [task(action="start", cards=[target(A, 2)]), "START-REJECTED"],
        "ROSTER-CHANGE": [
            roster(action="create_template", id="spare", role="sister", config={"system": "SPARE-V1"}),
            roster(action="create_instance", id="spare-1", template_id="spare", template_revision=1),
            roster(action="revise_template", id="spare", expected_revision=1, config={"system": "SPARE-V2"}),
            roster(action="show"), "ROSTER-DONE",
        ],
    }

    def respond(payload):
        messages = payload["messages"]
        last_user = next(i for i in reversed(range(len(messages))) if any(
            block.get("type") == "text" for block in messages[i]["content"]) and messages[i]["role"] == "user")
        prompt = json.dumps(messages[last_user]["content"])
        tail = messages[last_user+1:]
        if "WORKER-" in prompt:
            if not tail:
                if "WORKER-A" in prompt:
                    worker_ready.set()
                if "WORKER-C" in prompt:
                    return "read", {"path": "project-marker.txt"}
                return "misaka_card_complete", {"summary": "Worker submitted the requested result"}
            if any(b.get("name") == "read" for b in tail[-2]["content"]):
                return "misaka_card_complete", {"summary": "Worker read the requested file"}
            return "WORKER-DONE"
        for marker, chain in routes.items():
            if marker in prompt:
                steps = sum(1 for m in tail for b in m["content"] if b.get("type") == "tool_use")
                return chain[min(steps, len(chain)-1)]
        return "UNEXPECTED-PROMPT"

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            answer = respond(payload)
            if isinstance(answer, tuple):
                content = {"type": "tool_use", "name": answer[0], "input": answer[1], "id": f"call-{len(requests)}"}
            else:
                content = {"type": "text", "text": answer}
            frames = [
                {"type": "message_start", "message": {"id": f"msg-{len(requests)}", "type": "message",
                 "role": "assistant", "model": payload["model"], "content": [], "stop_reason": None,
                 "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": content},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use" if isinstance(answer, tuple) else "end_turn",
                 "stop_sequence": None}, "usage": {"output_tokens": 3}},
                {"type": "message_stop"},
            ]
            body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in frames).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def check(label):
        checks.append(label)
        print("PASS " + label, flush=True)

    def chat(text, who="lo"):
        return s.cli("chat", "--instance", who, "--prompt", text)

    def state():
        return json.loads(s.cli("state", "show").stdout)

    def cards():
        return {c["id"]: c for c in state()["cards"]}

    def until(cid, status):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = cards()[cid]
            if row["status"] == status:
                return row
            time.sleep(.1)
        raise AssertionError((cid, status, cards()[cid]))

    def wire(start=0):
        return json.dumps(requests[start:], ensure_ascii=False)

    try:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            s.env.pop(key, None)
        s.home.mkdir(parents=True, exist_ok=True)
        (s.home / "models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": f"http://127.0.0.1:{server.server_port}", "api": "anthropic-messages",
            "apiKey": "fixture-key", "models": [{"id": "local", "name": "Local fixture", "reasoning": False,
             "input": ["text"], "contextWindow": 200000, "maxTokens": 4096,
             "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}],
        }}}))
        (s.home / "settings.json").write_text(json.dumps({"defaultProvider": "fixture", "defaultModel": "local",
            "defaultProjectTrust": "always", "retry": {"enabled": False}, "compaction": {"enabled": False}}))
        (s.project / "project-marker.txt").write_text("WRONG-PROJECT-MARKER")
        (s.other / "project-marker.txt").write_text("CORRECT-PROJECT-MARKER")
        s.apply([
            {"op": "template.create", "id": "coord", "role": "last-order",
             "config": {"tools": ["misaka_task", "misaka_roster", *sorted(OLD)]}},
            {"op": "template.create", "id": "worker", "role": "sister",
             "config": {"tools": ["read", "misaka_card_complete", "misaka_task", "misaka_roster"]}},
            *({"op": "instance.create", "id": iid, "template_id": template, "template_revision": 1}
              for iid, template in (("lo", "coord"), ("other-lo", "coord"), ("worker-a", "worker"), ("worker-b", "worker"))),
        ])
        out = chat("PLAN-ONLY: Show me the plan and wait for my next reply before starting any card.")
        assert "PLAN-VISIBLE" in out.stdout, out
        names = {t["name"] for t in requests[0]["tools"]}
        assert {"misaka_task", "misaka_roster"} <= names and not names & OLD, names
        system = json.dumps(requests[0]["system"])
        assert all(phrase not in system.lower() for phrase in ("wait for the user's go-ahead", "start approved", "explicit user approval"))
        statuses = {k: v["status"] for k, v in cards().items()}
        errors = [b for p in requests for m in p["messages"] for b in m["content"] if b.get("type") == "tool_result"]
        assert statuses == {A: "ready", B: "todo"}, (statuses, errors)
        assert not worker_ready.is_set() and all("misaka_card_complete" not in {t["name"] for t in p["tools"]} for p in requests)
        check("先看计划：保存后只返回计划，未启动任何 Sister；旧工具和固定批准指令未进入模型请求")

        for prompt, error in (("START-CHILD-EARLY", "not ready"), ("START-STALE", "generation changed")):
            if prompt == "START-STALE":
                assert "REVISED" in chat("REVISE-PLAN").stdout
                assert cards()[A]["generation"] == 2
            offset = len(requests)
            chat(prompt)
            assert error in wire(offset), wire(offset)[-4000:]
            assert not worker_ready.is_set()
        check("依赖未满足和过期卡片代次均拒绝派工；已保存的改动经过新 CLI 进程仍生效")

        for prompt in ("FOREIGN-READ", "FOREIGN-EDIT", "FOREIGN-START"):
            offset = len(requests)
            chat(prompt, "other-lo")
            assert "not owned by this Last Order" in wire(offset)
        assert cards()[A]["status"] == "ready"
        check("另一 LO 无法读取、改写或启动不属于它的任务")

        offset = len(requests)
        assert "DISPATCHED-A" in chat("USER-GO: Go ahead with the revised plan.").stdout
        assert worker_ready.wait(8)
        until(A, "done")
        assert "WORKER-A-REVISED" in wire(offset)
        assert cards()[B]["status"] == "ready"
        assert "DISPATCHED-B" in chat("START-DEPENDENT").stdout
        until(B, "done")
        check("另一次用户输入后显式派工成功；依赖解除只使下一卡就绪，仍需单独启动")

        offset = len(requests)
        assert "DISPATCHED-C" in chat("DIRECT: Plan and start immediately within this request.").stdout
        until(C, "done")
        assert "CORRECT-PROJECT-MARKER" in wire(offset) and "WRONG-PROJECT-MARKER" not in wire(offset)
        actual = [p for p in requests if "misaka_card_complete" in {t["name"] for t in p["tools"]}]
        assert actual and all(not {t["name"] for t in p["tools"]} & {"misaka_task", "misaka_roster"} for p in actual)
        offset = len(requests)
        chat("SHOW-BOTH")
        assert "primary" in wire(offset) and "second" in wire(offset)
        check("直接派工：同一轮保存并显式派工，Sister 在第二个项目中执行，且拿不到编排工具")

        offset = len(requests)
        assert "ROSTER-DONE" in chat("ROSTER-CHANGE").stdout
        inventory = state()
        assert next(i for i in inventory["instances"] if i["id"] == "spare-1")["config"]["system"] == "SPARE-V1"
        assert sorted(t["revision"] for t in inventory["templates"] if t["id"] == "spare") == [1, 2]
        assert "SPARE-V2" in wire(offset)
        check("模型通过独立 roster 工具新建模板和实例、修订模板；旧实例保持固定版本")

        evidence = {"passed": len(checks), "checks": checks, "model_requests": len(requests),
                    "scope": "CLI + scripted loopback SSE model + real daemon/card PTYs; no paid model calls"}
        if report_path:
            Path(report_path).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(evidence, ensure_ascii=False))
    finally:
        s.cli("net", "stop", code=None)
        server.shutdown()
        server.server_close()
        s.close()


def test_coordination_external():
    main()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
