"""Read-only views of formal work requests. SQLite is the only card/plan authority."""

from __future__ import annotations

import copy
import sqlite3
from contextlib import closing
from pathlib import Path

from misaka.core.platform import tasks, work


class SnapshotReader:
    def __init__(self, projects, database):
        self.projects = sorted({str(Path(p).expanduser().resolve()) for p in projects})
        self.database = Path(database).expanduser().resolve()
        self.previous = None

    def read(self):
        result = {
            "schema": 2,
            "state_status": "ok",
            "state_error": None,
            "projects": [
                {
                    "path": p,
                    "name": Path(p).name,
                    "nodes": [],
                    "requests": [],
                    "errors": [],
                }
                for p in self.projects
            ],
        }
        if not self.database.exists():
            result["state_status"] = "missing"
            result["state_error"] = "尚未建立任务状态库"
            return result
        try:
            with closing(
                sqlite3.connect(
                    self.database.as_uri() + "?mode=ro", uri=True, timeout=0.1
                )
            ) as con:
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA query_only=ON")
                con.execute(
                    "BEGIN"
                )  # requests, cards, instances and edges are one committed snapshot
                recorded = con.execute(
                    "SELECT MAX(version) FROM schema_migrations WHERE component='tasks'"
                ).fetchone()[0]
                if recorded != tasks.TASK_SCHEMA_VERSION:
                    raise ValueError(
                        f"任务状态格式不匹配：{recorded}，需要 {tasks.TASK_SCHEMA_VERSION}"
                    )
                for project in result["projects"]:
                    requests = con.execute(
                        "SELECT * FROM work_requests WHERE workspace=? ORDER BY created_at,id",
                        (project["path"],),
                    ).fetchall()
                    for request in requests:
                        group = dict(request)
                        rows = con.execute(
                            "SELECT * FROM card_state WHERE request_id=? ORDER BY created_at,id",
                            (request["id"],),
                        ).fetchall()
                        group["nodes"] = [r["id"] for r in rows]
                        group["errors"] = []
                        try:
                            group["ranks"] = work.validate_graph(con, request["id"])
                        except ValueError as error:
                            group["errors"] = [str(error)]
                            group["ranks"] = {r["id"]: i for i, r in enumerate(rows)}
                        for row in rows:
                            parents = tasks.parent_ids(con, row["id"])
                            project["nodes"].append(
                                {
                                    "id": row["id"],
                                    "request_id": row["request_id"],
                                    "instance_id": row["instance_id"],
                                    "title": row["title"],
                                    "status": row["status"],
                                    "running": row["status"] == "running",
                                    "needs": parents,
                                    "stale": False,
                                    "errors": [],
                                    "block_reason": row["block_reason"],
                                    "session_file": row["session_file"],
                                    "fs_scope": row["fs_scope"],
                                    "forbidden_commands": row["forbidden_commands"],
                                    "subscriptions": row["subscriptions"],
                                }
                            )
                        project["requests"].append(group)
                con.rollback()
        except (sqlite3.Error, OSError, ValueError) as error:
            if self.previous is not None:
                result = copy.deepcopy(self.previous)
                for project in result["projects"]:
                    for node in project["nodes"]:
                        node.update(
                            stale=True,
                            running=None,
                            errors=["状态库暂不可用；显示旧快照"],
                        )
            result["state_status"] = "error"
            result["state_error"] = f"任务状态读取失败：{error}"
            return result
        self.previous = copy.deepcopy(result)
        return result
