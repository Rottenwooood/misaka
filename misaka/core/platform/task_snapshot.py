"""Read-only projections of existing cards. Never reconcile, migrate, claim or dispatch.

Card files own contracts/dependencies/at-rest state; SQLite owns runtime state.
Connected dependency components are a compatibility grouping, not work-request IDs.
A projection is observational: it does not certify process liveness or dispatchability.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from misaka.utils.frontmatter import parse_frontmatter

STATUSES = {
    "ready",
    "todo",
    "review",
    "done",
    "failed",
    "stopped",
    "blocked",
    "triage",
    "archived",
    "held",
}


class SnapshotReader:
    def __init__(self, projects, database):
        self.projects = sorted({str(Path(p).expanduser().resolve()) for p in projects})
        self.database = Path(database).expanduser().resolve()
        self.previous = {}

    def _runtime(self):
        if not self.database.exists():
            return {}, "missing", "运行索引尚不存在；执行状态未知"
        try:
            # A URI prevents sqlite3 from silently creating a missing database. Do not use
            # immutable=1: the writer may have uncheckpointed WAL transactions.
            with closing(
                sqlite3.connect(
                    self.database.as_uri() + "?mode=ro", uri=True, timeout=0.1
                )
            ) as con:
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA query_only=ON")
                rows = [
                    dict(r)
                    for r in con.execute(
                        "SELECT id, workspace, generation, status, block_reason, session_file FROM tasks"
                    )
                ]
            return (
                {
                    (str(Path(r["workspace"]).resolve()), r["id"]): r
                    for r in rows
                    if r.get("workspace")
                },
                "ok",
                None,
            )
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
            return {}, "error", f"运行索引读取失败：{error}"

    def read(self):
        runtime, runtime_state, runtime_error = self._runtime()
        projects = []
        for project in self.projects:
            root = Path(project)
            errors = []
            files = {}
            cards = root / "cards"
            try:
                if not root.is_dir():
                    raise OSError("项目目录不存在或不可读取")
                if cards.exists():
                    files = {
                        p.stem: p
                        for p in sorted(cards.iterdir())
                        if p.name.startswith("t_") and p.suffix == ".md"
                    }
            except OSError as error:
                errors.append(str(error))
            indexed = {
                tid: row
                for (workspace, tid), row in runtime.items()
                if workspace == project
            }
            cached = {
                tid: f
                for (workspace, tid), f in self.previous.items()
                if workspace == project
            }
            ids = set(files) | set(indexed) | (set(cached) if errors else set())
            if not errors:
                for tid in set(cached) - ids:
                    self.previous.pop((project, tid), None)
            nodes = []
            for tid in sorted(ids):
                row = indexed.get(tid)
                issues = []
                stale = False
                try:
                    path = files.get(tid, cards / (tid + ".md"))
                    fields = parse_frontmatter(
                        path.read_text(encoding="utf-8-sig")
                    ).frontmatter
                    if fields.get("id") != tid:
                        raise ValueError("卡片 id 与文件名不符或缺少 frontmatter")
                    if (
                        not isinstance(fields.get("status"), str)
                        or fields["status"] not in STATUSES
                    ):
                        raise ValueError("未知的持久状态")
                    for key in ("title", "assignee"):
                        if not isinstance(fields.get(key), str):
                            raise TypeError(f"{key} 必须为文本")
                    generation = fields.get("generation")
                    if type(generation) is not int or generation < 1:
                        raise ValueError("generation 必须为正整数")
                    needs = fields.get("needs")
                    if needs is None:
                        needs = []
                    if not isinstance(needs, list) or not all(
                        isinstance(n, str) for n in needs
                    ):
                        raise ValueError("needs 必须为卡片 id 列表")
                    fields = {**fields, "needs": list(dict.fromkeys(needs))}
                    self.previous[(project, tid)] = fields
                except (OSError, ValueError, TypeError) as error:
                    issues.append(f"卡片读取失败：{error}")
                    fields = cached.get(tid, {})
                    stale = bool(fields)
                compatible = (
                    not issues
                    and row is not None
                    and row.get("generation") == fields.get("generation")
                )
                if row is not None and not compatible and not issues:
                    issues.append("文件与运行索引代次不同；执行状态未知")
                running = row.get("status") == "running" if compatible else None
                nodes.append(
                    {
                        "id": tid,
                        "title": fields.get("title", tid),
                        "assignee": fields.get("assignee", "?"),
                        "status": fields.get("status", "unknown"),
                        "running": running,
                        "runtime_status": row.get("status") if compatible else None,
                        "block_reason": row.get("block_reason") if compatible else None,
                        "session_file": row.get("session_file") if compatible else None,
                        "needs": fields.get("needs", []),
                        "stale": stale,
                        "errors": issues,
                    }
                )
            projects.append(
                {
                    "path": project,
                    "name": root.name,
                    "nodes": nodes,
                    "groups": _groups(nodes),
                    "errors": errors,
                }
            )
        return {
            "schema": 1,
            "grouping": "legacy_dependency_components",
            "runtime_state": runtime_state,
            "runtime_error": runtime_error,
            "projects": projects,
        }


def _groups(nodes):
    """Stable connected groups and topological ranks; surface invalid graphs explicitly."""
    by_id = {n["id"]: n for n in nodes}
    adjacency = {tid: set() for tid in by_id}
    for node in nodes:
        node["missing_needs"] = [n for n in node["needs"] if n not in by_id]
        for parent in node["needs"]:
            if parent in by_id:
                adjacency[parent].add(node["id"])
                adjacency[node["id"]].add(parent)
    groups, unseen = [], set(by_id)
    while unseen:
        seed = min(unseen)
        members, pending = set(), [seed]
        while pending:
            tid = pending.pop()
            if tid in members:
                continue
            members.add(tid)
            pending.extend(adjacency[tid] - members)
        unseen -= members
        ranks = {}
        remaining = set(members)
        while remaining:
            ready = sorted(
                tid
                for tid in remaining
                if all(p not in members or p in ranks for p in by_id[tid]["needs"])
            )
            if not ready:
                break
            for tid in ready:
                ranks[tid] = max(
                    (ranks[p] + 1 for p in by_id[tid]["needs"] if p in ranks), default=0
                )
            remaining -= set(ready)
        unresolved = sorted(remaining)
        # Unresolved nodes include cycles and their downstream. Never call all of these
        # cyclic: a downstream card may itself be acyclic.
        for tid in unresolved:
            ranks[tid] = max(ranks.values(), default=-1) + 1
        groups.append(
            {
                "id": seed,
                "nodes": sorted(members),
                "ranks": ranks,
                "cycle_or_downstream": unresolved,
            }
        )
    return groups
