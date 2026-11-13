"""Instance-owned configuration and native conversation ownership.

The board is authoritative. Profile directories are empty resource anchors for the
engine, never a second settings/personality store. A process lock spans selection,
loading and disposal of a native conversation, including interactive /new.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import secrets
import time
from contextlib import closing, contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from misaka.config import home
from misaka.core.platform import processes, tasks, work

_CURRENT = contextvars.ContextVar("instance_writer", default=None)
logger = logging.getLogger(__name__)
_LAST_TOUCH: dict[str, float] = {}
CONFIG_KEYS = frozenset({"system", "model", "thinking", "tools", "skills", "settings",
                         "fs_scope", "forbidden_commands"})

# What a role starts with when its template does not name a tool list. Last Order coordinates:
# management and messages, no file or command tools. A template may list any tool its role
# provides, so a person who wants a Last Order that reads files can say so on the node.
# A Sister's entry is None: every tool its role provides.
DEFAULT_TOOLS = {
    "last-order": ("misaka_task", "misaka_roster", "misaka_usage", "misaka_context",
                   "misaka_skill", "SendMessage", "misaka_sister_peek"),
}


def default_tools(role):
    """The role's default tool list, or None when the role starts with every tool."""
    return DEFAULT_TOOLS.get("last-order" if role == "last-order" else "sister")


def validate_config(value):
    if not isinstance(value, dict):
        raise TypeError("Instance configuration must be an object")
    if unknown := set(value) - CONFIG_KEYS:
        raise ValueError(f"Unknown configuration fields: {sorted(unknown)}")
    for key in ("system", "model", "thinking"):
        if key in value and not isinstance(value[key], str):
            raise ValueError(f"{key} must be text")
    if "model" in value and (not value["model"].strip() or "/" not in value["model"]):
        raise ValueError("model must name provider/model")
    if "thinking" in value:
        from misaka.cli.args import is_valid_thinking_level
        if not is_valid_thinking_level(value["thinking"]):
            raise ValueError("Invalid thinking level")
    for key in ("tools", "skills"):
        if key in value and (not isinstance(value[key], list) or any(
            not isinstance(item, str) or not item.strip() for item in value[key]
        ) or len(set(value[key])) != len(value[key])):
            raise ValueError(f"{key} must be a list of unique nonempty names")
    for key in ("fs_scope", "forbidden_commands"):
        if key in value and (not isinstance(value[key], list) or any(
            not isinstance(item, str) or not item.strip() for item in value[key]
        ) or len(set(value[key])) != len(value[key])):
            raise ValueError(f"{key} must be a list of unique nonempty strings")
    if "settings" in value:
        from misaka.core.settings_manager import ROLE_KEYS
        if not isinstance(value["settings"], dict) or set(value["settings"]) - (ROLE_KEYS - {
            "defaultProvider", "defaultModel", "defaultThinkingLevel"
        }):
            raise ValueError("settings supports instance mcpServers and web sections; model/thinking have explicit fields")
        if any(not isinstance(v, dict) for v in value["settings"].values()):
            raise ValueError("Instance settings sections must be objects")
    return value


def get(instance_id):
    with closing(tasks.connect(str(home.path("db")))) as con:
        item = work.instance(con, instance_id)
    if item["retired"]:
        raise ValueError(f"Instance is retired: {instance_id}")
    return item


def profile(instance_id):
    item = get(instance_id)
    leaf = Path("last_order") if item["role"] == "last-order" else Path("sisters") / item["id"]
    path = home.path("instance_profiles") / item["id"] / "profiles" / leaf
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def profile_id(profile_dir):
    if not profile_dir:
        return None
    try:
        parts = Path(profile_dir).absolute().relative_to(home.path("instance_profiles")).parts
    except ValueError:
        return None
    if len(parts) not in (3, 4) or parts[1] != "profiles":
        raise ValueError("Malformed instance resource anchor")
    return work.identifier(parts[0])


def settings(instance_id):
    config = get(instance_id)["config"]
    result = dict(config.get("settings", {}))
    if config.get("model"):
        result["defaultProvider"], result["defaultModel"] = config["model"].split("/", 1)
    if "thinking" in config:
        result["defaultThinkingLevel"] = config["thinking"]
    return result


def mutate_settings(instance_id, mutate):
    """Persist engine selectors as a new template node for this instance."""
    with closing(tasks.connect(str(home.path("db")))) as con, tasks.write_txn(con):
        item = work.instance(con, instance_id)
        writer = _CURRENT.get()
        if processes.identity_is_alive(item["writer_pid"], item["writer_identity"]) and (
            not writer or writer.id != instance_id or writer.token != item["writer_token"]
        ):
            raise ValueError("Only the live instance writer can change its configuration")
        config = item["config"]
        data = dict(config.get("settings", {}))
        if config.get("model"):
            data["defaultProvider"], data["defaultModel"] = config["model"].split("/", 1)
        if "thinking" in config:
            data["defaultThinkingLevel"] = config["thinking"]
        mutate(data)
        patch = {}
        if data.get("defaultProvider") and data.get("defaultModel"):
            patch["model"] = f"{data.pop('defaultProvider')}/{data.pop('defaultModel')}"
        if "defaultThinkingLevel" in data:
            patch["thinking"] = data.pop("defaultThinkingLevel")
        patch["settings"] = data
        validate_config({**config, **patch})
        work.derive_node(con, item["id"], patch)
    return settings(instance_id)


def session_dir(instance_id):
    return str(home.path("sessions") / "instances" / work.identifier(instance_id))


def _lock(instance_id):
    path = home.path("locks") / f"instance-{work.identifier(instance_id)}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=0)


def _archive(con, item):
    con.execute("INSERT INTO instance_session_archive VALUES(?,?,?,?)", (
        item["id"], item["session_epoch"], home.stored(item["session_file"]) if item["session_file"] else None, int(time.time())))
    con.execute("UPDATE instances SET session_epoch=session_epoch+1,session_file=NULL,agent_id=NULL WHERE id=?", (item["id"],))


def new_session(con, instance_id, expected_epoch):
    """Explicit reset only; no damaged-file fallback and no history carry-over."""
    try:
        lock = _lock(instance_id)
        lock.acquire()
    except Timeout as error:
        raise ValueError(f"Instance {instance_id} has a live writer; close it before replacing its session") from error
    try:
        item = work.instance(con, instance_id)
        if type(expected_epoch) is not int or item["session_epoch"] != expected_epoch:
            raise ValueError("Instance session epoch conflict")
        if processes.identity_is_alive(item["writer_pid"], item["writer_identity"]):
            raise ValueError(f"Instance {instance_id} still has a live writer")
        _archive(con, item)
        return work.instance(con, instance_id)
    finally:
        lock.release()


def forget_session(con, instance_id):
    """Close an instance's session without deleting the instance; the transcript is archived."""
    with tasks.write_txn(con):
        item = work.instance(con, instance_id)
        if item["session_file"]:
            _archive(con, item)
        con.execute(
            "UPDATE instances SET writer_pid=NULL,writer_identity=NULL,writer_token=NULL,"
            "session_file=NULL,agent_id=NULL WHERE id=?",
            (instance_id,),
        )
    return work.instance(con, instance_id)


def touch(instance_id, *, force=False):
    """Record a live session's last tool boundary; a throttled, best-effort stamp."""
    now = time.time()
    if not force and now - _LAST_TOUCH.get(instance_id, 0.0) < 2.0:
        return
    _LAST_TOUCH[instance_id] = now
    try:
        with closing(tasks.connect(str(home.path("db")))) as con, tasks.write_txn(con):
            con.execute("UPDATE instances SET last_active_at=? WHERE id=?",
                        (int(now), instance_id))
    except Exception:
        logger.debug("Could not stamp activity for %s", instance_id, exc_info=True)


class Writer:
    def __init__(self, instance_id):
        self.id = instance_id
        self.token = secrets.token_hex(16)
        self.lock = _lock(instance_id)
        self.context_token = None
        self.owner_task = None

    def acquire(self):
        try:
            self.lock.acquire()
        except Timeout as error:
            raise ValueError(f"Instance {self.id} already has a live writer; attach to its session instead") from error
        try:
            with closing(tasks.connect(str(home.path("db")))) as con, tasks.write_txn(con):
                item = work.instance(con, self.id)
                if item["retired"]:
                    raise ValueError(f"Instance {self.id} is retired")
                if processes.identity_is_alive(item["writer_pid"], item["writer_identity"]):
                    raise ValueError(f"Instance {self.id} already has a live writer")
                if item["session_file"] and not Path(item["session_file"]).is_file():
                    raise ValueError(f"Instance {self.id} transcript is missing; restore it or explicitly replace the session")
                con.execute("UPDATE instances SET writer_pid=?,writer_identity=?,writer_token=? WHERE id=?", (
                    os.getpid(), processes.identity(os.getpid()), self.token, self.id))
            self.owner_task = asyncio.current_task()
            self.context_token = _CURRENT.set(self)
            return self
        except BaseException:
            self.lock.release()
            raise

    def flags(self, flags):
        """Choose the authoritative session after obtaining its exclusive writer lock."""
        item = get(self.id)
        result, index, chosen = [], 0, None
        root = Path(session_dir(self.id)).resolve()
        # A session in this instance's own history can be adopted and made writable again;
        # another instance's file is still refused.
        while index < len(flags):
            arg = flags[index]
            if arg == "--":
                result.extend(flags[index:])
                break
            if arg in ("--session", "--session-dir"):
                value = flags[index + 1]
                path = Path(value).expanduser().resolve()
                if arg == "--session-dir":
                    if path != root:
                        raise ValueError("An instance can write only inside its own session directory")
                else:
                    expected = Path(item["session_file"]).resolve() if item["session_file"] else None
                    if path != expected and not path.is_relative_to(root):
                        raise ValueError("An instance can write only its own sessions")
                    chosen = str(path)
                index += 2
                continue
            if arg in ("-c", "--continue", "-r", "--resume", "--fork", "--no-session"):
                raise ValueError("Instance sessions use their saved pointer; use instance.new-session to replace it")
            result.append(arg)
            index += 1
        selected = ["--session-dir", session_dir(self.id)]
        session = chosen or item["session_file"]
        if session:
            selected += ["--session", session]
        return selected + result

    def bind(self, path):
        pointer = home.stored(path)
        with closing(tasks.connect(str(home.path("db")))) as con, tasks.write_txn(con):
            item = work.instance(con, self.id)
            if item["writer_token"] != self.token:
                raise ValueError("Instance writer ownership was revoked")
            if item["session_file"] and os.path.realpath(item["session_file"]) != os.path.realpath(path):
                _archive(con, item)
            con.execute("UPDATE instances SET session_file=? WHERE id=? AND writer_token=?", (pointer, self.id, self.token))
            con.execute("UPDATE task_runs SET session_file=? WHERE id IN (SELECT current_run_id FROM tasks WHERE instance_id=? AND status='running')", (pointer, self.id))

    def close(self):
        try:
            with closing(tasks.connect(str(home.path("db")))) as con, tasks.write_txn(con):
                # A never-flushed native reservation is not a saved conversation.
                item = work.instance(con, self.id)
                if item["writer_token"] == self.token:
                    if item["session_file"] and not Path(item["session_file"]).exists():
                        con.execute("UPDATE instances SET session_file=NULL WHERE id=?", (self.id,))
                    con.execute("UPDATE instances SET writer_pid=NULL,writer_identity=NULL,writer_token=NULL WHERE id=? AND writer_token=?", (self.id, self.token))
        finally:
            self.restore_context()
            self.lock.release()

    def restore_context(self):
        # Shielded disposal runs in a child Task/Context. Release its physical
        # ownership there; only the opening task can pop its ContextVar token.
        if self.context_token is not None and asyncio.current_task() is self.owner_task:
            _CURRENT.reset(self.context_token)
            self.context_token = None


@contextmanager
def writing(instance_id):
    writer = Writer(instance_id).acquire()
    try:
        yield writer
    finally:
        writer.close()


def bind_current(instance_id, path):
    writer = _CURRENT.get()
    if not writer or writer.id != instance_id:
        raise ValueError("Instance session was assembled without its writer lock")
    writer.bind(path)


def current_state(instance_id):
    """Handoff contains current business state, never transcripts or historical events."""
    with closing(tasks.connect(str(home.path("db")))) as con, tasks.write_txn(con):
        state = work.state(con)
        requests = [r for r in state["requests"] if r["owner_id"] == instance_id]
        ids = {r["id"] for r in requests}
        cards = [{k: c[k] for k in ("id", "request_id", "instance_id", "title", "body", "status", "needs")}
                 for c in state["cards"] if c["request_id"] in ids and c["status"] not in ("done", "stopped")]
        instances = [{k: i[k] for k in ("id", "label", "role", "template_id", "template_revision", "config", "retired")}
                     for i in state["instances"]]
    # Connection settings can contain environment/header credentials. They are
    # local configuration, not automatic conversation context. Leave their
    # presence visible without copying values into a new model request.
    for item in [*state["templates"], *instances]:
        item["local_settings"] = bool(item["config"].pop("settings", {}))
    return {"templates": state["templates"], "instances": instances, "requests": requests, "active_cards": cards}


async def control(instance_id, operation):
    """Control the original writer through its existing fenced session socket."""
    from misaka.core import session_catalog, session_control
    if operation not in ("pause", "resume", "snapshot"):
        raise ValueError("Unsupported instance control")
    item = get(instance_id)
    if not item["session_file"] or not processes.identity_is_alive(item["writer_pid"], item["writer_identity"]):
        raise ValueError(f"Instance {instance_id} has no live writer; reopen its saved session")
    record = session_catalog.owner_record(item["session_file"])
    if record.get("instance_id") != instance_id or not record.get("control"):
        raise ValueError("The instance writer is starting or stopping; no control endpoint is available")
    return await session_control.request(record, operation)


def inventory(role=None):
    """Read-only registered actors for launchers; never initialize a store."""
    import sqlite3
    from urllib.parse import quote
    path = home.path("db")
    if not path.is_file():
        return []
    with closing(sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)) as con:
        from misaka.core.platform.tasks import require_schema
        require_schema(con, "tasks", tasks.TASK_SCHEMA_VERSION, populated=True)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT i.id,i.label,t.role FROM instances i JOIN templates t ON t.id=i.template_id AND t.revision=i.template_revision "
            "WHERE i.retired=0 AND (? IS NULL OR t.role=?) ORDER BY i.id", (role, role))]
