"""Instance-owned orchestration tools for ordinary work."""
import asyncio
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from misaka.core.platform import instances, processes, tasks, toolkit, usage, work
from misaka.core.platform.prompt_guard import untrusted
from misaka.utils.redact import redact

CardId = Annotated[str, Field(pattern=work.CARD_ID_PATTERN)]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShowTask(Command):
    action: Literal["show"]
    request_id: str | None = Field(None, description="Omit for a list of your requests across projects.")
    include_body: bool = Field(
        False, description="Include the request and card Markdown bodies; status queries can omit them.")


class CardSubscription(Command):
    source: str
    type: str
    target: str | None = None


class PlanCard(Command):
    id: CardId
    title: str | None = None
    body: str | None = Field(None, description="The card body, as Markdown.")
    instance: str | None = Field(None, description="The Sister instance this card runs on.")
    completion: Literal["once", "repeat_keep", "repeat_clear"] | None = Field(
        None, description="once; repeat_keep; or repeat_clear. Fixed after creation.")
    priority: int | None = Field(None, description="Scheduling priority; higher runs first.")
    model: str | None = Field(None, description="provider/model override for this card.")
    reviewer: str | None = Field(None, description="Independent reviewer Sister ID; null clears it.")
    needs: list[CardId] | None = None
    fs_scope: list[str] | None = Field(None, description="Paths this card may touch.")
    forbidden_commands: list[str] | None = Field(None, description="Command patterns this card must not run.")
    subscriptions: list[CardSubscription] | None = None


class SavePlan(Command):
    action: Literal["save"]
    request_id: str
    expected_revision: int | None = Field(None, ge=0, description="Omit when creating a request; required when editing one.")
    workspace: str | None = Field(None, description="Project directory, required for a new request.")
    title: str | None = None
    body: str | None = None
    cards: list[PlanCard] = Field(default_factory=list, description="New cards and patches to editable cards; omitted fields are unchanged.")


class ResumeCards(Command):
    action: Literal["resume"]
    cards: list[CardId] = Field(min_length=1, description="Blocked cards to put back on the board.")


class CancelCards(Command):
    action: Literal["cancel"]
    cards: list[CardId] = Field(min_length=1)
    hard: bool = Field(False, description="Delete the cards instead of keeping them as stopped.")


class DeleteRequest(Command):
    action: Literal["delete_request"]
    request_id: str
    force: bool = Field(False, description="Stop and delete even when cards are still active.")


class ReviewCard(Command):
    action: Literal["review"]
    card_id: CardId
    verdict: Literal["accept", "reject"]
    feedback: str | None = Field(None, description="Required when rejecting; it becomes the next turn's input.")


class CardResult(Command):
    action: Literal["result"]
    card_id: CardId | None = None
    request_id: str | None = Field(None, description="Return every card of this request.")
    status: str | None = Field(None, description="With request_id: keep cards in this status.")
    include: list[Literal["summary", "artifacts", "uncertain", "error"]] = Field(
        default_factory=lambda: ["summary", "artifacts", "uncertain", "error"])


class DispatchCard(Command):
    id: CardId
    generation: int | None = Field(None, ge=1, description="Omit to start the card as it is now; pass it to refuse a stale edit.")


class StartCards(Command):
    action: Literal["start"]
    cards: list[DispatchCard] = Field(min_length=1, description="Cards to start.")


class TaskParams(Command):
    command: Annotated[ShowTask | SavePlan | StartCards | ResumeCards | CancelCards | DeleteRequest
                       | ReviewCard | CardResult, Field(discriminator="action")]


class ShowRoster(Command):
    action: Literal["show"]
    kind: Literal["instance", "template"] = Field(
        "instance", description="Query instances or templates.")
    id: str | None = Field(
        None, description="One instance id when kind=instance, or one template id when kind=template; omit for all.")
    detail: bool = Field(
        False, description="Include full configuration; for an instance, the config of the template revision it pins.")


class SessionConfig(Command):
    """What a template node says about assembling a session; every field is optional."""
    system: str | None = Field(None, description="Additional system prompt text.")
    model: str | None = Field(None, description="provider/model.")
    thinking: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    tools: list[str] | None = Field(None, description="Tool names; empty means the role default.")
    skills: list[str] | None = Field(None, description="Skill names to load.")
    settings: dict[str, dict] | None = Field(
        None, description="Only mcpServers and web; model and thinking have their own fields.")
    fs_scope: list[str] | None = Field(
        None, description="Default paths cards of this instance may touch.")
    forbidden_commands: list[str] | None = Field(
        None, description="Default command patterns cards of this instance must not run.")


class CreateTemplate(Command):
    action: Literal["create_template"]
    id: str
    role: Literal["last-order", "sister"]
    config: SessionConfig


class ReviseTemplate(Command):
    action: Literal["revise_template"]
    id: str
    expected_revision: int = Field(ge=1)
    config: SessionConfig


class CreateInstance(Command):
    action: Literal["create_instance"]
    id: str
    template_id: str
    template_revision: int | None = Field(None, ge=1)
    config: SessionConfig | None = Field(
        None,
        description="With template_revision: changes merged over that node, which becomes a new node. "
                    "Without: the full configuration of a new template, and role is required.")
    role: Literal["last-order", "sister"] | None = Field(
        None, description="Only for a new template; a pinned node already has one.")
    label: str | None = None


class SubscriptionItem(Command):
    source: str
    type: str
    enabled: bool | None = Field(None, description="Omit on add to enable; required for set_enabled.")


class Subscriptions(Command):
    action: Literal["subscriptions"]
    id: str
    op: Literal["list", "add", "remove", "set_enabled"]
    items: list[SubscriptionItem] | None = None


class StopInstance(Command):
    action: Literal["stop_instance"]
    id: str
    mode: Literal["hibernate", "kill"]


class BulkDelete(Command):
    action: Literal["bulk_delete"]
    kind: Literal["instance", "template"]
    ids: list[str] = Field(min_length=1)
    revisions: list[int] | None = Field(
        None, description="For kind=template: only these revisions; omit to delete the template.")


class RosterParams(Command):
    command: Annotated[ShowRoster | CreateTemplate | ReviseTemplate | CreateInstance | Subscriptions
                       | StopInstance | BulkDelete, Field(discriminator="action")]


class UsageParams(Command):
    card_id: CardId | None = None
    request_id: str | None = None
    instance_id: str | None = None
    live_only: bool = Field(False, description="Only cards running now.")


def _command_schema(model):
    # Tool argument coercion validates each union arm independently. Inline the
    # local definitions so those arms are valid standalone JSON Schemas.
    schema = model.model_json_schema()
    definitions = schema.pop("$defs")

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if ref := value.get("$ref"):
            return inline(definitions[ref.removeprefix("#/$defs/")])
        return {key: inline(item) for key, item in value.items() if key != "discriminator"}

    return inline(schema)


class ContextParams(Command):
    action: Literal["isolate", "list"]
    instance_id: str | None = None
    label: str | None = None


class SkillParams(Command):
    action: Literal["propose", "list", "approve", "reject"]
    name: str | None = None
    body: str | None = None
    description: str | None = None


def _data(label, value):
    return {"content": [{"type": "text", "text": untrusted(label, redact(json.dumps(value, ensure_ascii=False)))}],
            "details": {}}


def _roster_view(con, kind="instance", target_id=None, *, detail=False):
    """One kind of roster entry: all of it, or the one named by ``target_id``.

    A template query returns each template's latest revision. An instance query
    returns the template revision that instance actually pins, and ``detail`` adds
    that revision's configuration rather than a possibly newer template's.
    """
    state = work.roster(con)
    if kind == "template":
        latest = {}
        for node in state["templates"]:
            if node["id"] not in latest or node["revision"] > latest[node["id"]]["revision"]:
                latest[node["id"]] = node
        templates = []
        for node in latest.values():
            if target_id is not None and node["id"] != target_id:
                continue
            row = {key: node[key] for key in ("id", "revision", "role")}
            if detail:
                row["config"] = node.get("config") or {}
            templates.append(row)
        if target_id is not None and not templates:
            raise ValueError(f"Template not found: {target_id}")
        return {"templates": templates}

    status = {row["id"]: row for row in work.instance_status(con, target_id)}
    instances = []
    for item in state["instances"]:
        if target_id is not None and item["id"] != target_id:
            continue
        row = {key: item[key] for key in ("id", "label", "template_id", "template_revision")
               if item.get(key) is not None}
        row["subscriptions"] = work.instance_specs(item["subscriptions"])
        if detail:
            row["role"] = item["role"]
            row["config"] = item["config"]
        row.update({key: value for key, value in status.get(item["id"], {}).items()
                    if key not in ("pid", "last_active_at") or value is not None})
        if item.get("retired"):
            row["retired"] = True
        instances.append(row)
    if target_id is not None and not instances:
        raise ValueError(f"Instance not found: {target_id}")
    return {"instances": instances}


def install(collector, runtime, connection, owner_id):
    def actor(con):
        if not owner_id:
            raise ValueError("This tool requires a registered Last Order instance")
        return work.coordinator(con, owner_id)["id"]

    def owned_card(con, owner, card_id):
        row = tasks.get(con, card_id)
        if row is not None:
            work.owned_request(con, owner, row["request_id"])
        return row

    async def stop_card(card_id, ctx):
        """True when no process is left, or there was none; False when a pane refused."""
        try:
            receipt = await runtime.stop(card_id, confirmed=True, context=ctx)
        except Exception:  # noqa: BLE001 - no pane owns it; the board is the only place to stop it
            return True
        return bool(receipt.get("stopped") or receipt.get("missing"))

    async def cancel_cards(command, con, ctx):
        owner = actor(con)
        results = []
        for card_id in command.cards:
            try:
                row = owned_card(con, owner, card_id)
            except ValueError as error:
                results.append({"id": card_id, "status": "error", "reason": str(error)})
                continue
            if row is None:
                results.append({"id": card_id, "status": "not_found"})
                continue
            if row["status"] in ("running", "review") and not await stop_card(card_id, ctx):
                results.append({"id": card_id, "status": "error", "reason": "stop_failed"})
                continue
            try:
                if command.hard:
                    work.delete_card(con, card_id, allow_active=True, detach=True)
                    results.append({"id": card_id, "status": "deleted"})
                else:
                    results.append(work.cancel_card(con, card_id))
            except ValueError as error:
                results.append({"id": card_id, "status": "error", "reason": str(error)})
        return _data("cancelled", results)

    async def delete_request(command, con, ctx):
        owner = actor(con)
        try:
            work.owned_request(con, owner, command.request_id)
        except ValueError as error:
            if con.execute("SELECT 1 FROM work_requests WHERE id=?",
                           (command.request_id,)).fetchone() is None:
                return _data("deleted", {"id": command.request_id, "status": "not_found",
                                         "deleted_cards": 0})
            return _data("deleted", {"id": command.request_id, "status": "error",
                                     "reason": str(error), "deleted_cards": 0})
        if command.force:
            for row in con.execute("SELECT id,status FROM tasks WHERE request_id=?",
                                   (command.request_id,)).fetchall():
                if row["status"] in ("running", "review") and not await stop_card(row["id"], ctx):
                    return _data("deleted", {"id": command.request_id, "status": "blocked",
                                             "reason": "stop_failed", "deleted_cards": 0})
        return _data("deleted", work.delete_request(con, command.request_id, force=command.force))

    async def stop_instance(command, con):
        item = work.instance(con, command.id)
        cards = con.execute(
            "SELECT id,status,worker_pid,worker_identity FROM tasks "
            "WHERE instance_id=? AND status IN ('running','review') ORDER BY id",
            (command.id,),
        ).fetchall()
        if command.mode == "hibernate":
            if item["writer_pid"] and processes.identity_is_alive(
                    item["writer_pid"], item["writer_identity"]):
                try:
                    await instances.control(command.id, "pause")
                except Exception:  # noqa: BLE001, S110 - no control endpoint; cards still release
                    pass
            for row in cards:
                tasks.back_to_ready(con, row["id"])
        else:
            seen = set()
            targets = [(item["writer_pid"], item["writer_identity"])]
            targets += [(row["worker_pid"], row["worker_identity"]) for row in cards]
            # The catalog can name a live read-only/older session whose writer pointer was
            # never recorded on the instance row. Kill that process too; otherwise the
            # panel still shows it after the roster says there is no writer.
            from misaka.core import session_catalog
            live_record = session_catalog.live_instance_session(command.id)
            if live_record:
                targets.append((live_record.get("pid"), live_record.get("identity")))
            for pid, identity in targets:
                if pid and pid not in seen and processes.identity_is_alive(pid, identity):
                    seen.add(pid)
                    await asyncio.to_thread(processes.terminate, pid)
            for row in cards:
                tasks.block_task(con, row["id"], "transient", "instance_killed")
            instances.forget_session(con, command.id)
        status = work.instance_status(con, command.id)[0]
        return _data("instance-stop", {
            "id": command.id,
            "session": status["session"],
            "affected_cards": [row["id"] for row in cards],
        })

    def sync_subscriptions(instance_id, before_count, subscriptions):
        from misaka.core.messaging import config as center_config
        from misaka.core.messaging import registry
        from misaka.core.messaging.sync import SubscriptionSync

        client, url = center_config.center(), center_config.inbox_url()
        if client is None or url is None:
            return False
        try:
            registry.ensure_endpoint(client, instance_id, url)
            enabled = [item for item in subscriptions if item["enabled"]]
            SubscriptionSync(client, instance_id).apply(
                instance_id, enabled, prefix="instance")
            stale = [SubscriptionSync.name_for(instance_id, index, "instance")
                     for index in range(len(enabled), before_count)]
            if stale:
                SubscriptionSync(client, instance_id).clear(stale)
            return True
        except Exception:  # noqa: BLE001 - a centre outage leaves the local record authoritative
            return False

    def subscription_view(con, command):
        before = work.instance_specs(work.instance(con, command.id)["subscriptions"])
        if command.op != "list" and not command.items:
            raise ValueError("This subscription operation needs items")
        if command.op == "set_enabled" and any(item.enabled is None for item in command.items or ()):
            raise ValueError("set_enabled needs an explicit enabled flag on every item")
        result = work.instance_subscriptions(
            con, command.id, command.op,
            [item.model_dump() for item in command.items or ()],
        )
        if command.op != "list":
            result["synced"] = sync_subscriptions(
                command.id, len(before), result["subscriptions"])
        return result

    @toolkit.register_tool(
        collector, name="misaka_task", label="Coordinate work",
        description="Read or save your task plans, start named ready cards, resume a blocked card, "
                    "cancel or delete a request, review finished work, or read a card's structured "
                    "result. show omits Markdown bodies unless include_body is true. Saving does not "
                    "start execution. Starting may consume model quota. Follow the user's "
                    "instructions for presenting plans.",
        parameters=TaskParams, schema=_command_schema,
    )
    async def task(tool_call_id, params, signal, on_update, ctx):
        con = connection()
        command = params.command
        if isinstance(command, CancelCards):
            return await cancel_cards(command, con, ctx)
        if isinstance(command, DeleteRequest):
            return await delete_request(command, con, ctx)
        if isinstance(command, ReviewCard):
            owner = actor(con)
            owned_card(con, owner, command.card_id)
            return _data("review", work.review_card(con, command.card_id, command.verdict,
                                                    command.feedback))
        if isinstance(command, CardResult):
            owner = actor(con)
            if (command.card_id is None) == (command.request_id is None):
                raise ValueError("Give card_id or request_id, not both")
            if command.card_id is not None:
                owned_card(con, owner, command.card_id)
                return _data("card-result", [work.card_result(
                    con, command.card_id, command.include)])
            work.owned_request(con, owner, command.request_id)
            rows = con.execute(
                "SELECT id,status FROM tasks WHERE request_id=? ORDER BY id",
                (command.request_id,)).fetchall()
            return _data("card-result", [
                work.card_result(con, row["id"], command.include)
                for row in rows
                if command.status is None or row["status"] == command.status])
        with tasks.write_txn(con):
            owner = actor(con)
            if isinstance(command, ShowTask):
                return _data("work-plan", work.owned_state(
                    con, owner, command.request_id, include_body=command.include_body))
            if isinstance(command, ResumeCards):
                for card_id in command.cards:
                    work.resume_card(con, card_id)
                return _data("work-plan", work.owned_state(con, owner))
            if isinstance(command, SavePlan):
                values = command.model_dump(exclude_unset=True, exclude={"action"})
                values["cards"] = [
                    {("instance_id" if key == "instance" else key): value
                     for key, value in card.items()}
                    for card in values.get("cards") or ()
                ]
                request = work.save_plan(con, owner, **values)
                return _data("work-plan", work.owned_state(con, owner, request["id"]))
            targets = {}
            for card in command.cards:
                if card.id in targets:
                    raise ValueError("Each start target must appear only once")
                targets[card.id] = card.generation
            for card_id, generation in targets.items():
                work.dispatch_target(con, owner, card_id, generation)
        receipts = []
        for card_id, generation in targets.items():
            try:
                if generation is None:
                    generation = work.card_generation(con, card_id)
                receipt = await runtime.launch(card_id, owner_id=owner, expected_generation=generation)
            except OSError as error:
                # A lost RPC reply cannot prove the daemon did not start the worker.
                receipt = {"task_id": card_id, "status": "unknown", "error": str(error)}
            except (RuntimeError, ValueError) as error:
                receipt = {"task_id": card_id, "status": "error", "error": str(error)}
            receipts.append({"id": card_id, "state": receipt.get("status")})
        return _data("launch-receipts", receipts)

    @toolkit.register_tool(
        collector, name="misaka_usage", label="Read usage",
        description="Read token counts, priced cost and wall-clock time for one card, a request, "
                    "an instance or the whole board; tokens separate input, output, cache reads "
                    "and cache writes, and total is their sum. model_calls counts model calls, "
                    "attempts counts runs and failures counts failed runs. live_only lists what "
                    "is running now.",
        parameters=UsageParams,
    )
    async def usage_tool(tool_call_id, params, signal, on_update, ctx):
        con = connection()
        actor(con)
        return _data("usage", usage.report(
            con, card_id=params.card_id, request_id=params.request_id,
            instance_id=params.instance_id, live_only=params.live_only))

    @toolkit.register_tool(
        collector, name="misaka_context", label="Set history aside",
        description="Start a fresh window for an instance and record what was set aside, or list "
                    "the stretches already set aside. The old transcript is kept.",
        parameters=ContextParams,
    )
    async def context_tool(tool_call_id, params, signal, on_update, ctx):
        from misaka.core.context.isolation import HistoryIsolation

        con = connection()
        owner = actor(con)
        target = params.instance_id or owner
        action = HistoryIsolation(lambda: con)
        if params.action == "list":
            return _data("history-isolations", action.list(target))
        return _data("history-isolation", action.isolate(target, params.label))

    @toolkit.register_tool(
        collector, name="misaka_skill", label="Propose a skill",
        description="Propose a reusable method, list proposals, approve one into the shared skills, "
                    "or reject it. A proposal loads nothing until it is approved.",
        parameters=SkillParams,
    )
    async def skill_tool(tool_call_id, params, signal, on_update, ctx):
        from misaka.core.context.skills import SkillCandidate

        actor(connection())
        candidate = SkillCandidate()
        if params.action == "list":
            return _data("skill-candidates", candidate.list())
        if not params.name:
            raise ValueError("A skill action needs a name")
        if params.action == "propose":
            return _data("skill-candidate", candidate.propose(
                params.name, params.body or "", description=params.description or ""))
        if params.action == "approve":
            return _data("skill-candidate", candidate.approve(params.name))
        return _data("skill-candidate", candidate.reject(params.name))

    @toolkit.register_tool(
        collector, name="misaka_roster", label="Manage templates and instances",
        description="Read either instances or templates: kind=instance with id for one instance "
                    "or without id for all; kind=template with id for one template or without id "
                    "for all latest templates. detail=true includes full configuration; an instance "
                    "shows the configuration of the template revision it pins. Create or revise "
                    "templates; register or stop an instance; manage its long-lived subscriptions; "
                    "delete instances or template revisions in bulk. These operations do not launch models.",
        parameters=RosterParams, schema=_command_schema,
    )
    async def roster(tool_call_id, params, signal, on_update, ctx):
        con = connection()
        command = params.command
        actor(con)
        if isinstance(command, ShowRoster):
            return _data("roster", _roster_view(
                con, command.kind, command.id, detail=command.detail))
        if isinstance(command, Subscriptions):
            return _data("instance-subscriptions", subscription_view(con, command))
        if isinstance(command, StopInstance):
            return await stop_instance(command, con)
        if isinstance(command, BulkDelete):
            if command.kind == "instance":
                for instance_id in command.ids:
                    status = work.instance_status(con, instance_id)
                    if status and status[0]["session"] == "live":
                        await stop_instance(StopInstance(
                            action="stop_instance", id=instance_id, mode="hibernate"), con)
            return _data("bulk-delete", work.bulk_delete(
                con, command.kind, command.ids, command.revisions))
        operation = {CreateTemplate: "template.create", ReviseTemplate: "template.revise",
                     CreateInstance: "instance.create"}[type(command)]
        values = command.model_dump(exclude_none=True, exclude={"action"})
        result, = work.apply(con, [{"op": operation, **values}])
        return _data("roster-change", {key: result[key] for key in
                                     ("id", "revision", "template_id", "template_revision", "role",
                                      "config")
                                     if key in result})
