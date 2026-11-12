"""Last Order’s board tools and durable card notifications."""
import asyncio
import json
import os
import secrets
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from misaka.config import CFG, current_config, sisters
from misaka.config.identity import (
    COORDINATOR_BOARD_STATUS,
    COORDINATOR_RECEIPTS,
)
from misaka.core.network.wiring import coordination
from misaka.core.network.sister_runtime import SisterRuntime, transcript_tail
from misaka.core.platform import toolkit, work
from misaka.core.platform import tasks as db
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.research.tool_policy import DRIVER_OWNED_TOOLS
from misaka.core.wiring import ToolCollector

_CON = None
TaskId = Annotated[str, Field(pattern=work.CARD_ID_PATTERN)]


def _session_id(ctx):
    """This conversation's session id: stamped on the cards it creates, so a resumed
    conversation knows which Sisters are hers."""
    return getattr(getattr(ctx, "sessionManager", None), "sessionId", None) or None


def _session_line(ctx, limit=10):
    """This conversation's session id plus the ids it was forked from (header
    ``parentSession`` chain): a fork mints a new id, but the cards stamped before the
    fork still belong to this line."""
    ids = [i for i in (_session_id(ctx),) if i]
    current = getattr(getattr(ctx, "sessionManager", None), "sessionFile", None)
    if not current:
        return ids
    from misaka.core.session_manager import read_session_header
    for _ in range(limit):
        current = (read_session_header(current) or {}).get("parentSession")
        if not current:
            break
        parent_id = (read_session_header(current) or {}).get("id")
        if parent_id and parent_id not in ids:
            ids.append(parent_id)
    return ids


def _card_log(workspace, task_id, author, text):
    """Record a note in the canonical event log."""
    from misaka.core.platform import cards as card_files
    try:
        card_files.append_log(_con(), task_id, author, text)
    except OSError:
        pass


def _pane_for_card(task_id):
    """The live pane running or showing this card, if any (panel mode only).

    The pane holding the card's claim wins. A pane that is only showing the card's saved
    session is a reader: anything steered into it is swallowed, so it must never be mistaken
    for the attempt (2026-09-18, B14)."""
    from misaka.ui.panel import client as net
    panes = [p for p in net.request("panes.list")["panes"]
             if p.get("card") == task_id and p.get("alive")]
    return next((p for p in panes if p.get("claimed")), panes[0] if panes else None)


def _cfg():
    return current_config()


def _con():
    global _CON
    if _CON is None:
        _CON = db.connect(CFG["db"])
    return _CON


def _sisters():
    return sorted(sisters())


def _workspace(ctx):
    return db.canonical_workspace(getattr(ctx, "cwd", None))


def _text(s):
    return {"content": [{"type": "text", "text": s}], "details": {}}


def _schema(model):
    "Convert a Pydantic model to the JSON schema expected by the harness."
    return model.model_json_schema()


def _task_ids(args):
    """Task IDs named by a board tool, for the one project-ownership check."""
    data = args.model_dump() if isinstance(args, BaseModel) else {}
    out = []
    for key in ("task_id", "parent_id", "child_id"):
        if data.get(key):
            out.append(str(data[key]))
    out.extend(str(task_id) for task_id in (data.get("task_ids") or []))
    return list(dict.fromkeys(out))


def _require_project_tasks(ctx, args):
    workspace = _workspace(ctx)
    try:
        dm_cards = json.loads(os.environ.get("MISAKA_DM_CARD_ALLOWLIST", "[]"))
    except (TypeError, ValueError):
        dm_cards = []
    if not isinstance(dm_cards, list):
        dm_cards = []
    generation = getattr(args, "generation", None)
    for task_id in _task_ids(args):
        row = db.get(_con(), task_id)
        allowed_dm_card = (
            row is not None
            and generation is not None
            and int(row["generation"]) == int(generation)
            and [task_id, int(generation), db.workspace_for(row)] in dm_cards
        )
        if row is None or (db.workspace_for(row) != workspace and not allowed_dm_card):
            raise ValueError(f"Task card not found in this project: {task_id}")


def _require_ordinary_tasks(task_ids):
    if task_ids:
        work.ordinary_cards(_con(), task_ids)


def _register(harn, name, label, description, parameters, snippet=None, guidelines=None):
    """Decorator: register ``fn`` as a harness tool whose raw arguments are parsed into ``parameters``.

    The shared seam does the registering, so no board tool can reach the ``tools``
    array while missing from the prompt inventory. What this file adds on top is its
    own schema conversion and the project-ownership check, which has to run on the
    parsed arguments and before the handler sees them."""
    def before(ctx, args):
        _require_project_tasks(ctx, args)
        if name in DRIVER_OWNED_TOOLS:
            _require_ordinary_tasks(_task_ids(args))

    return toolkit.register_tool(
        harn,
        name=name, label=label, description=description, parameters=parameters,
        snippet=snippet, guidelines=guidelines,
        schema=_schema, before=before,
    )


def _install(harn, runtime, owner_id=None):
    """Register the board tools on ``harn`` (a ToolCollector) against ``runtime``."""

    coordination.install(harn, runtime, _con, owner_id)

    class StrictParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class CardReadParams(StrictParams):
        generation: int | None = Field(
            None, ge=1,
            description="For a contact-session help request, supply its generation with its task ID; required to read that card outside its project.",
        )

    class ReviewConfigParams(StrictParams):
        task_id: TaskId
        reviewer: str | None = Field(
            None, description="Independent reviewer Sister ID; use null to remove review from an unstarted card."
        )

    @_register(
        harn,
        name="misaka_card_request_review", label="Configure independent review",
        description="Set or clear the independent reviewer for a todo or ready task card.",
        snippet="Configure a task card's independent reviewer",
        parameters=ReviewConfigParams)
    async def misaka_card_request_review(tool_call_id, params, signal, on_update, ctx):
        if params.reviewer and params.reviewer not in set(_sisters()):
            return _text(f"Reviewer {params.reviewer} is not in the Sister roster.")
        row = db.get(_con(), params.task_id)
        if row is None:
            return _text(f"Task card not found: {params.task_id}")
        if params.reviewer == row["assignee"]:
            return _text("The reviewer must be different from the assignee.")
        if not db.configure_review(_con(), params.task_id, params.reviewer):
            return _text(f"Task card {params.task_id} is {row['status']}; review can be configured only while todo or ready.")
        reviewer = params.reviewer or "none"
        return _text(f"Task card {params.task_id} reviewer: {reviewer}.")


    class ReviewDecisionParams(StrictParams):
        task_id: TaskId
        reviewer: str = Field(description="Reviewing Sister ID; must match the card's configured reviewer.")
        decision: Literal["approve", "request_changes"]
        feedback: str = Field("", description="Review summary; required when requesting changes.")

    @_register(
        harn,
        name="misaka_card_review", label="Submit independent review",
        description="Approve a reviewed submission or return it to the original Sister with specific requested changes.",
        snippet="Submit an independent task-card review",
        parameters=ReviewDecisionParams)
    async def misaka_card_review(tool_call_id, params, signal, on_update, ctx):
        con = _con()
        row = db.get(con, params.task_id)
        if row is None:
            return _text(f"Task card not found: {params.task_id}")
        if row["reviewer"] != params.reviewer:
            return _text(
                f"Task card reviewer is {row['reviewer'] or 'none'}, not {params.reviewer}."
            )
        if params.decision == "request_changes" and not params.feedback.strip():
            return _text("`request_changes` requires specific feedback.")
        lock = f"review:{params.reviewer}:{secrets.token_hex(4)}"
        if not db.claim_review(
            con, params.task_id, params.reviewer, lock,
            generation=int(row["generation"]), reviewer_identity="board-tool",
        ):
            return _text(f"Task card {params.task_id} is {row['status']} or its review is already claimed.")

        def decide():
            """The decision and everything it drags behind it, off Last Order's loop.

            Both branches mirror the card file and commit it (``platform/repo`` retries an
            index.lock with a ``time.sleep`` backoff), and an approval then indexes the
            submitted artifacts -- which runs ``pdftotext`` at up to 300s per PDF. On the
            loop that is Last Order's whole conversation frozen, DM handling and every
            other tool included, for as long as the biggest artifact takes. The board
            connection is a ``SerializedConnection`` (``check_same_thread=False`` plus a
            re-entrant statement lock), so a worker thread is a place it can be used; the
            compensating release travels with the decision for the same reason it exists.
            """
            try:
                if params.decision == "approve":
                    done = db.approve_review(
                        con, params.task_id, lock, generation=int(row["generation"]),
                        summary=params.feedback,
                    )
                    if done:
                        from misaka.core.network import dispatch
                        dispatch.index_after_review(con, params.task_id, int(row["generation"]))
                else:
                    done = db.request_review_changes(
                        con, params.task_id, lock, params.feedback,
                        generation=int(row["generation"]),
                    )
            except Exception:
                db.release_review(con, params.task_id, lock, generation=int(row["generation"]))
                raise
            return done

        changed = await asyncio.to_thread(decide)
        if not changed:
            return _text("Review ownership expired; no decision was recorded.")
        if params.decision == "approve":              # the card is done; there is nothing to relaunch
            state = db.get(con, params.task_id)["status"]
            return _text(f"Review recorded: approve; the card is done ({state}).")

        return _text(f"Review recorded: changes requested; {params.task_id} has not restarted.")

    class SisterOutputParams(CardReadParams):
        # The default is the non-blocking read. Progress arrives on its own as a
        # <sister-notification>, and Last Order's charter tells her not to poll while
        # work is running, so a bare call is asking "where is this card now" -- it must
        # not quietly hold the turn for thirty seconds waiting for an answer.
        task_id: TaskId = Field(description="Sister task-card ID.")
        block: bool = Field(False, description=(
            "Leave false to inspect the task's current state and return immediately. "
            "Set true only when the user asked to wait for the result, or when you asked its "
            "Sister something you must hear before going on (a finished card woken by your "
            "SendMessage answers and completes again): the call then holds until the task "
            "reaches an accepted, failed, or stopped state."
        ))
        timeout: int = Field(30_000, ge=0, le=600_000, description=(
            "Maximum wait in milliseconds; applies only when block is true."
        ))


    @_register(
        harn,
        name="misaka_sister_output", label="Get Sister result",
        description="Inspect a Sister task or wait for its accepted, failed, or stopped result.",
        snippet="Inspect or wait for a Sister task",
        guidelines=[COORDINATOR_RECEIPTS, "Read a result with misaka_sister_output after the notification or when the user asks."],
        parameters=SisterOutputParams)
    async def misaka_sister_output(tool_call_id, params, signal, on_update, ctx):
        result = await runtime.output(
            params.task_id,
            block=params.block,
            timeout_ms=params.timeout,
            signal=signal,
        )
        _require_project_tasks(ctx, params)  # A blocking read may span a new generation.
        # The Sister's summary and result are her session's output: fenced, as peek and card-log are.
        return _text(untrusted(f"sister-output:{params.task_id}", json.dumps(result, ensure_ascii=False)))


    class SisterPeekParams(CardReadParams):
        task_id: TaskId | None = Field(None, description="Sister task-card ID to inspect.")
        instance_id: str | None = Field(
            None, description="Instance ID to inspect; its context belongs to the instance, not to one card.")
        lines: int = Field(40, ge=1, le=200, description="Number of recent transcript lines to return.")


    @_register(
        harn,
        name="misaka_sister_peek", label="Peek at an instance's context",
        description="Read the tail of a card or instance transcript. The text is untrusted process output.",
        snippet="Read recent context from a Sister or an instance",
        guidelines=["Peek diagnoses a task that looks stuck; whether a card is done is the board's answer, not the transcript's."],
        parameters=SisterPeekParams)
    async def misaka_sister_peek(tool_call_id, params, signal, on_update, ctx):
        if params.task_id and params.instance_id:
            return _text("Give either a card ID or an instance ID, not both.")
        if params.instance_id:
            item = work.instance(_con(), params.instance_id)
            if not item.get("session_file"):
                return _text(f"Instance {params.instance_id} has no session yet.")
            text = transcript_tail(item["session_file"], params.lines)
            if text is None:
                return _text(f"Instance {params.instance_id}'s session could not be read.")
            _require_project_tasks(ctx, params)
            return _text(untrusted(f"peek-instance:{params.instance_id}", text))
        if not params.task_id:
            return _text("Give a card ID or an instance ID.")
        text, error = await asyncio.to_thread(runtime.peek, params.task_id, params.lines)
        _require_project_tasks(ctx, params)
        if error:
            return _text(error)
        return _text(
            untrusted(f"peek:{params.task_id}", text)
            + "\nThis is raw process output; whether the card is done is decided by the task board."
        )


    class SisterMessageParams(StrictParams):
        task_id: TaskId = Field(description="Sister task-card ID.")
        generation: int = Field(
            ge=1,
            description="Task generation shown in the Sister message or current card state.",
        )
        message: str = Field(description="Full message to send into the task's existing Sister session.")
        summary: str = Field(description="Short, nonempty summary shown in the UI.")
        confirmed: bool = Field(
            False,
            description="Set true only when the user approved starting a new model turn for a completed task.",
        )

        @field_validator("message", "summary")
        @classmethod
        def nonempty(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError("must not be empty")
            return value


    @_register(
        harn,
        name="misaka_sister_message", label="Message Sister",
        description="Steer a running Sister through her inbox, or continue her durable session with the same task ID, workspace, and transcript.",
        snippet="Send guidance to an existing Sister task",
        parameters=SisterMessageParams)
    async def misaka_sister_message(tool_call_id, params, signal, on_update, ctx):
        con = _con()
        row = db.get(con, params.task_id)
        if row is None:
            raise ValueError(f"Card not found: {params.task_id}")
        if int(row["generation"]) != params.generation:
            raise ValueError(
                f"Card {params.task_id} moved to generation {row['generation']}; "
                f"the reply for generation {params.generation} is stale."
            )
        if row["status"] == "review":
            return _text(
                f"Task card {params.task_id} is under review; wait for the review before messaging its Sister."
            )

        help_reply = (
            row["status"] in {"blocked", "triage"}
            and row["block_kind"] == "needs_input"
        )
        result = await runtime.message(
            params.task_id,
            params.message,
            summary=params.summary,
            confirmed=params.confirmed or help_reply,
            expected_generation=params.generation,
            context=ctx,
        )
        row = db.get(_con(), params.task_id)
        if row is not None:
            _card_log(row["workspace"], params.task_id, "last-order",
                      f"[{result.get('mode') or 'message'}] {params.message}")
        return _text(json.dumps(result, ensure_ascii=False))


    class CardTodosParams(CardReadParams):
        task_id: TaskId = Field(description="Card whose to-do list to show.")


    @_register(
        harn,
        name="misaka_card_todos", label="View a card's to-do list",
        description="Show a Sister card's nested to-do items, owners, statuses, and blocker notes as she keeps them.",
        snippet="Check a card's to-do list",
        parameters=CardTodosParams)
    async def misaka_card_todos(tool_call_id, params, signal, on_update, ctx):
        from misaka.core.network import todo
        text = todo.render(_con(), params.task_id)
        _require_project_tasks(ctx, params)
        return _text(text)


    class SisterResumeParams(StrictParams):
        task_id: TaskId = Field(description="Card whose saved Sister session to reopen.")


    @_register(
        harn,
        name="misaka_sister_resume", label="Reopen Sister session",
        description="Show a finished, failed, stopped, or blocked card's saved Sister session in a pane, as a reader: no model turn, no claim, nothing typed there reaches her. Continue her afterwards with misaka_sister_message, which takes over that same pane.",
        snippet="Show a finished Sister task's saved session",
        guidelines=["After a resumed conversation, bring back only the cards listed as yours in the <resume-briefing>; a card that is already open in a pane is not reopened."],
        parameters=SisterResumeParams)
    async def misaka_sister_resume(tool_call_id, params, signal, on_update, ctx):
        if not os.environ.get("MISAKA_NET_PANE"):
            return _text(f"No panel, so there is no pane to reopen card {params.task_id} in. To continue her, "
                         "send the next instruction with misaka_sister_message (confirmed=true); it resumes her session.")
        live = await asyncio.to_thread(_pane_for_card, params.task_id)
        if live:
            return _text(f"Card {params.task_id} is already open in pane {live['id']}.")
        from misaka.ui.panel import client as net
        row = db.get(_con(), params.task_id)
        if row is not None and not row["session_file"]:
            from misaka.config import sessions as session_roots
            from misaka.core.session_manager import find_most_recent_session
            if find_most_recent_session(session_roots.card_session_dir(row)) is None:
                return _text(f"Card {params.task_id} never ran: it was stopped before its first turn, so "
                             "there is no session to show. misaka_sister_message (confirmed=true) starts "
                             "its first attempt with your note; misaka_card_requeue puts it back in the "
                             "queue for misaka_task with action=start.")
        out = await asyncio.to_thread(
            net.request, "pane.open_card_session",
            {"task_id": params.task_id, "place": {"grid": os.environ["MISAKA_NET_PANE"]}})
        return _text(f"Card {params.task_id}'s saved session is in pane {out['pane_id']}, beside this one, "
                     "as a reader. To continue her, send the next instruction with misaka_sister_message "
                     "(confirmed=true); it takes that pane over.")


    def _tab_panes():
        """This conversation's pane and the panes of the tab it sits in."""
        from misaka.ui.panel import client as net
        me = os.environ["MISAKA_NET_PANE"]
        return me, net.request("panes.list", {"tab_of": me})["panes"]

    def _pane_kind(pane, me):
        """``(what it is, verdict)``; the verdict is what misaka_pane_close does with it."""
        if pane["id"] == me:
            return "this conversation", "self"
        if pane.get("card"):
            card = f"card {pane['card']} ({pane.get('status') or '?'})"
            if pane.get("claimed", True):          # a daemon that does not say is treated as running
                return f"running {card}", "running"
            if pane.get("alive"):
                return f"showing {card}; its attempt does not run here", "leftover"
            return f"{card}; its program has exited", "leftover"
        foreground = (pane.get("foreground") or {}).get("name") or (pane.get("argv") or ["?"])[0]
        state = "running" if pane.get("alive") else "exited"
        return f"{os.path.basename(str(foreground))} ({state})", "ask"

    class PaneListParams(StrictParams):
        pass

    @_register(
        harn,
        name="misaka_pane_list", label="List this tab's panes",
        description="List the panes in this conversation's tab of the panel -- the cards you started, the "
                    "sessions you reopened, what finished cards left behind, shells -- and which of them "
                    "misaka_pane_close may close.",
        snippet="List the panes in this conversation's tab",
        parameters=PaneListParams)
    async def misaka_pane_list(tool_call_id, params, signal, on_update, ctx):
        if not os.environ.get("MISAKA_NET_PANE"):
            return _text("No panel, so this conversation has no tab and no panes.")
        me, panes = await asyncio.to_thread(_tab_panes)
        closes = {"self": "no", "running": "no: stop the card with misaka_sister_stop",
                  "leftover": "yes", "ask": "only when the user asks"}
        lines = []
        for pane in panes:
            what, verdict = _pane_kind(pane, me)
            lines.append(f"{pane['id']}  {pane.get('title') or ''}  -- {what}; close: {closes[verdict]}")
        return _text("\n".join(lines) or "No panes in this tab.")

    class PaneCloseParams(StrictParams):
        pane_ids: list[str] = Field(min_length=1, description="Pane ids from misaka_pane_list (p3, ...), not card ids.")
        confirmed: bool = Field(False, description=(
            "True only when the user asked to close a pane that is not a card's -- a shell or an agent they "
            "started. A finished card's pane and a reader need no confirmation."))

    @_register(
        harn,
        name="misaka_pane_close", label="Close panes in this tab",
        description="Close panes in this conversation's tab. What a finished card left behind and a session "
                    "reopened as a reader may be closed at any time; a pane running a card never (that is "
                    "misaka_sister_stop); any other pane only when the user asks.",
        snippet="Close leftover panes in this conversation's tab",
        guidelines=[("Close what finished cards left in your tab once you have read their results; list the "
                     "tab with misaka_pane_list first, since a pane id is not a card id.")],
        parameters=PaneCloseParams)
    async def misaka_pane_close(tool_call_id, params, signal, on_update, ctx):
        if not os.environ.get("MISAKA_NET_PANE"):
            return _text("No panel, so this conversation has no panes to close.")
        from misaka.ui.panel import client as net
        me, panes = await asyncio.to_thread(_tab_panes)
        by_id = {pane["id"]: pane for pane in panes}
        lines = []
        for pane_id in dict.fromkeys(params.pane_ids):
            pane = by_id.get(pane_id)
            if pane is None:
                lines.append(f"{pane_id}: not in this conversation's tab; left alone.")
                continue
            what, verdict = _pane_kind(pane, me)
            if verdict == "self":
                lines.append(f"{pane_id}: this conversation's own pane; left alone.")
            elif verdict == "running":
                lines.append(f"{pane_id}: {what}; stop the card with misaka_sister_stop instead.")
            elif verdict == "ask" and not params.confirmed:
                lines.append(f"{pane_id}: {what} is not a card's pane; close it only when the user asks "
                             "(confirmed=true).")
            else:
                await asyncio.to_thread(net.request, "pane.close", {"id": pane_id})
                lines.append(f"{pane_id}: closed ({what}).")
        return _text("\n".join(lines))






    class SisterStopParams(StrictParams):
        task_id: TaskId = Field(description="Sister task-card ID.")
        confirmed: bool = Field(description="True only when the user explicitly requested the stop.")


    @_register(
        harn,
        name="misaka_sister_stop", label="Stop Sister task",
        description="Stop an active or waiting Sister task after explicit user confirmation.",
        snippet="Stop a running Sister task",
        parameters=SisterStopParams)
    async def misaka_sister_stop(tool_call_id, params, signal, on_update, ctx):
        result = await runtime.stop(
            params.task_id,
            confirmed=params.confirmed,
            context=ctx,
        )
        return _text(json.dumps(result, ensure_ascii=False))


    class CardCommentsParams(CardReadParams):
        task_id: TaskId = Field(description="Task-card ID whose comments should be listed")
        after_id: int = Field(0, ge=0, description="Only return log entries whose index is greater than this value (each returned line starts with its index)")
        limit: int = Field(50, ge=1, le=200, description="Maximum comments to return")

    @_register(
        harn,
        name="misaka_card_comments", label="View comments",
        description="List persistent comments attached to a task card.",
        snippet="View a task card's comment history",
        parameters=CardCommentsParams)
    async def misaka_card_comments(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        from misaka.core.platform import cards as card_files
        try:
            lines = card_files.read_log(_con(), params.task_id)
        except OSError:
            lines = []
        _require_project_tasks(ctx, params)
        entries = [(index, line) for index, line in enumerate(lines, 1) if index > params.after_id][-params.limit:]
        log = "\n".join(f"{index}: {line}" for index, line in entries)
        if not log:
            return _text("(no log entries)")
        return _text(untrusted(f"card-log:{params.task_id}", log))


    class CardAttachParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID that will receive the attachment.")
        path: str = Field(description="Path to a local regular file; Misaka copies it into managed attachment storage.")

    @_register(
        harn,
        name="misaka_card_attach", label="Attach local file",
        description="Copy a local regular file into managed task storage; symlinks and files over 50 MiB are rejected.",
        snippet="Attach a local file to a task card",
        parameters=CardAttachParams)
    async def misaka_card_attach(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        from misaka.core.platform import cards as card_files
        rel = await asyncio.to_thread(card_files.attach, _con(), row["workspace"], params.task_id, params.path)
        return _text(f"Attached {rel} to card {params.task_id} (a file in the project repository).")


    class CardAttachUrlParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID that will receive the link.")
        url: str = Field(description="HTTP or HTTPS reference URL.")

    @_register(
        harn,
        name="misaka_card_attach_url", label="Attach reference URL",
        description="Record an HTTP or HTTPS URL as task input; the URL is provided at execution time and is not downloaded now.",
        snippet="Attach a reference URL to a task card",
        parameters=CardAttachUrlParams)
    async def misaka_card_attach_url(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        from misaka.core.platform import cards as card_files
        card_files.attach_url(_con(), params.task_id, params.url)
        return _text(f"Attached reference URL to card {params.task_id}.")


    class CardAttachmentsParams(CardReadParams):
        task_id: TaskId = Field(description="Task-card ID whose attachments should be listed.")

    @_register(
        harn,
        name="misaka_card_attachments", label="View attachments",
        description="List local-file attachments and URL references for a task card.",
        snippet="View a task card's attachments",
        parameters=CardAttachmentsParams)
    async def misaka_card_attachments(tool_call_id, params, signal, on_update, ctx):
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Task card not found: {params.task_id}")
        from misaka.core.platform import cards as card_files
        items = card_files.attachment_list(_con(), row["workspace"], params.task_id)
        _require_project_tasks(ctx, params)
        return _text("\n".join(
            f"[{item['kind']}] {item['name']} — {item.get('path') or item.get('source')}"
            for item in items
        ) or "(No attachments.)")


    class CardUnblockParams(StrictParams):
        task_id: TaskId = Field(description="Blocked or triage task-card ID to reopen.")

    @_register(
        harn,
        name="misaka_card_unblock", label="Unblock task card",
        description="Return a blocked or triage card to todo or ready without starting it.",
        snippet="Unblock a task card",
        parameters=CardUnblockParams)
    async def misaka_card_unblock(tool_call_id, params, signal, on_update, ctx):
        if not db.unblock_task(_con(), params.task_id):
            row = db.get(_con(), params.task_id)
            raise ValueError(
                f"Task card not found: {params.task_id}" if row is None
                else f"Task card {params.task_id} is {row['status']}, not blocked or in triage."
            )
        row = db.get(_con(), params.task_id)
        return _text(f"Card {params.task_id} unblocked and returned to {row['status']}; work has not started.")


    class CardRequeueParams(StrictParams):
        task_id: TaskId = Field(description="Stopped or failed task-card ID to put back in the queue.")
        confirmed: bool = Field(
            description="True only when the user asked for this card to be opened for another attempt.")

    @_register(
        harn,
        name="misaka_card_requeue", label="Requeue task card",
        description="Put a stopped or failed card back in the queue as a fresh attempt, the way the board "
                    "reopens one itself. Research cards are not requeued here: their run is resumed by the "
                    "user with /research resume.",
        snippet="Requeue a stopped or failed card",
        guidelines=[COORDINATOR_BOARD_STATUS + " Requeue a stopped or failed card with misaka_card_requeue, and "
                    "ask the user first. Requeueing only puts the card back in the queue; misaka_task with action=start is what "
                    "starts it."],
        parameters=CardRequeueParams)
    async def misaka_card_requeue(tool_call_id, params, signal, on_update, ctx):
        con = _con()
        row = db.get(con, params.task_id)
        if row is None:
            raise ValueError(f"Card not found: {params.task_id}")
        if row["status"] not in {"stopped", "failed"}:
            raise ValueError(
                f"Card {params.task_id} is {row['status']}; only a stopped or failed card is requeued. "
                "Reopen a blocked one with misaka_card_unblock and continue a finished one with "
                "misaka_sister_message."
            )
        if not params.confirmed:
            raise ValueError(
                f"Requeueing card {params.task_id} opens a new attempt on it. Ask the user, then call "
                "again with confirmed=true."
            )
        if not db.reopen_task(con, params.task_id, expected_generation=row["generation"]):
            raise ValueError(
                f"Card {params.task_id} changed underneath the requeue; read it again before retrying."
            )
        row = db.get(con, params.task_id)
        # Nothing polls for ready cards: every attempt is started by somebody asking for it.
        waiting = ("Use misaka_task action=start when it should run." if row["status"] == "ready"
                   else "It waits at todo until the cards it depends on finish, then misaka_task with action=start "
                        "starts it.")
        return _text(f"Card {params.task_id} is back at {row['status']} as attempt "
                     f"{row['generation']}, and is not running yet. {waiting}")


    class CardDeleteParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID to delete.")
        confirmed: bool = Field(
            description="True only when the user explicitly requested this irreversible deletion; related events and budget records are also removed.")

    @_register(
        harn,
        name="misaka_card_delete", label='Delete card',
        description="Permanently delete a stopped or inactive task card after explicit user confirmation.",
        snippet="Permanently delete a task card",
        guidelines=["Deletion is irreversible. Never call misaka_card_delete without an explicit user request to delete the card."],
        parameters=CardDeleteParams)
    async def misaka_card_delete(tool_call_id, params, signal, on_update, ctx):
        if not params.confirmed:
            raise ValueError("Task-card deletion is irreversible and requires explicit user confirmation.")
        row = db.get(_con(), params.task_id)
        if row is None:
            raise ValueError(f"Card not found: {params.task_id}")
        from misaka.core.platform import cards as card_files
        ok, msg = card_files.remove(_con(), row["workspace"], params.task_id)
        if not ok:
            raise ValueError(msg)
        return _text(msg)

    class SisterViewParams(StrictParams):
        sister: str = Field(description="Sister ID from the registered Sister roster.")

    @_register(
        harn,
        name="misaka_sister_view", label="View Sister summary",
        description="Read a Sister's ID, summary, and the first 200 characters of her introduction before assigning work.",
        snippet="View a Sister's routing summary and introduction excerpt",
        guidelines=["When assignment is uncertain, inspect relevant Sister summaries instead of guessing from an ID."],
        parameters=SisterViewParams)
    async def misaka_sister_view(tool_call_id, params, signal, on_update, ctx):
        from misaka.core.network import roster as roster_mod
        sid = params.sister.strip()
        if sid not in set(_sisters()):
            return _text(f"Sister {sid} is not in the roster ({', '.join(_sisters())}).")
        root = _cfg()["profiles_root"]
        desc, body = roster_mod.describe(sid, root=root)
        profile = roster_mod.coordinator_profile({"id": sid, "description": desc, "profile": body})
        return _text(untrusted("sister-profile", json.dumps(profile, ensure_ascii=False)))


class NetworkPart:
    """Last Order's board: the coordination tools, the Sister runtime behind them, and what a session hears at its start."""

    # How often a live coordinator session looks at the notification outbox, so a card
    # is announced within seconds of settling rather than at Last Order's next turn.
    PENDING_POLL_SECONDS = 5.0

    def __init__(self, *, receive_notifications=True, owner_id=None):
        self.session = None
        self.commands = []
        self.receive_notifications = receive_notifications
        self._pending_watch = None
        self.runtime = SisterRuntime(None, _con)
        collector = ToolCollector()
        _install(collector, self.runtime, owner_id)
        self.tools = collector.tools

    def attach(self, session):
        self.session = session
        self.runtime.session = session

    async def session_start(self, event, ctx):
        if not self.receive_notifications:
            return  # branch/headless owners keep the tools but do not consume the root's inbox
        await self._resume_briefing(event, ctx)
        await self._settle_orphans()
        asyncio.ensure_future(self._collect_pending(ctx))
        self._pending_watch = asyncio.ensure_future(self._watch_pending(ctx))

    async def before_agent_start(self, event, ctx):
        if self.receive_notifications:
            asyncio.ensure_future(self._collect_pending(ctx))

    async def session_shutdown(self, event, ctx):
        if self._pending_watch is not None:
            self._pending_watch.cancel()
            self._pending_watch = None
        await self.runtime.close()

    async def _watch_pending(self, ctx):
        """Announce a card the moment it settles, not at Last Order's next turn.

        A card in a daemon pane is driven by its own card shell; nothing in this process
        sees it finish. Its terminal event lands in the durable outbox, and until this
        loop the only readers were session start and ``before_agent_start`` -- so a
        coordinator who said "wait for the notification" and then waited was never told.
        The durable outbox leases and ACKs delivery; no coordinator-side process
        supervisor is involved. The runtime connection is used on this loop only.
        """
        workspace = _workspace(ctx)
        while True:
            await asyncio.sleep(self.PENDING_POLL_SECONDS)
            try:
                self.runtime.deliver_pending(workspace)
            except Exception:  # noqa: BLE001, S110 - a failed pass is retried on the next tick; the outbox keeps the event
                pass

    async def _resume_briefing(self, event, ctx):
        """A resumed conversation is told which cards it created and where they stand, so Last
        Order brings the right Sisters back herself (misaka_sister_resume) instead of guessing
        from the transcript. Nothing is said when the conversation has no cards. A fork is
        the same living line twice over: its cards come via _session_line, and the in-place
        fork restart itself needs no briefing (nothing was forgotten)."""
        if (event or {}).get("reason") in ("reload", "fork"):
            return
        mine = _session_line(ctx)
        if not mine:
            return
        rows = _con().execute(
            "SELECT id,status,assignee,title FROM card_state WHERE origin_session IN "
            f"({','.join('?' * len(mine))}) AND workspace=? ORDER BY created_at",
            [*mine, _workspace(ctx)],
        ).fetchall()
        if not rows:
            return
        open_cards = set()
        if os.environ.get("MISAKA_NET_PANE"):
            try:
                from misaka.ui.panel import client as net
                open_cards = {p["card"] for p in net.request("panes.list")["panes"]
                              if p.get("card") and p.get("alive")}
            except Exception:  # noqa: BLE001, S110 - the daemon may be gone; the briefing still lists the cards
                pass
        lines = [f"  {r['id']}  {r['status']:<10} {r['assignee']:<8} {r['title'][:50]}"
                 + ("  (open in a pane)" if r["id"] in open_cards else "") for r in rows]
        self.session.moments.send_message(
            {"customType": "resume-briefing",
             "content": "<resume-briefing>\nCards created in this conversation:\n"
                        + "\n".join(lines)
                        + "\nReopen a closed one in its own tab with misaka_sister_resume; steer it with "
                          "misaka_sister_message (a finished card restarts only on the user's nod).\n"
                          "</resume-briefing>",
             "display": True, "details": {"cards": [r["id"] for r in rows]}},
            {"triggerTurn": False})

    async def _settle_orphans(self):
        """kill -9 leaves a running card with a live-looking claim and nobody driving it
        (the card drives itself now; the daemon only hosts panes). Settle those whenever a
        coordinator session starts: dispatch.reconcile checks leases and process identity."""
        try:
            from misaka.core.network import dispatch
            await asyncio.to_thread(dispatch.reconcile, _con(), _cfg())
        except Exception:  # noqa: BLE001, S110 - reconciliation is a safety net at startup, never a blocker
            pass

    async def _collect_pending(self, ctx):
        """Deliver terminal-card notifications queued while no session was running, then hint about cards awaiting review."""
        con = _con()
        workspace = _workspace(ctx)
        self.runtime.deliver_pending(workspace)
        reviewing = [r["id"] for r in db.by_status(con, "review", workspace=workspace)]
        if reviewing:
            self.session.moments.send_message(
                {"customType": "board-hint", "display": True,
                 "content": f"{len(reviewing)} task cards await independent review "
                            f"({', '.join(reviewing[:5])}{'…' if len(reviewing) > 5 else ''}).",
                 "details": {"reviewing": reviewing}},
                {"deliverAs": "followUp", "triggerTurn": False},
            )


SESSION_KINDS = {"foreground", "dm"}
ROLES = {"last_order"}


def part(spec):
    notifications = spec.receive_notifications
    return NetworkPart(receive_notifications=spec.receive_messages if notifications is None else notifications,
                       owner_id=spec.instance_id)
