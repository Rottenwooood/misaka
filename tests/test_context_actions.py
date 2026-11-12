"""Isolation starts a fresh window; a skill candidate loads nothing until approved."""
from contextlib import closing

from misaka.core.context.isolation import HistoryIsolation
from misaka.core.context.skills import SkillCandidate
from tests.work_fixture import Scenario


def test_isolation_archives_the_session_and_records_why(tmp_path):
    from misaka.core.platform import tasks
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order", "config": {"system": "c"}},
            {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
        ])
        connect = lambda: tasks.connect(s.database)
        action = HistoryIsolation(connect)
        started = action.isolate("lo", "after the first report")
        assert started["session_epoch"] == 2
        rows = action.list("lo")
        assert [(row["epoch"], row["label"]) for row in rows] == [(1, "after the first report")]
        again = action.isolate("lo", "second")
        assert again["session_epoch"] == 3
        assert [row["epoch"] for row in action.list("lo")] == [1, 2]
    finally:
        s.close()


def test_a_candidate_is_invisible_until_it_is_approved(tmp_path, monkeypatch):
    from misaka.core.context import skills as module
    from misaka.config import home
    monkeypatch.setattr(home, "path", lambda key: tmp_path / key)

    action = SkillCandidate()
    action.propose("weekly-report", "# Weekly report\n\nRead last week's notes first.",
                   description="Write the weekly report")
    assert [item["status"] for item in action.list()] == ["proposed"]
    assert not (tmp_path / "shared_skills" / "weekly-report").exists()

    approved = action.approve("weekly-report")
    assert approved["status"] == "published"
    body = (tmp_path / "shared_skills" / "weekly-report" / "SKILL.md").read_text()
    assert "status: published" in body
    assert action.list() == []


def test_a_rejected_candidate_leaves_nothing(tmp_path, monkeypatch):
    from misaka.config import home
    monkeypatch.setattr(home, "path", lambda key: tmp_path / key)
    action = SkillCandidate()
    action.propose("throwaway", "# Nope")
    action.reject("throwaway")
    assert action.list() == []
    assert not (tmp_path / "shared_skills").exists()
