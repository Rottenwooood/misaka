"""Acceptance through external CLI processes and committed database observations."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.work_fixture import BODY, A, B, C, D, E, Scenario


def main(report_path=None):
    s = Scenario()
    checks = []

    def check(label):
        checks.append(label)
        print("PASS " + label, flush=True)

    try:
        fresh = s.snapshot()
        assert fresh["state_status"] == "missing" and not s.home.exists()
        s.seed()
        data = s.snapshot()
        p = data["projects"][0]
        assert len(p["requests"]) == 2
        group = next(r for r in p["requests"] if r["id"] == "req-export")
        assert group["ranks"][A] < group["ranks"][B] < group["ranks"][D]
        assert all(
            not (s.project / "cards" / (tid + ".md")).exists() for tid in (A, B, C, D)
        )
        with sqlite3.connect(s.database) as con:
            assert "assignee" not in {
                r[1] for r in con.execute("PRAGMA table_info(tasks)")
            }
            assert (
                con.execute(
                    "SELECT assignee FROM card_state WHERE id=?", (A,)
                ).fetchone()[0]
                == "sister"
            )
        check(
            "公开 CLI 原子创建模板、实例、请求和前向引用 DAG；没有卡片文件真源或冗余执行者字段"
        )

        # Launch another process after all prior writers have exited.
        state = json.loads(s.cli("state", "show").stdout)
        assert len(state["instances"]) == 7 and len(state["requests"]) == 4
        s.apply(
            [
                {
                    "op": "template.revise",
                    "id": "sister",
                    "expected_revision": 1,
                    "config": {"model": "test/model-v2"},
                }
            ]
        )
        s.apply(
            [
                {
                    "op": "instance.create",
                    "id": "i-new",
                    "template_id": "sister",
                    "template_revision": 2,
                }
            ]
        )
        state = json.loads(s.cli("state", "show").stdout)
        by_id = {i["id"]: i for i in state["instances"]}
        assert by_id["i-1"]["config"]["model"] == "test/model-v1"
        assert by_id["i-new"]["config"]["model"] == "test/model-v2"
        assert by_id["i-1"]["template_revision"] == 1
        check("跨进程重启读取完整状态；模板修订不改变已存在实例的版本和配置")

        # A disconnected card remains in its real request. A temporary cycle within
        # the batch is harmless if the complete committed graph is acyclic.
        s.apply(
            [
                {
                    "op": "plan.update",
                    "request_id": "req-export",
                    "expected_revision": 1,
                    "cards": [{"id": A, "needs": [C]}, {"id": C, "needs": []}],
                }
            ]
        )
        p = s.snapshot()["projects"][0]
        assert len(p["requests"]) == 2
        assert next(n for n in p["nodes"] if n["id"] == C)["needs"] == []
        assert next(n for n in p["nodes"] if n["id"] == A)["needs"] == [C]
        check("整批调整 DAG 后一次校验；无依赖的节点仍属于原请求，不按连通分量拆组")

        before = s.cli("state", "show").stdout
        with sqlite3.connect(s.database) as con:
            count = con.execute("SELECT count(*) FROM card_state").fetchone()[0]
            events = con.execute("SELECT count(*) FROM events").fetchone()[0]
        invalid = [
            {
                "op": "template.create",
                "id": "must-rollback",
                "role": "sister",
                "config": {},
            },
            {
                "op": "plan.update",
                "request_id": "req-export",
                "expected_revision": 2,
                "cards": [
                    {
                        "id": "t_123456",
                        "title": "rollback",
                        "body": BODY,
                        "instance_id": "i-1",
                    },
                    {"id": C, "needs": [D]},
                ],
            },
        ]
        refused = s.apply(invalid, code=1)
        assert "cycle" in refused.stderr.lower()
        assert s.cli("state", "show").stdout == before
        with sqlite3.connect(s.database) as con:
            assert con.execute("SELECT count(*) FROM card_state").fetchone()[0] == count
            assert con.execute("SELECT count(*) FROM events").fetchone()[0] == events
        for parent in ("t_ffffff", E):
            s.apply(
                [
                    {
                        "op": "plan.update",
                        "request_id": "req-export",
                        "expected_revision": 2,
                        "cards": [{"id": C, "needs": [parent]}],
                    }
                ],
                code=1,
            )
        assert s.cli("state", "show").stdout == before
        check("环、缺失依赖及跨请求依赖被拒绝；整批实体、事件和计划版本均回滚")

        # Independent writers race the same expected revision.
        processes = []
        for title in ("并发版本甲", "并发版本乙"):
            payload = {
                "operations": [
                    {
                        "op": "plan.update",
                        "request_id": "req-export",
                        "expected_revision": 2,
                        "cards": [{"id": B, "title": title}],
                    }
                ]
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "misaka", "state", "apply", "--file", "-"],
                cwd=s.project,
                env=s.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            proc.stdin.write(json.dumps(payload, ensure_ascii=False))
            proc.stdin.close()
            processes.append(proc)
        outputs = []
        for proc in processes:
            proc.wait(timeout=15)
            outputs.append((proc.returncode, proc.stdout.read(), proc.stderr.read()))
        assert sorted(o[0] for o in outputs) == [0, 1], outputs
        assert any("revision conflict" in o[2] for o in outputs)
        assert (
            next(
                r
                for r in json.loads(s.cli("state", "show").stdout)["requests"]
                if r["id"] == "req-export"
            )["revision"]
            == 3
        )
        check("两个外部进程并发改同一计划：仅一个提交，另一个明确收到版本冲突")

        # Generated exports cannot rewrite title, state or edges.
        exported = Path(s.cli("task", "export", B).stdout.strip())
        assert exported.exists()
        stable = s.snapshot()
        exported.write_text("---\nid: corrupt\nstatus: done\nneeds: []\n---\n")
        assert s.snapshot() == stable
        exported.unlink()
        assert s.snapshot() == stable
        check("修改或删除 Markdown 导出不影响卡片、状态和依赖")

        # Admission is also backed by instance identity, not just a role name.
        s.apply(
            [
                {
                    "op": "plan.update",
                    "request_id": "req-independent",
                    "expected_revision": 1,
                    "cards": [
                        {
                            "id": "t_222222",
                            "title": "same instance",
                            "body": BODY,
                            "instance_id": "i-5",
                            "needs": [],
                        }
                    ],
                }
            ]
        )
        script = 'import sys; from misaka.core.platform import tasks; from misaka.config import CFG; c=tasks.connect(CFG["db"]); print(tasks.claim(c,sys.argv[1],"acceptance",generation=1))'
        claimers = [
            subprocess.Popen(
                [sys.executable, "-c", script, tid],
                cwd=s.project,
                env=s.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for tid in (E, "t_222222")
        ]
        verdicts = []
        for proc in claimers:
            out, err = proc.communicate(timeout=15)
            assert proc.returncode == 0, err
            verdicts.append(out.strip())
        assert sorted(verdicts) == ["False", "True"], verdicts
        with sqlite3.connect(s.database) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM card_state WHERE instance_id='i-5' AND status='running'"
                ).fetchone()[0]
                == 1
            )
        check("两个独立执行认领进程竞争同一实例：最多一张卡进入 running")
        active = (E, "t_222222")[verdicts.index("True")]
        queued = "t_222222" if active == E else E
        for status in ("blocked", "triage", "review"):
            s.runtime(active, status)  # External worker-state input, preserving started_at.
            denied = subprocess.run([sys.executable, "-c", script, queued], cwd=s.project,
                                    env=s.env, capture_output=True, text=True, check=True)
            assert denied.stdout.strip() == "False", (status, denied.stdout)
        s.runtime(active, "blocked")
        resume = "import sys,os; from misaka.core.platform import tasks; from misaka.config import CFG; c=tasks.connect(CFG['db']); assert tasks.claim_resume(c,sys.argv[1],'resume',os.getpid(),expected_generation=1); assert tasks.mark_stopped(c,sys.argv[1],generation=2); assert tasks.claim(c,sys.argv[2],'next',generation=1)"
        subprocess.run([sys.executable, "-c", resume, active, queued], cwd=s.project,
                       env=s.env, check=True, capture_output=True, text=True)
        check("已开工卡片等待输入、分诊或复核时保留实例名额；原卡恢复后明确停止才允许后卡开工")


        old_generation = next(
            c
            for c in json.loads(s.cli("state", "show").stdout)["cards"]
            if c["id"] == C
        )["generation"]
        s.apply(
            [
                {
                    "op": "plan.update",
                    "request_id": "req-export",
                    "expected_revision": 3,
                    "cards": [{"id": C, "title": "新合同"}],
                }
            ]
        )
        stale_script = 'import sys; from misaka.core.platform import tasks; from misaka.config import CFG; c=tasks.connect(CFG["db"]); print(tasks.claim(c,sys.argv[1],"stale",generation=int(sys.argv[2])))'
        stale = subprocess.run(
            [sys.executable, "-c", stale_script, C, str(old_generation)],
            cwd=s.project,
            env=s.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert stale.returncode == 0 and stale.stdout.strip() == "False"
        with sqlite3.connect(s.database) as con:
            previous = json.loads(
                con.execute(
                    "SELECT payload FROM events WHERE task_id=? AND kind='contract_updated' ORDER BY id DESC LIMIT 1",
                    (C,),
                ).fetchone()[0]
            )
            assert previous["previous_generation"] == old_generation
            assert previous["previous_contract"]["instance_id"] == "i-3"
            assert previous["previous_contract"]["body"] == BODY.strip()
        check("修改合同推进卡片代次；持有旧内容的外部执行器不能认领新计划")
        s.apply(
            [
                {
                    "op": "plan.update",
                    "request_id": "req-export",
                    "expected_revision": 4,
                    "cards": [{"id": C, "cancel": True}],
                }
            ]
        )
        node = next(n for n in s.snapshot()["projects"][0]["nodes"] if n["id"] == C)
        assert node["status"] == "stopped"
        assert s.cli("task", C, "--delete", code=1).returncode == 1
        check("取消保留节点与依赖；被引用的卡片拒绝删除")

        # Corrupt format rejection happens before any DDL or WAL mode change.
        incompatible = s.base / "incompatible" / "state" / "board.db"
        incompatible.parent.mkdir(parents=True)
        with sqlite3.connect(incompatible) as con:
            con.executescript(
                "CREATE TABLE tasks(id TEXT); CREATE TABLE schema_migrations(component TEXT,version INT,applied_at INT); INSERT INTO schema_migrations VALUES('tasks',9,0);"
            )
        before_hash = hashlib.sha256(incompatible.read_bytes()).hexdigest()
        env = {**s.env, "MISAKA_HOME": str(incompatible.parent.parent)}
        proc = subprocess.run(
            [sys.executable, "-m", "misaka", "state", "apply", "--file", "-"],
            cwd=s.project,
            env=env,
            input=json.dumps(
                {
                    "operations": [
                        {
                            "op": "template.create",
                            "id": "x",
                            "role": "sister",
                            "config": {},
                        }
                    ]
                }
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert proc.returncode == 1
        assert hashlib.sha256(incompatible.read_bytes()).hexdigest() == before_hash
        with sqlite3.connect(incompatible) as con:
            assert (
                con.execute(
                    "SELECT name FROM sqlite_master WHERE name='work_requests'"
                ).fetchone()
                is None
            )
        check("不兼容数据库明确拒绝，未改写旧格式；没有迁移或回退路径")
        if os.name == "posix":
            import socket
            import threading

            sock_path = s.home / "run" / "net.sock"
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(sock_path))
            listener.listen(4)
            listener.settimeout(10)
            received = []

            def old_peer():
                try:
                    for _ in range(2):
                        client, _address = listener.accept()
                        with client:
                            client.settimeout(5)
                            raw = b""
                            while not raw.endswith(b"\n"):
                                raw += client.recv(65536)
                            message = json.loads(raw)
                            received.append(message["method"])
                            client.sendall(
                                (
                                    json.dumps(
                                        {
                                            "id": message["id"],
                                            "result": {"proto": 54, "pid": 999},
                                        }
                                    )
                                    + "\n"
                                ).encode()
                            )
                finally:
                    listener.close()

            thread = threading.Thread(target=old_peer, daemon=True)
            thread.start()
            try:
                for args in [("view", "tasks", "--pane"), ("net", "status")]:
                    rejected = s.cli(*args, code=1)
                    assert "protocol mismatch" in rejected.stderr
                thread.join(timeout=12)
                assert not thread.is_alive() and received == ["ping", "ping"], received
            finally:
                listener.close()
                sock_path.unlink(missing_ok=True)
            check("真实外部协议端返回旧版本时明确拒绝；不发停止、开窗或替换请求")
        result = {
            "passed": len(checks),
            "checks": checks,
            "scope": "external CLI, subprocess concurrency, SQLite and generated-file input; no models",
        }
        if report_path:
            Path(report_path).write_text(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
        print(json.dumps({"passed": len(checks)}, ensure_ascii=False))
    finally:
        s.close()


def test_work_state_external():
    main()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
