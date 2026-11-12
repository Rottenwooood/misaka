"""Instance-owned orchestration tools for ordinary work."""
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from misaka.core.platform import tasks, toolkit, work
from misaka.core.platform.prompt_guard import untrusted
from misaka.utils.redact import redact

CardId = Annotated[str, Field(pattern=work.CARD_ID_PATTERN)]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShowTask(Command):
    action: Literal["show"]
    request_id: str | None = Field(None, description="Omit for a list of your requests across projects.")


class CardSubscription(Command):
    source: str
    type: str
    target: str | None = None


class PlanCard(Command):
    id: CardId
    title: str | None = None
    body: str | None = Field(None, description="The contract; it must have an ## acceptance criteria section.")
    instance: str | None = Field(None, description="The Sister instance this card runs on.")
    needs: list[CardId] | None = None
    fs_scope: list[str] | None = Field(None, description="Paths this card may touch.")
    forbidden_commands: list[str] | None = Field(None, description="Command patterns this card must not run.")
    subscriptions: list[CardSubscription] | None = None
    cancel: bool = False


class SavePlan(Command):
    action: Literal["save"]
    request_id: str
    expected_revision: int | None = Field(None, ge=0, description="Omit when creating a request; required when editing one.")
    workspace: str | None = Field(None, description="Project directory, required for a new request.")
    title: str | None = None
    body: str | None = None
    cards: list[PlanCard] = Field(default_factory=list, description="New cards and patches to unstarted cards; omitted fields are unchanged.")


class ResumeCards(Command):
    action: Literal["resume"]
    cards: list[CardId] = Field(min_length=1, description="Blocked cards to put back on the board.")


class StopCards(Command):
    action: Literal["stop"]
    cards: list[CardId] = Field(min_length=1, description="Cards to stop; a running card's process is terminated.")


class DispatchCard(Command):
    id: CardId
    generation: int | None = Field(None, ge=1, description="Omit to start the card as it is now; pass it to refuse a stale edit.")


class StartCards(Command):
    action: Literal["start"]
    cards: list[DispatchCard] = Field(min_length=1, description="Cards to start.")


class TaskParams(Command):
    command: Annotated[ShowTask | SavePlan | StartCards | ResumeCards | StopCards, Field(discriminator="action")]


class ShowRoster(Command):
    action: Literal["show"]


CONFIG_FIELDS = "system, model, thinking, tools, skills, settings"


class CreateTemplate(Command):
    action: Literal["create_template"]
    id: str
    role: Literal["last-order", "sister"]
    config: dict[str, Any] = Field(description=f"Session configuration; keys: {CONFIG_FIELDS}.")


class ReviseTemplate(Command):
    action: Literal["revise_template"]
    id: str
    expected_revision: int = Field(ge=1)
    config: dict[str, Any] = Field(description=f"The node's full configuration; keys: {CONFIG_FIELDS}.")


class CreateInstance(Command):
    action: Literal["create_instance"]
    id: str
    template_id: str
    template_revision: int | None = Field(None, ge=1)
    config: dict[str, Any] | None = Field(
        None,
        description=f"With template_revision: changes merged over that node, which becomes a new node. "
                    f"Without: the full configuration of a new template, and role is required. Keys: {CONFIG_FIELDS}.")
    role: Literal["last-order", "sister"] | None = Field(
        None, description="Only for a new template; a pinned node already has one.")
    label: str | None = None


class SetSubscriptions(Command):
    action: Literal["set_subscriptions"]
    id: str
    subscriptions: list[CardSubscription]


class DeleteInstance(Command):
    action: Literal["delete_instance"]
    id: str


class DeleteTemplate(Command):
    action: Literal["delete_template"]
    id: str
    revision: int | None = Field(None, ge=1, description="One revision; omit to delete the template.")


class RosterParams(Command):
    command: Annotated[ShowRoster | CreateTemplate | ReviseTemplate | CreateInstance | SetSubscriptions
                       | DeleteInstance | DeleteTemplate, Field(discriminator="action")]


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


def _roster_view(state):
    """Latest node per template and the instances; nothing constant, nothing historical."""
    latest = {}
    for node in state["templates"]:
        if node["id"] not in latest or node["revision"] > latest[node["id"]]["revision"]:
            latest[node["id"]] = node
    templates = []
    for node in latest.values():
        row = {key: node[key] for key in ("id", "revision", "role")}
        if node.get("config"):
            row["config"] = node["config"]
        templates.append(row)
    instances = []
    for item in state["instances"]:
        row = {key: item[key] for key in ("id", "label", "template_id", "template_revision")
               if item.get(key) is not None}
        if item.get("retired"):
            row["retired"] = True
        instances.append(row)
    return {"templates": templates, "instances": instances}


def install(collector, runtime, connection, owner_id):
    def actor(con):
        if not owner_id:
            raise ValueError("This tool requires a registered Last Order instance")
        return work.coordinator(con, owner_id)["id"]

    async def stop_cards(command, con, ctx):
        results = []
        for card_id in command.cards:
            row = tasks.get(con, card_id)
            if row is None:
                raise ValueError(f"Card not found: {card_id}")
            if row["status"] in ("done", "failed", "stopped"):
                results.append({"id": card_id, "state": row["status"]})
                continue
            stopped = False
            try:
                receipt = await runtime.stop(card_id, confirmed=True, context=ctx)
                stopped = bool(receipt.get("stopped"))
            except Exception:  # noqa: BLE001 - no pane owns it; the board is the only place to stop it
                stopped = False
            if not stopped:
                with tasks.write_txn(con):
                    stopped = tasks.mark_stopped(con, card_id)
            results.append({"id": card_id, "state": "stopped" if stopped else row["status"]})
        return _data("stopped", results)

    @toolkit.register_tool(
        collector, name="misaka_task", label="Coordinate work",
        description="Read or save your task plans, start named ready cards, resume a blocked card, "
                    "or stop a card and its process. Saving does not start execution. Starting may "
                    "consume model quota. Follow the user's instructions for presenting plans.",
        parameters=TaskParams, schema=_command_schema,
    )
    async def task(tool_call_id, params, signal, on_update, ctx):
        con = connection()
        command = params.command
        if isinstance(command, StopCards):
            return await stop_cards(command, con, ctx)
        with tasks.write_txn(con):
            owner = actor(con)
            if isinstance(command, ShowTask):
                return _data("work-plan", work.owned_state(con, owner, command.request_id))
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
        description="Read the roster, create or revise templates, or register an instance pinned to a template revision. "
                    "These operations do not launch models. A template revision does not change existing instances.",
        parameters=RosterParams, schema=_command_schema,
    )
    async def roster(tool_call_id, params, signal, on_update, ctx):
        con = connection()
        command = params.command
        with tasks.write_txn(con):
            actor(con)
            if isinstance(command, ShowRoster):
                return _data("roster", _roster_view(work.roster(con)))
            if isinstance(command, DeleteInstance):
                work.delete_instance(con, command.id)
                return _data("roster", _roster_view(work.roster(con)))
            if isinstance(command, DeleteTemplate):
                work.delete_template(con, command.id, command.revision)
                return _data("roster", _roster_view(work.roster(con)))
            if isinstance(command, SetSubscriptions):
                item = work.set_instance_subscriptions(
                    con, command.id, [spec.model_dump() for spec in command.subscriptions])
                return _data("instance-subscriptions", {
                    "id": item["id"],
                    "subscriptions": json.loads(item["subscriptions"] or "[]"),
                })
            operation = {CreateTemplate: "template.create", ReviseTemplate: "template.revise",
                         CreateInstance: "instance.create"}[type(command)]
            values = command.model_dump(exclude_none=True, exclude={"action"})
            result, = work.apply(con, [{"op": operation, **values}])
            return _data("roster-change", {key: result[key] for key in
                                         ("id", "revision", "template_id", "template_revision", "role")
                                         if key in result})
