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


class PlanCard(Command):
    id: CardId
    title: str | None = None
    body: str | None = Field(None, description="Contract including a testable ## acceptance criteria section.")
    instance_id: str | None = None
    needs: list[CardId] | None = None
    priority: int | None = None
    model: str | None = None
    reviewer: str | None = None
    cancel: bool = False


class SavePlan(Command):
    action: Literal["save"]
    request_id: str
    expected_revision: int | None = Field(None, ge=0, description="Omit when creating a request; required when editing one.")
    workspace: str | None = Field(None, description="Project directory, required for a new request.")
    title: str | None = None
    body: str | None = None
    cards: list[PlanCard] = Field(default_factory=list, description="New cards and patches to unstarted cards. Omitted fields are unchanged.")


class DispatchCard(Command):
    id: CardId
    generation: int = Field(ge=1, description="Generation from the saved plan.")


class StartCards(Command):
    action: Literal["start"]
    cards: list[DispatchCard] = Field(min_length=1, description="Explicit cards to start, with the generations you intend to run.")


class TaskParams(Command):
    command: Annotated[ShowTask | SavePlan | StartCards, Field(discriminator="action")]


class ShowRoster(Command):
    action: Literal["show"]


class CreateTemplate(Command):
    action: Literal["create_template"]
    id: str
    role: Literal["last-order", "sister"]
    config: dict[str, Any]


class ReviseTemplate(Command):
    action: Literal["revise_template"]
    id: str
    expected_revision: int = Field(ge=1)
    config: dict[str, Any]
    parent_revision: int | None = Field(None, ge=1)


class CreateInstance(Command):
    action: Literal["create_instance"]
    id: str
    template_id: str
    template_revision: int | None = Field(None, ge=1)
    config: dict[str, Any] | None = None
    role: Literal["last-order", "sister"] | None = None
    label: str | None = None


class RosterParams(Command):
    command: Annotated[ShowRoster | CreateTemplate | ReviseTemplate | CreateInstance, Field(discriminator="action")]


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


def _data(label, value):
    return {"content": [{"type": "text", "text": untrusted(label, redact(json.dumps(value, ensure_ascii=False)))}],
            "details": {}}


def _roster_config(item):
    config = dict(item["config"])
    local_settings = bool(config.pop("settings", None))
    fields = ("id", "revision", "parent_revision", "role", "label", "template_id",
              "template_revision", "retired")
    return {**{key: item[key] for key in fields if key in item},
            "config": config, "local_settings": local_settings}


def install(collector, runtime, connection, owner_id):
    def actor(con):
        if not owner_id:
            raise ValueError("This tool requires a registered Last Order instance")
        return work.coordinator(con, owner_id)["id"]

    @toolkit.register_tool(
        collector, name="misaka_task", label="Coordinate work",
        description="Read or save your task plans, or explicitly start named ready cards. "
                    "Saving does not start execution. Starting may consume model quota. "
                    "Follow the user's instructions for presenting plans and coordinating with them.",
        parameters=TaskParams, schema=_command_schema,
    )
    async def task(tool_call_id, params, signal, on_update, ctx):
        con = connection()
        command = params.command
        with tasks.write_txn(con):
            owner = actor(con)
            if isinstance(command, ShowTask):
                return _data("work-plan", work.owned_state(con, owner, command.request_id))
            if isinstance(command, SavePlan):
                request = work.save_plan(con, owner, **command.model_dump(exclude_unset=True, exclude={"action"}))
                return _data("work-plan", work.owned_state(con, owner, request["id"]))
            targets = {card.id: card.generation for card in command.cards}
            if len(targets) != len(command.cards):
                raise ValueError("Each start target must appear only once")
            for card_id, generation in targets.items():
                work.dispatch_target(con, owner, card_id, generation)
        receipts = []
        for card_id, generation in targets.items():
            try:
                receipt = await runtime.launch(card_id, owner_id=owner, expected_generation=generation)
            except OSError as error:
                # A lost RPC reply cannot prove the daemon did not start the worker.
                receipt = {"task_id": card_id, "status": "unknown", "error": str(error)}
            except (RuntimeError, ValueError) as error:
                receipt = {"task_id": card_id, "status": "error", "error": str(error)}
            receipts.append(receipt)
        return _data("launch-receipts", receipts)

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
                return _data("roster", {key: [_roster_config(item) for item in items]
                                        for key, items in work.roster(con).items()})
            operation = {CreateTemplate: "template.create", ReviseTemplate: "template.revise",
                         CreateInstance: "instance.create"}[type(command)]
            values = command.model_dump(exclude_none=True, exclude={"action"})
            result, = work.apply(con, [{"op": operation, **values}])
            return _data("roster-change", {key: result[key] for key in
                                         ("id", "revision", "template_id", "template_revision", "role")
                                         if key in result})
