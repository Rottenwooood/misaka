"""Card contracts and Sister thinking defaults; all artifacts are local fixtures."""
import json
from types import SimpleNamespace as NS

import pytest
from test_agent_model_runtime import (
    local_models as local_models,  # noqa: PLC0414
)
from test_agent_model_runtime import (
    opened,
    prompt,
)
from test_subagent_native_startup import isolated as isolated  # noqa: PLC0414

from misaka.config import home
from misaka.core.network import worker
from misaka.core.research import planner, report
from misaka.core.subagent.agents import AgentDefinition
from misaka.core.subagent.runtime import SubagentManager


@pytest.mark.parametrize("declaration,name", [
    ("c6_italy_germany.md：①制度编年；②文本锚点", "c6_italy_germany.md"),
    ("findings.md: Sources and counterevidence.", "findings.md"),
    ("research report.md", "research report.md"),
    ("`研究 报告.md`：证据与不确定性", "研究 报告.md"),
])
def test_contract_names_only_the_file(tmp_path, declaration, name):
    task = {"body": f"## deliverable\n{declaration}\n\n## boundaries\nnone\n",
            "output_dir": str(tmp_path)}
    assert worker.contract_deliverable(task) == name
    assert worker.missing_deliverable(task) == name
    (tmp_path / name).write_text("", encoding="utf-8")
    assert worker.missing_deliverable(task) == name
    (tmp_path / name).write_text("Fixture evidence.", encoding="utf-8")
    assert worker.missing_deliverable(task) is None


@pytest.mark.parametrize("declaration", [
    "../outside.md", "/tmp/outside.md", "C:\\outside.md", "sub/report.md", "sub\\report.md",
    ".", "..", "", "\n## boundaries\nnone", "Write a report about the sources.",
    "Write `critique.md` under your card's deliverable directory.", "Write findings.md.",
    "Compare `one.md` and `two.md`.", "report.md\x00", "`one.md` and `two.md`",
    "one.md\n\n## deliverable\ntwo.md",
])
def test_bad_contract_fails_closed(tmp_path, declaration):
    task = {"body": f"## deliverable\n{declaration}\n", "output_dir": str(tmp_path)}
    assert worker.missing_deliverable(task), "A malformed contract must not silently waive the artifact gate"
    with pytest.raises(ValueError, match="deliverable"):
        worker.contract_deliverable(task)


def test_missing_output_and_symlink_escape_fail_closed(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    task = {"body": "## deliverable\nreport.md\n", "output_dir": None}
    assert worker.missing_deliverable(task) == "report.md"
    task["output_dir"] = str(out)
    outside = tmp_path / "outside.md"
    outside.write_text("Not this card's deliverable", encoding="utf-8")
    (out / "report.md").symlink_to(outside)
    assert worker.missing_deliverable(task) == "report.md"
    (out / "report.md").unlink()
    (out / "report.md").mkdir()
    assert worker.missing_deliverable(task) == "report.md"
    (out / "report.md").rmdir()
    (out / "report.md").write_text("Fixture evidence", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").symlink_to(out, target_is_directory=True)
    task.update(workspace=str(workspace), output_dir=str(workspace / "out"))
    assert worker.missing_deliverable(task) == "report.md"  # the output root itself escaped


def test_research_generators_separate_filename_from_requirements(monkeypatch):
    body = planner.task_body({"question": "Question", "rationale": "Rationale",
                              "deliverable": "findings.md：证据与反证"})
    assert "## deliverable\n`findings.md`\n证据与反证\n" in body
    assert worker.contract_deliverable({"body": body}) == "findings.md"
    monkeypatch.setattr(report, "_checkpoint", lambda *args: {"path": "survey.md"})
    monkeypatch.setattr(report, "index_run", lambda *args: {})
    monkeypatch.setattr(report, "_catalog", lambda *args: [])
    review = report.review_body(None, {"question": "Question"},
                                {"path": "draft.md", "id": "draft", "sha256": "fixture"})
    assert "## deliverable\n`critique.md`\n" in review
    assert worker.contract_deliverable({"body": review}) == "critique.md"


def test_plan_rejects_an_ambiguous_deliverable_before_dispatch():
    task = {"local_id": "one", "title": "One", "question": "Question", "rationale": "Rationale",
            "deliverable": "Write a report about the sources.", "assignee": "10032"}
    with pytest.raises(ValueError, match="deliverable"):
        planner._validate_task(task, ["10032"], 1)


def test_a_described_deliverable_is_told_to_lead_with_a_filename_and_names_its_task():
    """2026-09-27: "方法论备忘录：……" was refused as "a directory or absolute path" -- the
    full-width colon -- and the plan's author could not tell what to change."""
    task = {"local_id": "T1", "title": "One", "question": "Question", "rationale": "Rationale",
            "deliverable": "方法论备忘录：反事实推演合法性论证、置信度分级方案", "assignee": "10032"}
    with pytest.raises(ValueError, match=r"^Task T1: .*first line must be the file's name, such as `memo.md`") as caught:
        planner._validate_task(task, ["10032"], 1)
    assert "方法论备忘录" in str(caught.value) and "directory" not in str(caught.value)
    with pytest.raises(ValueError, match="not a directory or absolute path"):
        planner._validate_task({**task, "deliverable": "notes/memo.md"}, ["10032"], 1)


def card_flags(root, monkeypatch, model=None):
    from contextlib import closing

    from misaka.config import identity, sessions
    from misaka.core import wiring
    from misaka.core.platform import tasks, work
    with closing(tasks.connect(str(home.path("db")))) as con:
        if not con.execute("SELECT 1 FROM instances WHERE id='card-sister'").fetchone():
            work.apply(con, [{"op": "template.create", "id": "card-role", "role": "sister", "config": {"model": "fixture-a/shared"}},
                             {"op": "instance.create", "id": "card-sister", "template_id": "card-role", "template_revision": 1}])

    monkeypatch.setattr(worker.task_store, "task_state_dir", lambda _: str(root / "state"))
    monkeypatch.setattr(sessions, "card_session_dir", lambda _: str(root / "cards"))
    monkeypatch.setattr(worker.skill_sandbox, "snapshot_stack", lambda *args: None)
    monkeypatch.setattr(identity, "base_prompt_sources", lambda *args: [])
    monkeypatch.setattr(wiring, "assemble", lambda spec: NS(spec=spec))
    row = {"id": "fixture", "model": model, "body": "Task", "instance_id": "card-sister"}
    return worker.card_session_setup(row, str(root / "workspace"))[0]


@pytest.mark.parametrize("preference", [None, "medium"])
async def test_card_start_uses_defaults_and_native_resume(local_models, monkeypatch, preference):
    root, requests = local_models
    models_path = home.path("models")
    models = json.loads(models_path.read_text(encoding="utf-8"))
    models["providers"]["fixture-a"]["models"][0].update(
        reasoning=True, compat={"forceAdaptiveThinking": True})
    models_path.write_text(json.dumps(models), encoding="utf-8")
    settings_path = home.path("settings")
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["defaultThinkingLevel"] = "high"
    if preference:
        settings["modelThinkingLevels"] = {"fixture-a/shared": preference}
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    flags = card_flags(root, monkeypatch)
    assert "--thinking" not in flags
    async with opened(root, "sisters/10032", "card", flags) as (_, session):
        assert session.thinkingLevel == (preference or "high")
        session.setThinkingLevel("low")
        await prompt(session, "FIXTURE_RESUME")
        session_file = session.sessionFile
    async with opened(root, "sisters/10032", "card", [*flags, "--session", session_file]) as (_, session):
        assert session.thinkingLevel == "low"
    explicit = card_flags(root, monkeypatch, "fixture-a/shared:high")
    async with opened(root, "sisters/10032", "card", explicit) as (_, session):
        assert session.thinkingLevel == "high"
    assert len(requests) == 1


@pytest.mark.parametrize("manager_type,expected", [(SubagentManager, "off")])
async def test_only_board_sister_drops_generic_child_thinking_override(tmp_path, monkeypatch, manager_type, expected):
    manager = object.__new__(manager_type)
    monkeypatch.setattr(manager, "_refresh_task_project_trust", lambda task: None)
    task = NS(id="fixture", metadata_path=tmp_path / "meta.json", transcript=tmp_path / "session.jsonl",
              definition=AgentDefinition(name="fixture", description="Fixture", prompt="Fixture persona", source="fixture"),
              model_provider="fixture", model_id="model", project_trusted=True)
    flags = await manager._child_flags(task)
    assert flags[flags.index("--model") + 1] == "model"
    assert flags[flags.index("--session") + 1] == str(task.transcript)
    if expected is None:
        assert "--thinking" not in flags
    else:
        assert flags[flags.index("--thinking") + 1] == expected
