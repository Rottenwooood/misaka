"""Card services over the canonical SQLite board.

Markdown is an explicit generated export. It is never read to recover, dispatch,
or alter a card. Attachments remain ordinary material files, not mutable state.
"""

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from misaka.config import home
from misaka.core.platform import repo, tasks, work
from misaka.utils import atomic


def card_path(workspace, task_id):
    return str(Path(workspace) / "cards" / (task_id + ".md"))


def create(
    con,
    workspace,
    title,
    body,
    instance_id,
    *,
    request_id,
    reviewer=None,
    model=None,
    priority=0,
    origin_session=None,
    after_row=None,
    needs=(),
):
    with tasks._write_txn(con):
        owner = work.request(con, request_id)
        if tasks.canonical_workspace(workspace) != owner["workspace"]:
            raise ValueError("Card workspace must match its work request at creation")
        tid = "t_" + secrets.token_hex(3)
        work.update_plan(
            con,
            request_id,
            owner["revision"],
            [
                {
                    "id": tid,
                    "title": title,
                    "body": body,
                    "instance_id": instance_id,
                    "model": model,
                    "priority": priority,
                    "reviewer": reviewer,
                    "needs": list(needs),
                }
            ],
        )
        if origin_session:
            con.execute(
                "UPDATE tasks SET origin_session=? WHERE id=?", (origin_session, tid)
            )
        if after_row is not None:
            after_row(tid)
        return tid


def append_log(con, task_id, author, text):
    tasks.add_event(con, task_id, "note", {"author": author, "text": text})


def read_log(con, task_id):
    result = []
    for row in con.execute(
        "SELECT kind,payload,created_at FROM events WHERE task_id=? ORDER BY id",
        (task_id,),
    ):
        payload = json.loads(row["payload"] or "{}")
        if row["kind"] == "note":
            result.append(f"{payload['author']}: {payload['text']}")
        else:
            result.append(f"{row['kind']}: {json.dumps(payload, ensure_ascii=False)}")
    return result


def export(con, task_id):
    with tasks._write_txn(con):
        row = tasks.get(con, task_id)
        if row is None:
            raise ValueError(f"Card not found: {task_id}")
        fields = {
            key: row[key]
            for key in (
                "id",
                "request_id",
                "instance_id",
                "title",
                "status",
                "generation",
            )
        }
        fields["needs"] = tasks.parent_ids(con, task_id)
        # JSON is valid YAML, and keeps the projection deterministic without another parser.
        text = (
            "---\n"
            + json.dumps(fields, ensure_ascii=False, indent=2)
            + "\n---\n\n"
            + row["body"]
        )
        text += (
            "\n\n## log\n"
            + "\n".join("- " + line for line in read_log(con, task_id))
            + "\n"
        )
        path = card_path(row["workspace"], task_id)
        atomic.write_text(path, text)
        return path


MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def attachment_dir(base_dir, task_id):
    return os.path.join(base_dir, "cards", str(task_id))


def attach(con, workspace, task_id, source):
    """Copy a local regular file into the card's attachment directory
    (``cards/<id>/<name>``, part of the repository) and log it. Returns the repo-relative
    path. Symlinks and files over 50 MiB are rejected."""
    src = Path(source)
    if src.is_symlink() or not src.is_file():
        raise ValueError(f"Not a regular file: {source}")
    if src.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"Attachment exceeds 50 MiB: {source}")
    workspace = tasks.canonical_workspace(workspace)
    target_dir = attachment_dir(workspace, task_id)
    os.makedirs(target_dir, exist_ok=True)
    name, target = src.name, os.path.join(target_dir, src.name)
    stem, dot, ext = src.name.rpartition(".")
    counter = 0
    while os.path.exists(
        target
    ):  # same name, same second: keep counting, never overwrite
        counter += 1
        suffix = f"{int(time.time())}" + (f"-{counter}" if counter > 1 else "")
        name = f"{stem or ext}-{suffix}{dot}{ext if stem else ''}"
        target = os.path.join(target_dir, name)
    import shutil

    shutil.copyfile(src, target)
    append_log(con, task_id, "last-order", f"[attach] cards/{task_id}/{name}")
    return os.path.join("cards", str(task_id), name)


def attach_url(con, task_id, url):
    if not str(url).startswith(("http://", "https://")):
        raise ValueError("Only http(s) URLs can be attached.")
    with tasks._write_txn(con):
        con.execute(
            "INSERT OR IGNORE INTO card_urls(task_id,url) VALUES(?,?)", (task_id, url)
        )
        append_log(con, task_id, "last-order", f"[url] {url}")


def attachment_list(con, base_dir, task_id):
    """The card's inputs as the prompt expects them: the files under ``cards/<id>/`` in the
    directory the card runs in (worktree or project), plus the URL references from the card
    file. Same shape the old staging produced: {"kind", "path"/"source", "name"}."""
    out = []
    root = attachment_dir(base_dir, task_id)
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isfile(full) and not os.path.islink(full):
                out.append({"kind": "file", "path": full, "name": name})
    out.extend(
        {"kind": "url", "source": r[0], "name": r[0]}
        for r in con.execute(
            "SELECT url FROM card_urls WHERE task_id=? ORDER BY url", (task_id,)
        )
    )
    return out


STUCK_SECONDS = 24 * 3600  # a card blocked this long is waiting on nobody in particular
FAILING_AFTER = 2  # consecutive failed attempts before the board says so


def card_signal(row, now=None):
    """What a reader should notice about a card beyond its status: ``failing`` (attempts are
    running out) or ``stuck`` (blocked for a day); None otherwise."""
    if row is None:
        return None
    if int(row["consecutive_failures"] or 0) >= FAILING_AFTER:
        return "failing"
    now = int(time.time()) if now is None else int(now)
    if (
        row["status"] in ("blocked", "triage")
        and row["blocked_at"]
        and now - int(row["blocked_at"]) >= STUCK_SECONDS
    ):
        return "stuck"
    return None


def board(con, workspace):
    result = []
    for row in con.execute(
        "SELECT * FROM card_state WHERE workspace=? ORDER BY created_at,id",
        (tasks.canonical_workspace(workspace),),
    ):
        item = dict(row)
        if signal := card_signal(row):
            item["signal"] = signal
        result.append(item)
    return result


def remove(con, workspace, task_id):
    with tasks._write_txn(con):
        ok, message = tasks.delete_task(con, task_id)
    if not ok:
        return ok, message
    import shutil

    shutil.rmtree(attachment_dir(workspace, task_id), ignore_errors=True)
    Path(card_path(workspace, task_id)).unlink(missing_ok=True)
    return ok, message


GITIGNORE_TEMPLATE = """# MISAKA: derived and cache material. The originals these point at are what git records.
{config_dir}/
.pageindex/
.office-cache/
.office-intent/
downloads/
.DS_Store
__pycache__/
*.pyc
# research source bundles: rebuilt at every settle, hard links of files kept elsewhere
nodes/**/SOURCES.md
nodes/**/sources/
nodes/**/.SOURCES-*.part
final/*-SOURCES.md
final/*-sources/
final/.SOURCES-*.part
"""

PROJECT_TEMPLATE = """# {name}

> 项目简报（PROJECT.md）——所有 agent 开工前读这份；计划、范围、已知缺口变了就改这里。

## 一句话目标

（待填）

## 行文规范

- 一句一行（semantic line breaks）：每个句子独占一行，改动对照（diff）即逐句校记。
- 语料（PDF/EPUB 等）不入库：用 `misaka doc add` 登记，正文里引用 doc id。

## 范围与边界

（待填）

## 已知缺口

（待填）
"""


def _git(folder, *args):
    """Run one git command for :func:`init_project`, reporting a missing or failing git as
    a readable RuntimeError. ``misaka init`` is usually the first command a new user runs,
    and git is a hard prerequisite the installer cannot supply: a subprocess traceback here
    tells them nothing about what to install."""
    try:
        subprocess.run(["git", *args], cwd=folder, check=True)
    except FileNotFoundError:
        raise RuntimeError(
            "misaka init needs git, but no git was found on PATH. Install git first "
            "(macOS: xcode-select --install; Debian/Ubuntu: apt install git), "
            "then run misaka init again."
        ) from None
    except subprocess.CalledProcessError as err:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {folder} (exit {err.returncode})."
        ) from None


def init_project(folder, *, draft_brief=True, git=True):
    """Make a folder a MISAKA project: the skeleton (PROJECT.md, cards/, .gitignore) with its
    database initialized, and -- with ``git``, as ``misaka init`` asks -- a git repository. Only a
    repository this call itself created gets the initial commit; an existing repository is never
    committed to. Starting research passes ``git=False``: git is the user's, never a side effect."""
    folder = tasks.canonical_workspace(folder)
    _refuse_whole_home(folder)
    actions = []
    fresh = git and not repo.enabled(
        folder
    )  # a worktree's .git is a file, not a directory
    if fresh:
        _git(folder, "init", "-q")
        actions.append("git repository created")
    os.makedirs(os.path.join(folder, "cards"), exist_ok=True)
    project_md = os.path.join(folder, "PROJECT.md")
    if draft_brief and not os.path.exists(project_md):
        atomic.write_text(
            project_md, PROJECT_TEMPLATE.format(name=os.path.basename(folder) or folder)
        )
        actions.append("PROJECT.md written")
    ignore = os.path.join(folder, ".gitignore")
    config_dir = home.project_dir(folder)
    if not os.path.exists(ignore) and config_dir is not None:
        # Derived and cache material only. The research bundle patterns are anchored to the
        # research layout, so a person's own ``sources/`` elsewhere stays theirs to commit.
        atomic.write_text(ignore, GITIGNORE_TEMPLATE.format(config_dir=config_dir.name))
        actions.append(".gitignore written")
    con = tasks.connect(os.path.expanduser(_cfg_db()))
    con.close()
    if fresh:
        # Only the skeleton this call wrote, never `git add -A`. A folder that is not
        # already a repository may be full of things that are not project material, and
        # the first commit is the one place nobody reviews: run in a home directory,
        # `add -A` quietly commits ssh keys, .env files and whatever is in Downloads.
        # Existing repositories are not committed to at all, so this is the only path.
        skeleton = [
            name
            for name in ("PROJECT.md", "cards", ".gitignore")
            if os.path.exists(os.path.join(folder, name))
        ]
        if skeleton:
            _git(folder, "add", "--", *skeleton)
        _git(
            folder,
            "-c",
            "user.name=misaka",
            "-c",
            "user.email=misaka@local",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "misaka init: project skeleton",
        )
        actions.append("initial commit")
    return actions or ["already initialized"]


def _refuse_whole_home(folder):
    """A project is a folder of its own. Turning a home directory or a filesystem root into
    one is never what was meant -- it is what pressing Enter at the wizard's folder prompt
    would do from a fresh shell -- and everything downstream (indexing, git, the board's
    workspace identity) then treats every file on the machine as project material."""
    if folder == os.path.dirname(folder):
        raise RuntimeError(
            f"{folder} is a filesystem root; a project has to be a folder of its own."
        )
    try:
        home = str(Path.home().resolve())
    except (OSError, RuntimeError):
        return
    if folder == home:
        raise RuntimeError(
            f"{folder} is your home directory; a project has to be a folder of its own. "
            "Make one and initialize that instead, for example:\n"
            "  mkdir ~/research && cd ~/research && misaka init"
        )


def _cfg_db():
    from misaka.config import CFG

    return CFG["db"]
