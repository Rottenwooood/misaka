"""External CLI fixtures shared by state and terminal acceptance tests."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BODY = "## goal\nProduce an output.\n## boundaries\nTemporary project only.\n## acceptance criteria\n- The output can be checked.\n"
A, B, C, D, E, F = (
    "t_a00001",
    "t_b00001",
    "t_c00001",
    "t_d00001",
    "t_e00001",
    "t_f00001",
)


class Scenario:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mv-")
        self.base = Path(self.temp.name)
        self.project, self.other, self.empty = (
            self.base / n for n in ("P1", "P2", "empty")
        )
        for p in (self.project, self.other, self.empty):
            p.mkdir()
        self.home = self.base / "home"
        self.database = self.home / "state" / "board.db"
        self.env = {
            **os.environ,
            "MISAKA_HOME": str(self.home),
            "PYTHONPATH": str(ROOT),
            "MISAKA_ALLOW_NESTED": "1",
        }
        for key in list(self.env):
            if key.endswith(("_API_KEY", "_AUTH_TOKEN")):
                self.env.pop(key)

    def close(self):
        self.temp.cleanup()

    def cli(self, *args, code=0, cwd=None, payload=None):
        kwargs = (
            {"stdin": subprocess.DEVNULL}
            if payload is None
            else {"input": json.dumps(payload, ensure_ascii=False)}
        )
        out = subprocess.run(
            [sys.executable, "-m", "misaka", *args],
            cwd=cwd or self.project,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
            **kwargs,
        )
        if code is not None:
            assert out.returncode == code, (
                args,
                out.returncode,
                out.stdout,
                out.stderr,
            )
        return out

    def apply(self, operations, code=0):
        out = self.cli(
            "state",
            "apply",
            "--file",
            "-",
            payload={"operations": operations},
            code=code,
        )
        return json.loads(out.stdout) if code == 0 else out

    def snapshot(self, *args, code=0):
        return json.loads(self.cli("view", "tasks", "--json", *args, code=code).stdout)

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.database)) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute(sql, params)
            con.commit()

    def runtime(self, tid, status):
        self.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))

    def seed(self):
        operations = [
            {
                "op": "template.create",
                "id": "last-order",
                "role": "last-order",
                "config": {"system": "coordinate"},
            },
            {
                "op": "template.create",
                "id": "sister",
                "role": "sister",
                "config": {"model": "test/model-v1", "tools": ["read"]},
            },
            {
                "op": "instance.create",
                "id": "lo-1",
                "template_id": "last-order",
                "template_revision": 1,
            },
        ]
        operations += [
            {
                "op": "instance.create",
                "id": f"i-{n}",
                "template_id": "sister",
                "template_revision": 1,
            }
            for n in range(1, 7)
        ]
        operations += [
            {
                "op": "request.create",
                "id": rid,
                "title": title,
                "owner_id": "lo-1",
                "workspace": str(path),
            }
            for rid, title, path in [
                ("req-export", "导出功能", self.project),
                ("req-independent", "独立交付", self.project),
                ("req-wait", "外部资料", self.other),
                ("req-draft", "未拆分需求", self.empty),
            ]
        ]

        def card(tid, title, instance, needs=()):
            return {
                "id": tid,
                "title": title,
                "body": BODY,
                "instance_id": instance,
                "needs": list(needs),
            }

        operations += [
            {
                "op": "plan.update",
                "request_id": "req-export",
                "expected_revision": 0,
                "cards": [
                    card(D, "集成验收", "i-4", [B, C]),
                    card(B, "实现中文导出", "i-2", [A]),
                    card(A, "调查现有接口", "i-1"),
                    card(C, "编写使用说明", "i-3", [A]),
                ],
            },
            {
                "op": "plan.update",
                "request_id": "req-independent",
                "expected_revision": 0,
                "cards": [card(E, "独立交付", "i-5")],
            },
            {
                "op": "plan.update",
                "request_id": "req-wait",
                "expected_revision": 0,
                "cards": [card(F, "等待外部数据", "i-6")],
            },
        ]
        self.apply(operations)

    def demo_states(self):
        for tid, status in [
            (A, "done"),
            (B, "running"),
            (C, "ready"),
            (D, "todo"),
            (E, "done"),
            (F, "blocked"),
        ]:
            self.runtime(tid, status)
