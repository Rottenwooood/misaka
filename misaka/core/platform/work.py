"""Work requests, immutable templates and durable instance identities in the board DB.

No model calls and no process lifecycle here. Every plan edit is an atomic command
with an expected revision; card and dependency readers use the same committed data.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from contextlib import nullcontext
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
 id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision > 0),
 parent_revision INTEGER,
 role TEXT NOT NULL CHECK(role IN ('last-order','sister')),
 config_json TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(id,revision)
);
CREATE TABLE IF NOT EXISTS instances (
 id TEXT PRIMARY KEY,
 template_id TEXT NOT NULL,
 template_revision INTEGER NOT NULL,
 label TEXT NOT NULL,
 subscriptions TEXT, -- JSON list of source/type pairs this instance listens for
 retired INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0,1)),
 created_at INTEGER NOT NULL,
 session_epoch INTEGER NOT NULL DEFAULT 1,
 session_file TEXT,
 agent_id TEXT,
 writer_pid INTEGER,
 writer_identity TEXT,
 writer_token TEXT,
 last_active_at INTEGER,
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
CARD_ID_PATTERN = r"^t_[0-9a-f]{6}$"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid identity: {value!r}")
    return value


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value.strip()


def _stored_list(value, field):
    """A field read back from the board is JSON text; a change supplied by a caller is a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ValueError(f"{field} is not stored as a list") from error
    return _string_list(value, field)


def _string_list(value, field):
    """A list of unique nonempty strings, or nothing."""
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of nonempty strings")
    return list(dict.fromkeys(item.strip() for item in value))


def _subscription_specs(value):
    """Card subscriptions: source, type and an optional target, nothing else."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("subscriptions must be a list")
    specs = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"source", "type", "target"}:
            raise ValueError(
                "Each subscription takes source, type and an optional target"
            )
        target = item.get("target")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise ValueError("A subscription target must be nonempty text")
        specs.append({
            "source": _text(item.get("source"), "subscription source"),
            "type": _text(item.get("type"), "subscription type"),
            "target": target.strip() if target else None,
        })
    return specs


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
    data["config"] = json.loads(data.pop("config_json"))
    if data["session_file"]:
        from misaka.config import home
        data["session_file"] = str(home.from_stored(data["session_file"]))
    return data


def instance_status(con, instance_id=None):
    """Each instance's session state: running a card, idle, or never started."""
    from misaka.core.platform import processes

    rows = con.execute(
        "SELECT i.id,i.session_file,i.agent_id,i.writer_pid,i.writer_identity,"
        "i.last_active_at FROM instances i WHERE (? IS NULL OR i.id=?) ORDER BY i.id",
        (instance_id, instance_id),
    ).fetchall()
    result = []
    for row in rows:
        running = con.execute(
            "SELECT id FROM tasks WHERE instance_id=? AND status IN ('running','review') "
            "ORDER BY started_at DESC, id LIMIT 1",
            (row["id"],),
        ).fetchone()
        pid = row["writer_pid"] if processes.identity_is_alive(
            row["writer_pid"], row["writer_identity"]) else None
        # A read-only viewer and an older session can be alive without the instance's
        # writer pointer (the panel still sees them in its catalog). Include that live
        # process so roster and panel do not disagree about who is running.
        live_record = None
        if pid is None:
            from misaka.core import session_catalog
            live_record = session_catalog.live_instance_session(row["id"])
            if live_record and processes.identity_is_alive(
                    live_record.get("pid"), live_record.get("identity")):
                pid = live_record.get("pid")
        session = "live" if running else (
            "idle" if (row["session_file"] or row["agent_id"] or row["writer_pid"]
                       or live_record) else "none")
        result.append({
            "id": row["id"],
            "session": session,
            "pid": pid,
            "running_card": running["id"] if running else None,
            "last_active_at": row["last_active_at"],
        })
    return result


def instance_specs(value):
    """Instance subscriptions: one (source, type) pair each, with an enabled flag."""
    if isinstance(value, str):
        value = json.loads(value or "[]")
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("subscriptions must be a list")
    unique = {}
    for item in value:
        if not isinstance(item, dict) or set(item) - {"source", "type", "enabled", "target"}:
            raise ValueError("Each subscription takes source, type and an optional enabled flag")
        source = _text(item.get("source"), "subscription source")
        kind = _text(item.get("type"), "subscription type")
        enabled = item.get("enabled")
        if enabled is None:
            enabled = True
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        unique[(source, kind)] = {"source": source, "type": kind, "enabled": enabled}
    return [unique[key] for key in sorted(unique)]


def instance_subscriptions(con, instance_id, operation, items=None):
    """List, add, remove or enable instance subscriptions; the caller syncs the centre."""
    item = instance(con, instance_id)
    current = instance_specs(item["subscriptions"])
    by_key = {(entry["source"], entry["type"]): entry for entry in current}
    changed = False
    failed = []
    if operation == "list":
        return {"id": instance_id, "subscriptions": current, "failed": failed}
    if operation in ("add", "set_enabled"):
        for raw in items or ():
            entry = instance_specs([raw])
            if not entry:
                continue
            spec = entry[0]
            key = (spec["source"], spec["type"])
            if operation == "add":
                if key in by_key:
                    if raw.get("enabled") is not None:
                        by_key[key]["enabled"] = spec["enabled"]
                        changed = True
                else:
                    by_key[key] = spec
                    changed = True
            elif key in by_key:
                by_key[key]["enabled"] = spec["enabled"]
                changed = True
            else:
                failed.append({"source": spec["source"], "type": spec["type"],
                               "reason": "not_found"})
    elif operation == "remove":
        for raw in items or ():
            entry = instance_specs([raw])
            if not entry:
                continue
            key = (entry[0]["source"], entry[0]["type"])
            if key in by_key:
                del by_key[key]
                changed = True
            else:
                failed.append({"source": key[0], "type": key[1], "reason": "not_found"})
    else:
        raise ValueError(f"Unknown subscription operation: {operation}")
    current = [by_key[key] for key in sorted(by_key)]
    if changed:
        from misaka.core.platform import tasks

        with tasks.write_txn(con):
            con.execute("UPDATE instances SET subscriptions=? WHERE id=?",
                        (json.dumps(current, ensure_ascii=False), instance_id))
    return {"id": instance_id, "subscriptions": current, "failed": failed}


def derive_node(con, instance_id, patch):
    """Create a child revision of the instance's template and repoint the instance.

    Instances carry no configuration of their own, so a change to one instance's
    configuration is a new node in that template's tree.
    """
    item = instance(con, instance_id)
    row = con.execute(
        "SELECT * FROM templates WHERE id=? AND revision=?",
        (item["template_id"], item["template_revision"]),
    ).fetchone()
    merged = {**json.loads(row["config_json"]), **patch}
    revision = con.execute(
        "SELECT MAX(revision) FROM templates WHERE id=?", (item["template_id"],)
    ).fetchone()[0] + 1
    con.execute(
        "INSERT INTO templates(id,revision,parent_revision,role,config_json,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (item["template_id"], revision, item["template_revision"], row["role"],
         _json(merged), int(time.time())),
    )
    con.execute(
        "UPDATE instances SET template_revision=? WHERE id=?", (revision, instance_id)
    )
    return revision


def tag_experience(con, card_id, label=None):
    """Mark a finished card's history as an experience the instance may reuse.

    The board stores what happened and where it is; the history layer still holds the
    conversation. Isolation of the marked range is a separate action.
    """
    from misaka.core.platform import tasks

    row = tasks.get(con, card_id)
    if row is None:
        raise ValueError(f"Card not found: {card_id}")
    text = _text(label if label is not None else row["title"], "label")
    with tasks.write_txn(con):
        con.execute(
            "INSERT INTO experience_tags(card_id,instance_id,label,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(card_id) DO UPDATE SET label=excluded.label",
            (card_id, row["instance_id"], text, int(time.time())),
        )
    return {"card_id": card_id, "instance_id": row["instance_id"], "label": text}


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
            if not re.fullmatch(CARD_ID_PATTERN, tid):
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
                "fs_scope",
                "forbidden_commands",
                "subscriptions",
                "completion",
            }
            if set(change) - allowed:
                raise ValueError(
                    f"Unknown card fields: {sorted(set(change) - allowed)}"
                )
            values = dict(row) if row is not None else {}
            values.update(change)
            title = _text(values.get("title"), "title")
            body = _text(values.get("body"), "body")
            completion = values.get("completion", row["completion"] if row is not None else "once")
            if completion not in ("once", "repeat_keep", "repeat_clear"):
                raise ValueError("completion must be once, repeat_keep or repeat_clear")
            if row is not None and completion != row["completion"]:
                raise ValueError("A card's completion type is fixed at creation")
            executor = instance(con, values.get("instance_id"))
            reusing_retired = (
                row is not None and row["completion"] == "repeat_clear" and executor["retired"]
            )
            if executor["role"] != "sister" or (executor["retired"] and not reusing_retired):
                raise ValueError("Cards require a non-retired Sister instance")
            if completion == "repeat_clear" and not reusing_retired and (
                executor["session_file"] or executor["writer_pid"]
            ):
                raise ValueError("A repeat_clear card needs a fresh instance with no session")
            if row is not None and row["status"] not in ("ready", "todo", "held", "blocked", "triage"):
                raise ValueError(
                    f"Card {tid} is {row['status']}; stop it with the stop action, or create a follow-up"
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
            fs_scope = _stored_list(values.get("fs_scope"), "fs_scope")
            forbidden = _stored_list(values.get("forbidden_commands"), "forbidden_commands")
            specs = (_subscription_specs(change.get("subscriptions"))
                     if "subscriptions" in change else None)
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
                    fs_scope=fs_scope,
                    forbidden_commands=forbidden,
                    subscriptions=specs,
                    completion=completion,
                    template_id=executor["template_id"],
                    template_revision=executor["template_revision"],
                )
            else:
                con.execute(
                    "UPDATE tasks SET title=?,body=?,instance_id=?,priority=?,model=?,reviewer=?,"
                    "fs_scope=?,forbidden_commands=?,"
                    "subscriptions=COALESCE(?,subscriptions),generation=generation+1 WHERE id=?",
                    (
                        title,
                        body,
                        executor["id"],
                        values.get("priority", 0),
                        values.get("model"),
                        values.get("reviewer"),
                        None if fs_scope is None else json.dumps(fs_scope),
                        None if forbidden is None else json.dumps(forbidden),
                        None if specs is None else json.dumps(specs),
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
                        "fs_scope",
                        "forbidden_commands",
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
                parent = op.pop("parent_revision", None)
                if kind == "template.revise" and parent is None:
                    parent = latest["revision"]
                if parent is not None and (
                    type(parent) is not int
                    or con.execute(
                        "SELECT 1 FROM templates WHERE id=? AND revision=?", (tid, parent)
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Parent revision must exist in the same template")
                revision = latest["revision"] + 1 if latest is not None else 1
                if op:
                    raise ValueError(f"Unknown fields: {sorted(op)}")
                con.execute(
                    "INSERT INTO templates(id,revision,parent_revision,role,config_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (tid, revision, parent if latest is not None else None, role, config, now),
                )
                results.append({"id": tid, "revision": revision,
                                "parent_revision": parent if latest is not None else None})
            elif kind == "instance.create":
                iid = identifier(op.pop("id", None))
                tid = identifier(op.pop("template_id", None))
                revision = op.pop("template_revision", None)
                config = op.pop("config", None)
                role = op.pop("role", None)
                label = _text(op.pop("label", iid), "label")
                if op:
                    raise ValueError(f"Unknown fields: {sorted(op)}")
                if revision is None:
                    if config is None or role not in ("last-order", "sister"):
                        raise ValueError(
                            "A new template needs a role and a full configuration"
                        )
                    if con.execute(
                        "SELECT 1 FROM templates WHERE id=?", (tid,)
                    ).fetchone() is not None:
                        raise ValueError(
                            "Template already exists; pass template_revision and a patch instead"
                        )
                    revision = 1
                    con.execute(
                        "INSERT INTO templates(id,revision,parent_revision,role,config_json,created_at) "
                        "VALUES(?,?,NULL,?,?,?)",
                        (tid, revision, role, _json(config), now),
                    )
                else:
                    parent = con.execute(
                        "SELECT * FROM templates WHERE id=? AND revision=?",
                        (tid, revision),
                    ).fetchone()
                    if parent is None:
                        raise ValueError(
                            "Instance must pin an existing template revision"
                        )
                    if config is not None:
                        merged = {**json.loads(parent["config_json"]), **config}
                        if merged == json.loads(parent["config_json"]):
                            config = None  # a patch that changes nothing pins the parent
                        else:
                            revision = con.execute(
                                "SELECT MAX(revision) FROM templates WHERE id=?", (tid,)
                            ).fetchone()[0] + 1
                            con.execute(
                                "INSERT INTO templates(id,revision,parent_revision,role,config_json,created_at) "
                                "VALUES(?,?,?,?,?,?)",
                                (tid, revision, parent["revision"], parent["role"],
                                 _json(merged), now),
                            )
                con.execute(
                    "INSERT INTO instances(id,template_id,template_revision,label,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (iid, tid, revision, label, now),
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


def roster(con):
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
    }


def state(con):
    from misaka.core.platform import tasks

    return {
        **roster(con),
        "requests": [
            dict(r) for r in con.execute("SELECT * FROM work_requests ORDER BY id")
        ],
        "cards": [
            dict(r) | {"needs": tasks.parent_ids(con, r["id"])}
            for r in con.execute("SELECT * FROM card_state ORDER BY request_id,id")
        ],
    }


def owned_request(con, owner, request_id):
    current = request(con, request_id)
    if current["owner_id"] != owner:
        raise ValueError(f"Request {request_id} is not owned by this Last Order")
    return current


def ordinary_cards(con, card_ids):
    """Research-owned cards must be edited and launched by their driver."""
    from misaka.core.research import runs

    if not card_ids:
        return
    contexts = runs.task_contexts(con)
    for card_id in card_ids:
        if context := contexts.get(card_id):
            raise ValueError(
                f"Card {card_id} belongs to research run {context['run_id']}; "
                "its driver owns the plan and execution. "
                f"Use the current Research tools or /research resume {context['run_id']}."
            )


def owned_state(con, owner, request_id=None, *, include_body=False):
    """A status-oriented view: bodies and empty lists are opt-in noise.

    ``include_body`` is for reading the Markdown contract, not for every status poll.
    """
    from misaka.core.platform import tasks

    if request_id is None:
        return {"requests": [dict(row) for row in con.execute(
            "SELECT id,title,workspace,revision FROM work_requests WHERE owner_id=? ORDER BY created_at,id", (owner,)
        )]}
    current = owned_request(con, owner, request_id)
    request_view = {key: current[key] for key in ("id", "title", "workspace", "revision")}
    if include_body:
        request_view["body"] = current["body"]
    rows = con.execute("SELECT * FROM card_state WHERE request_id=? ORDER BY created_at,id", (request_id,))
    cards = []
    for row in rows:
        card = {key: row[key] for key in ("id", "title", "status", "generation", "completion")
                if row[key] is not None}
        if include_body and row["body"]:
            card["body"] = row["body"]
        if row["completion"] != "once":
            card["completion_count"] = row["completion_count"]
            if row["last_completed_at"] is not None:
                card["last_completed_at"] = row["last_completed_at"]
        if row["source_card_id"]:
            card["source_card_id"] = row["source_card_id"]
        card["instance"] = row["instance_id"]
        card["needs"] = tasks.parent_ids(con, row["id"])
        if scoped := json.loads(row["fs_scope"] or "[]"):
            card["fs_scope"] = scoped
        card["forbidden_commands"] = json.loads(row["forbidden_commands"] or "[]")
        card["subscriptions"] = json.loads(row["subscriptions"] or "[]")
        if row["block_reason"]:
            card["blocked"] = row["block_reason"]
        cards.append(card)
    return {"request": request_view, "cards": cards}


def save_plan(con, owner, request_id, *, expected_revision=None, workspace=None,
              title=None, body=None, cards=None):
    from misaka.core.platform import tasks

    with tasks.write_txn(con):
        if expected_revision is None:
            current, = apply(con, [{"op": "request.create", "id": request_id, "owner_id": owner,
                                    "workspace": workspace, "title": title, "body": body or ""}])
        else:
            current = owned_request(con, owner, request_id)
            _revision(con, request_id, expected_revision)
            if workspace is not None:
                raise ValueError("An existing request cannot change its project directory")
        if cards:
            ordinary_cards(con, [card["id"] for card in cards] +
                           [parent for card in cards for parent in (card.get("needs") or [])])
            current = update_plan(con, request_id, current["revision"], cards)
        if expected_revision is not None and (title is not None or body is not None):
            # A single save increments the revision once, even if it edits both metadata and cards.
            con.execute(
                "UPDATE work_requests SET title=?,body=?,revision=? WHERE id=?",
                (_text(title, "title") if title is not None else current["title"],
                 body if body is not None else current["body"],
                 expected_revision + 1, request_id),
            )
        return request(con, request_id)


def delete_instance(con, instance_id):
    """Remove an instance that owns no cards; its session logs stay on disk."""
    instance(con, instance_id)
    if con.execute("SELECT 1 FROM tasks WHERE instance_id=? LIMIT 1", (instance_id,)).fetchone():
        raise ValueError(f"Instance {instance_id} has cards; reassign or remove them first")
    con.execute("DELETE FROM instance_session_archive WHERE instance_id=?", (instance_id,))
    con.execute("DELETE FROM instances WHERE id=?", (instance_id,))
    con.commit()


def delete_template(con, template_id, revision=None):
    """Remove one revision, or the whole template when every revision is free."""
    row = con.execute("SELECT 1 FROM templates WHERE id=? AND (? IS NULL OR revision=?)",
                      (template_id, revision, revision)).fetchone()
    if row is None:
        raise ValueError(f"Template not found: {template_id}")
    if con.execute("SELECT 1 FROM instances WHERE template_id=? AND (? IS NULL OR template_revision=?)",
                   (template_id, revision, revision)).fetchone():
        raise ValueError(f"Template {template_id} is pinned by an instance")
    if con.execute("SELECT 1 FROM templates WHERE parent_revision IS NOT NULL AND id=? AND (? IS NULL OR parent_revision=?)",
                   (template_id, revision, revision)).fetchone():
        raise ValueError(f"Template {template_id} has a newer revision derived from it")
    if revision is None:
        con.execute("DELETE FROM templates WHERE id=?", (template_id,))
    else:
        con.execute("DELETE FROM templates WHERE id=? AND revision=?", (template_id, revision))
    con.commit()


def bulk_delete(con, kind, ids, revisions=None):
    """Delete or retire many owners; one failure does not stop the rest."""
    deleted, retired, failed = [], [], []
    for item_id in ids:
        try:
            if kind == "instance":
                instance(con, item_id)
                if con.execute("SELECT 1 FROM tasks WHERE instance_id=? LIMIT 1",
                               (item_id,)).fetchone():
                    if con.execute(
                        "SELECT 1 FROM tasks WHERE instance_id=? AND status IN ('running','review')",
                        (item_id,),
                    ).fetchone():
                        raise ValueError("has_active_cards")
                    con.execute("UPDATE instances SET retired=1 WHERE id=?", (item_id,))
                    con.commit()
                    retired.append(item_id)
                else:
                    delete_instance(con, item_id)
                    deleted.append(item_id)
            elif kind == "template":
                for revision in revisions or [None]:
                    label = item_id if revision is None else f"{item_id}:{revision}"
                    try:
                        delete_template(con, item_id, revision)
                        deleted.append(label)
                    except ValueError as error:
                        reason = str(error)
                        if "pinned" in reason or "derived" in reason:
                            reason = "in_use"
                        failed.append({"id": label, "reason": reason})
            else:
                raise ValueError(f"Unknown bulk_delete kind: {kind}")
        except ValueError as error:
            reason = str(error)
            if "pinned" in reason or "derived" in reason:
                reason = "in_use"
            failed.append({"id": item_id, "reason": reason})
    return {"deleted": deleted, "retired": retired, "failed": failed}


def cancel_card(con, card_id):
    """Take a card off the board without deleting it; any status may be cancelled."""
    from misaka.core.platform import tasks

    status = tasks.cancel_task(con, card_id)
    if status is None:
        raise ValueError(f"Card not found: {card_id}")
    return {"id": card_id, "status": status}


def _drop_card_subscriptions(card_id, specs):
    """Best effort: stop the centre delivering a card that is gone."""
    if not specs:
        return
    try:
        from misaka.core.messaging import config as center_config
        from misaka.core.messaging.sync import SubscriptionSync

        client = center_config.center()
        if client is None:
            return
        names = [SubscriptionSync.name_for(card_id, index) for index in range(len(specs))]
        SubscriptionSync(client, "").clear(names)
    except Exception:
        logger.warning("Could not drop subscriptions for %s", card_id, exc_info=True)


def delete_card(con, card_id, *, allow_active=False, detach=False):
    """Remove a card's rows and its file mirror.

    ``detach`` lets a hard cancel remove a card others depended on: the edges are
    withdrawn first and a child that is now runnable is promoted.
    """
    from misaka.core.platform import cards as card_files
    from misaka.core.platform import tasks

    row = tasks.get(con, card_id)
    if row is None:
        raise ValueError(f"Card not found: {card_id}")
    if tasks._children_of(con, card_id) and not detach:
        raise ValueError(f"Card {card_id} still has dependents; edit the plan before deleting it")
    children = tasks._children_of(con, card_id)
    if detach and children:
        with tasks.write_txn(con):
            con.execute("DELETE FROM task_dependencies WHERE parent_id=?", (card_id,))
    workspace = row["workspace"]
    specs = json.loads(row["subscriptions"] or "[]")
    ok, message = tasks.delete_task(con, card_id, allow_active=allow_active)
    if not ok:
        raise ValueError(message)
    import shutil
    shutil.rmtree(card_files.attachment_dir(workspace, card_id), ignore_errors=True)
    Path(card_files.card_path(workspace, card_id)).unlink(missing_ok=True)
    _drop_card_subscriptions(card_id, specs)
    for child in children:
        tasks.promote_task(con, child)


def delete_request(con, request_id, *, force=False):
    """Delete a request and its cards atomically; active cards refuse unless ``force``."""
    from misaka.core.platform import cards as card_files
    from misaka.core.platform import tasks

    request = con.execute("SELECT id FROM work_requests WHERE id=?", (request_id,)).fetchone()
    if request is None:
        return {"id": request_id, "status": "not_found", "deleted_cards": 0}
    rows = con.execute("SELECT id,status FROM tasks WHERE request_id=? ORDER BY id",
                       (request_id,)).fetchall()
    active = [row["id"] for row in rows
              if row["status"] in ("running", "review", "ready", "todo", "blocked", "triage")]
    if active and not force:
        return {"id": request_id, "status": "blocked", "reason": "has_active_cards",
                "deleted_cards": 0}
    remaining = [row["id"] for row in rows]
    deleted_rows = []
    with tasks.write_txn(con):
        while remaining:
            progress = False
            for card_id in list(remaining):
                if tasks._children_of(con, card_id):
                    continue
                row = tasks.get(con, card_id)
                ok, message = tasks.delete_task(con, card_id, allow_active=True)
                if not ok:
                    raise ValueError(message)
                deleted_rows.append((card_id, row["workspace"],
                                     json.loads(row["subscriptions"] or "[]")))
                remaining.remove(card_id)
                progress = True
            if not progress:
                raise ValueError("Card graph is cyclic")
        con.execute("DELETE FROM work_requests WHERE id=?", (request_id,))
    for card_id, workspace, specs in deleted_rows:
        import shutil

        shutil.rmtree(card_files.attachment_dir(workspace, card_id), ignore_errors=True)
        Path(card_files.card_path(workspace, card_id)).unlink(missing_ok=True)
        _drop_card_subscriptions(card_id, specs)
    return {"id": request_id, "status": "deleted", "deleted_cards": len(deleted_rows)}


def review_card(con, card_id, verdict, feedback=None):
    """A person's verdict on finished work: keep it done, or send it back as a new generation."""
    from misaka.core.platform import tasks

    row = tasks.get(con, card_id)
    if row is None:
        raise ValueError(f"Card not found: {card_id}")
    if row["status"] not in ("done", "blocked"):
        raise ValueError(f"Card {card_id} is {row['status']}; only a done or blocked card can be reviewed")
    feedback = str(feedback or "").strip()
    if verdict == "reject" and row["status"] != "done":
        raise ValueError(f"Card {card_id} is {row['status']}; only done work can be sent back")
    if verdict == "reject" and not feedback:
        raise ValueError("Rejecting a card requires feedback")
    with tasks.write_txn(con):
        if verdict == "accept":
            if row["status"] == "blocked":
                con.execute(
                    "UPDATE tasks SET status='done',completed_at=?,block_kind=NULL,block_reason=NULL,"
                    "block_fingerprint=NULL,block_recurrence=0,blocked_at=NULL,claim_lock=NULL,"
                    "worker_pid=NULL,worker_identity=NULL WHERE id=?",
                    (int(time.time()), card_id))
                tasks.promote_dependents(con, card_id)
            tasks.add_event(con, card_id, "review_accepted", {"feedback": feedback[:4000]})
            status = "done"
        else:
            state, _parents = tasks.dependency_state(con, card_id)
            status = "ready" if state == "ready" else "todo"
            con.execute(
                "UPDATE tasks SET status=?,generation=generation+1,review_feedback=?,completed_at=NULL,"
                "block_kind=NULL,block_reason=NULL,block_fingerprint=NULL,block_recurrence=0,"
                "blocked_at=NULL,claim_lock=NULL,worker_pid=NULL,"
                "worker_identity=NULL WHERE id=?",
                (status, feedback[:4000], card_id))
            tasks.add_event(con, card_id, "review_rejected", {"feedback": feedback[:4000]})
    current = tasks.get(con, card_id)
    return {"id": card_id, "status": status, "generation": int(current["generation"])}


def _submission(con, card_id, generation):
    from misaka.core.platform import tasks

    for candidate in (generation, None):
        raw = tasks.latest_payload(con, card_id, "submitted", generation=candidate)
        if raw:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                return value
    return {}


def card_result(con, card_id, include=None):
    """The structured result a card already declared; no transcript needed."""
    from misaka.core.platform import tasks

    row = tasks.get(con, card_id)
    if row is None:
        raise ValueError(f"Card not found: {card_id}")
    keys = ({"summary", "artifacts", "uncertain", "error"}
            if include is None else set(include))
    submitted = _submission(con, card_id, int(row["generation"])) if row["status"] in ("done", "blocked") else {}
    result = {"id": card_id, "status": row["status"]}
    if "summary" in keys:
        result["summary"] = submitted.get("summary") or None
    if "artifacts" in keys:
        result["artifacts"] = card_artifacts(con, card_id) if submitted else []
    if "uncertain" in keys:
        result["uncertain"] = submitted.get("uncertain") or []
    if "error" in keys and row["status"] == "blocked":
        result["error"] = {"message": row["block_reason"] or "", "at": row["blocked_at"]}
    if row["status"] in ("done", "blocked"):
        result["finished_at"] = row["completed_at"]
    return result


def card_artifacts(con, card_id):
    """The files a submission lists, with size and digest taken from disk now."""
    from pathlib import Path

    from misaka.core.platform import tasks

    row = tasks.get(con, card_id)
    if row is None:
        raise ValueError(f"Card not found: {card_id}")
    submitted = _submission(con, card_id, int(row["generation"]))
    root = Path(row["workspace"]).resolve()
    items = []
    for name in submitted.get("artifacts") or []:
        path = (root / str(name)).resolve()
        try:
            path.relative_to(root)
            stat = path.stat()
            digest = submitted.get("artifact_digests", {}).get(str(name))
            items.append({"path": str(name), "bytes": stat.st_size, "digest": digest,
                          "created_at": int(stat.st_mtime)})
        except (OSError, ValueError):
            items.append({"path": str(name), "bytes": 0, "digest": None, "created_at": None})
    return items


def resume_card(con, card_id):
    """Return a blocked card to the board; its parent graph decides whether it waits."""
    from misaka.core.platform import tasks

    row = tasks.get(con, card_id)
    if row is None:
        raise ValueError(f"Card not found: {card_id}")
    if row["status"] != "blocked":
        raise ValueError(f"Card {card_id} is {row['status']}; only a blocked card resumes")
    if not tasks.unblock_task(con, card_id):
        raise ValueError(f"Card {card_id} could not be resumed")


def card_generation(con, card_id):
    from misaka.core.platform import tasks

    row = tasks.get(con, card_id)
    if row is None:
        raise ValueError(f"Card not found: {card_id}")
    return int(row["generation"])


def _fresh_instance(con, card_id, template_id, template_revision):
    """Create the per-run instance a repeat_clear card needs; caller owns the transaction."""

    if not template_id or template_revision is None:
        raise ValueError("A repeat_clear card has no template to instantiate")
    iid = f"{card_id}-{int(time.time())}-{secrets.token_hex(2)}"
    con.execute(
        "INSERT INTO instances(id,template_id,template_revision,label,created_at) VALUES(?,?,?,?,?)",
        (iid, template_id, template_revision, f"{card_id} run", int(time.time())),
    )
    return instance(con, iid)


def dispatch_target(con, owner, card_id, generation):
    from misaka.core.platform import tasks

    row = tasks.get(con, card_id)
    if row is None:
        raise ValueError(f"Card not found: {card_id}")
    owned_request(con, owner, row["request_id"])
    ordinary_cards(con, [card_id])
    if generation is not None and int(row["generation"]) != int(generation):
        raise ValueError(f"Card {card_id} generation changed; read the current plan")
    if row["status"] != "ready":
        raise ValueError(f"Card {card_id} is not ready (current status: {row['status']})")
    if row["completion"] == "repeat_clear":
        current = instance(con, row["instance_id"])
        if current["retired"] or current["session_file"] or current["writer_pid"]:
            con.execute("UPDATE instances SET retired=1 WHERE id=?", (current["id"],))
            fresh = _fresh_instance(con, card_id, row["template_id"], row["template_revision"])
            con.execute("UPDATE tasks SET instance_id=? WHERE id=?", (fresh["id"], card_id))
            row = tasks.get(con, card_id)
    return row
