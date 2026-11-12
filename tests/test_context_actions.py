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


def test_an_answer_to_a_parked_card_starts_it_again():
    import os
    from misaka.core.network import messages
    from misaka.core.platform import tasks
    from tests.work_fixture import BODY, Scenario

    s = Scenario()
    con = None
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order", "config": {}},
            {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
            {"op": "template.create", "id": "sister", "role": "sister", "config": {}},
            {"op": "instance.create", "id": "worker", "template_id": "sister", "template_revision": 1},
            {"op": "request.create", "id": "req", "title": "R", "owner_id": "lo",
             "workspace": str(s.project)},
            {"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [{
                "id": "t_a00001", "title": "One", "instance_id": "worker", "body": BODY}]},
        ])
        con = tasks.connect(s.database)
        assert tasks.claim(con, "t_a00001", "lock-1", ttl_seconds=60, generation=1,
                           pid=os.getpid(), worker_identity="t")
        assert tasks.block_task(con, "t_a00001", "needs_input", "which archive?",
                                generation=1, claim_lock="lock-1")
        assert tasks.get(con, "t_a00001")["status"] == "blocked"

        target = {"to_addr": "t_a00001", "to_task": "t_a00001", "to_session": None,
                  "generation": 1, "label": "the card"}
        original_board = messages._board_path
        original_send, original_connect = messages.send, messages.connect
        messages._board_path = lambda: s.database
        messages.send = lambda *a, **k: 1
        messages.connect = lambda: type("Con", (), {"close": lambda self: None})()
        try:
            messages.post(target, "the answer", summary="answer", sender="last-order")
        finally:
            messages._board_path = original_board
            messages.send, messages.connect = original_send, original_connect

        assert tasks.get(con, "t_a00001")["status"] == "ready"
    finally:
        if con is not None:
            con.close()
        s.close()
