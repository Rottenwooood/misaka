"""Work requests, immutable templates and durable instance identities in the board DB.

No model calls and no process lifecycle here. Every plan edit is an atomic command
with an expected revision; card and dependency readers use the same committed data.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import nullcontext
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
 id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision > 0),
 role TEXT NOT NULL CHECK(role IN ('last-order','sister')),
 config_json TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(id,revision)
);
CREATE TABLE IF NOT EXISTS instances (
 id TEXT PRIMARY KEY,
 template_id TEXT NOT NULL,
 template_revision INTEGER NOT NULL,
 overrides_json TEXT NOT NULL DEFAULT '{}',
 label TEXT NOT NULL,
 retired INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0,1)),
 created_at INTEGER NOT NULL,
 session_epoch INTEGER NOT NULL DEFAULT 1,
 session_file TEXT,
 agent_id TEXT,
 writer_pid INTEGER,
 writer_identity TEXT,
 writer_token TEXT,
 FOREIGN KEY(template_id,template_revision) REFERENCES templates(id,revision)
);
CREATE TABLE IF NOT EXISTS instance_session_archive (
 instance_id TEXT NOT NULL REFERENCES instances(id),
 epoch INTEGER NOT NULL,
 session_file TEXT,
 archived_at INTEGER NOT NULL,
 PRIMARY KEY(instance_id,epoch)
);
CREATE TABLE IF NOT EXISTS work_requests (
 id TEXT PRIMARY KEY,
 title TEXT NOT NULL,
 body TEXT NOT NULL DEFAULT '',
 workspace TEXT NOT NULL,
 owner_id TEXT NOT NULL REFERENCES instances(id),
 revision INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL
);
"""
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid identity: {value!r}")
    return value


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value.strip()


def _json(value):
    if not isinstance(value, dict):
        raise TypeError("Configuration must be an object")
    from misaka.core.platform.instances import validate_config
    validate_config(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def instance(con, instance_id):
    row = con.execute(
        "SELECT i.*,t.role,t.config_json FROM instances i JOIN templates t "
        "ON t.id=i.template_id AND t.revision=i.template_revision WHERE i.id=?",
        (instance_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Instance not found: {instance_id}")
    data = dict(row)
    data["config"] = {
        **json.loads(data.pop("config_json")),
        **json.loads(data.pop("overrides_json")),
    }
    if data["session_file"]:
        from misaka.config import home
        data["session_file"] = str(home.from_stored(data["session_file"]))
    return data


def request(con, request_id):
    row = con.execute(
        "SELECT * FROM work_requests WHERE id=?", (request_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Work request not found: {request_id}")
    return dict(row)


def coordinator(con, owner_id=None):
    if owner_id is None:
        candidates = [
            r[0]
            for r in con.execute(
                "SELECT i.id FROM instances i JOIN templates t ON t.id=i.template_id AND t.revision=i.template_revision "
                "WHERE t.role='last-order' AND i.retired=0 ORDER BY i.id"
            )
        ]
        if len(candidates) != 1:
            raise ValueError(
                "Specify the Last Order owner instance; use misaka state show/apply"
            )
        owner_id = candidates[0]
    owner = instance(con, owner_id)
    if owner["role"] != "last-order" or owner["retired"]:
        raise ValueError("Owner must be a non-retired Last Order instance")
    return owner


def validate_graph(con, request_id):
    rows = con.execute(
        "SELECT id FROM card_state WHERE request_id=?", (request_id,)
    ).fetchall()
    graph = {r["id"]: [] for r in rows}
    for row in con.execute(
        "SELECT e.child_id,e.parent_id FROM task_dependencies e "
        "JOIN card_state c ON c.id=e.child_id WHERE c.request_id=?",
        (request_id,),
    ):
        if row["parent_id"] not in graph:
            raise ValueError("A dependency must stay inside one work request")
        graph[row["child_id"]].append(row["parent_id"])
    try:
        ordered = tuple(TopologicalSorter(graph).static_order())
    except CycleError as error:
        raise ValueError("Task dependency creates a cycle") from error
    ranks = {}
    for tid in ordered:
        ranks[tid] = max((ranks[p] + 1 for p in graph[tid]), default=0)
    return ranks


def _revision(con, request_id, expected):
    current = request(con, request_id)
    if type(expected) is not int or current["revision"] != expected:
        raise ValueError(
            f"Plan revision conflict: expected {expected}, current {current['revision']}"
        )
    return current


def update_plan(con, request_id, expected_revision, changes):
    """Apply all changed contracts and edges before validating/publishing the graph.

    Running/completed contracts need a lifecycle action or a new follow-up card;
    this persistence command never silently stops a process or rewrites a result.
    """
    from misaka.core.platform import tasks

    with tasks._write_txn(con):
        current = _revision(con, request_id, expected_revision)
        if not isinstance(changes, list) or not changes:
            raise ValueError("Plan changes must be a nonempty card list")
        seen = set()
        for change in changes:
            if not isinstance(change, dict):
                raise TypeError("Each card change must be an object")
            tid = identifier(change.get("id"))
            if not re.fullmatch(r"t_[0-9a-f]{6}", tid):
                raise ValueError("Card IDs use t_ followed by six hex digits")
            if tid in seen:
                raise ValueError(f"Repeated card change: {tid}")
            seen.add(tid)
            row = tasks.get(con, tid)
            if row is not None and row["request_id"] != request_id:
                raise ValueError("A card cannot change its work request")
            allowed = {
                "id",
                "title",
                "body",
                "instance_id",
                "needs",
                "priority",
                "model",
                "reviewer",
                "cancel",
            }
            if set(change) - allowed:
                raise ValueError(
                    f"Unknown card fields: {sorted(set(change) - allowed)}"
                )
            values = dict(row) if row is not None else {}
            values.update(change)
            title = _text(values.get("title"), "title")
            body = _text(values.get("body"), "body")
            if "## acceptance criteria" not in body.lower():
                raise ValueError("Card body must contain ## acceptance criteria")
            executor = instance(con, values.get("instance_id"))
            if executor["role"] != "sister" or executor["retired"]:
                raise ValueError("Cards require a non-retired Sister instance")
            if row is not None and row["status"] not in ("ready", "todo", "held"):
                raise ValueError(
                    f"Card {tid} is {row['status']}; stop/review it through its lifecycle or create a follow-up"
                )
            if type(values.get("priority", 0)) is not int:
                raise ValueError("priority must be an integer")
            for key in ("model", "reviewer"):
                if values.get(key) is not None and not isinstance(values[key], str):
                    raise TypeError(f"{key} must be text or null")
            if values.get("reviewer") == executor["template_id"]:
                raise ValueError("The reviewer must be different from the assignee")
            if "cancel" in change and type(change["cancel"]) is not bool:
                raise ValueError("cancel must be boolean")
            if row is None:
                tasks.create_task(
                    con,
                    title,
                    body=body,
                    request_id=request_id,
                    instance_id=executor["id"],
                    workspace=current["workspace"],
                    priority=values.get("priority", 0),
                    model=values.get("model"),
                    reviewer=values.get("reviewer"),
                    task_id=tid,
                )
            else:
                con.execute(
                    "UPDATE tasks SET title=?,body=?,instance_id=?,priority=?,model=?,reviewer=?,generation=generation+1 WHERE id=?",
                    (
                        title,
                        body,
                        executor["id"],
                        values.get("priority", 0),
                        values.get("model"),
                        values.get("reviewer"),
                        tid,
                    ),
                )
                previous = {
                    key: row[key]
                    for key in (
                        "title",
                        "body",
                        "instance_id",
                        "model",
                        "reviewer",
                        "priority",
                    )
                }
                previous["needs"] = tasks.parent_ids(con, tid)
                tasks.add_event(
                    con,
                    tid,
                    "contract_updated",
                    {
                        "revision": expected_revision + 1,
                        "previous_generation": row["generation"],
                        "previous_contract": previous,
                    },
                )
            if change.get("cancel"):
                con.execute(
                    "UPDATE tasks SET status='stopped',completed_at=? WHERE id=?",
                    (int(time.time()), tid),
                )
                tasks.add_event(con, tid, "cancelled", {})
            if "needs" in change:
                needs = change["needs"]
                if not isinstance(needs, list) or any(
                    not isinstance(p, str) for p in needs
                ):
                    raise ValueError("needs must be a list of card IDs")
                con.execute("DELETE FROM task_dependencies WHERE child_id=?", (tid,))
                # Deferred FK checks allow forward references within this atomic edit.
                for parent in dict.fromkeys(needs):
                    if parent == tid:
                        raise ValueError(f"Card {tid} cannot depend on itself")
                    con.execute(
                        "INSERT INTO task_dependencies(child_id,parent_id) VALUES(?,?)",
                        (tid, parent),
                    )
        validate_graph(con, request_id)
        for row in con.execute(
            "SELECT id FROM card_state WHERE request_id=? AND status IN ('ready','todo')",
            (request_id,),
        ).fetchall():
            tid = row["id"]
            state, _parents = tasks.dependency_state(con, tid)
            con.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                ("ready" if state == "ready" else "todo", tid),
            )
        con.execute(
            "UPDATE work_requests SET revision=revision+1 WHERE id=?", (request_id,)
        )
        return request(con, request_id)


def apply(con, operations):
    """One transaction for an externally supplied batch; any failed operation rolls back all."""
    from misaka.core.platform import tasks

    if not isinstance(operations, list) or not operations:
        raise ValueError("operations must be a nonempty list")
    lock = con.serialized() if hasattr(con, "serialized") else nullcontext()
    results = []
    with lock, tasks._write_txn(con):
        for operation in operations:
            if not isinstance(operation, dict):
                raise TypeError("Each operation must be an object")
            op = dict(operation)
            kind = op.pop("op", None)
            now = int(time.time())
            if kind in ("template.create", "template.revise"):
                tid = identifier(op.pop("id", None))
                latest = con.execute(
                    "SELECT * FROM templates WHERE id=? ORDER BY revision DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                expected = op.pop("expected_revision", None)
                if kind == "template.create" and latest is not None:
                    raise ValueError(f"Template already exists: {tid}")
                if kind == "template.revise" and (
                    latest is None
                    or type(expected) is not int
                    or expected != latest["revision"]
                ):
                    raise ValueError("Template revision conflict")
                role = op.pop("role", latest["role"] if latest is not None else None)
                if (
                    role not in ("last-order", "sister")
                    or latest is not None
                    and role != latest["role"]
                ):
                    raise ValueError(
                        "Template role must be last-order or sister and cannot change"
                    )
                config = _json(op.pop("config", None))
                revision = latest["revision"] + 1 if latest is not None else 1
                if op:
                    raise ValueError(f"Unknown fields: {sorted(op)}")
                con.execute(
                    "INSERT INTO templates VALUES(?,?,?,?,?)",
                    (tid, revision, role, config, now),
                )
                results.append({"id": tid, "revision": revision})
            elif kind == "instance.create":
                iid = identifier(op.pop("id", None))
                tid = identifier(op.pop("template_id", None))
                revision = op.pop("template_revision", None)
                if (
                    type(revision) is not int
                    or con.execute(
                        "SELECT 1 FROM templates WHERE id=? AND revision=?",
                        (tid, revision),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Instance must pin an existing template revision")
                overrides = _json(op.pop("overrides", {}))
                label = _text(op.pop("label", iid), "label")
                if op:
                    raise ValueError(f"Unknown fields: {sorted(op)}")
                con.execute(
                    "INSERT INTO instances(id,template_id,template_revision,overrides_json,label,created_at) VALUES(?,?,?,?,?,?)",
                    (iid, tid, revision, overrides, label, now),
                )
                results.append(instance(con, iid))
            elif kind == "instance.new-session":
                from misaka.core.platform.instances import new_session
                iid = identifier(op.pop("id", None))
                epoch = op.pop("expected_epoch", None)
                if op:
                    raise ValueError(f"Unknown fields: {sorted(op)}")
                results.append(new_session(con, iid, epoch))
            elif kind == "request.create":
                rid = identifier(op.pop("id", None))
                owner = instance(con, op.pop("owner_id", None))
                if owner["role"] != "last-order" or owner["retired"]:
                    raise ValueError(
                        "A work request needs a non-retired Last Order owner"
                    )
                title = _text(op.pop("title", None), "title")
                workspace = str(
                    Path(_text(op.pop("workspace", None), "workspace"))
                    .expanduser()
                    .resolve()
                )
                body = op.pop("body", "")
                if not isinstance(body, str) or op:
                    raise ValueError("Invalid request fields")
                con.execute(
                    "INSERT INTO work_requests(id,title,body,workspace,owner_id,created_at) VALUES(?,?,?,?,?,?)",
                    (rid, title, body, workspace, owner["id"], now),
                )
                results.append(request(con, rid))
            elif kind == "request.update":
                rid = op.pop("id", None)
                current = _revision(con, rid, op.pop("expected_revision", None))
                title = _text(op.pop("title", current["title"]), "title")
                body = op.pop("body", current["body"])
                owner = coordinator(con, op.pop("owner_id", current["owner_id"]))
                if not isinstance(body, str) or op:
                    raise ValueError("Invalid request fields")
                con.execute(
                    "UPDATE work_requests SET title=?,body=?,owner_id=?,revision=revision+1 WHERE id=?",
                    (title, body, owner["id"], rid),
                )
                results.append(request(con, rid))
            elif kind == "plan.update":
                rid = op.pop("request_id", None)
                revision = op.pop("expected_revision", None)
                changes = op.pop("cards", None)
                if op:
                    raise ValueError(f"Unknown fields: {sorted(op)}")
                results.append(update_plan(con, rid, revision, changes))
            else:
                raise ValueError(f"Unknown state operation: {kind}")
    return results


def state(con):
    from misaka.core.platform import tasks

    templates = []
    for row in con.execute("SELECT * FROM templates ORDER BY id,revision"):
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        templates.append(item)
    return {
        "templates": templates,
        "instances": [
            instance(con, r["id"])
            for r in con.execute("SELECT id FROM instances ORDER BY id")
        ],
        "requests": [
            dict(r) for r in con.execute("SELECT * FROM work_requests ORDER BY id")
        ],
        "cards": [
            dict(r) | {"needs": tasks.parent_ids(con, r["id"])}
            for r in con.execute("SELECT * FROM card_state ORDER BY request_id,id")
        ],
    }
