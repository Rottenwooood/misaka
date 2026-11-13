"""Card governance: cancel, request deletion, review, and reading declared results."""
import time
from contextlib import closing

from tests.work_fixture import BODY, Scenario

CARD = "t_a00001"


def _seed(s):
    s.apply([
        {"op": "template.create", "id": "lo", "role": "last-order", "config": {}},
        {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
        {"op": "template.create", "id": "sister", "role": "sister", "config": {}},
        {"op": "instance.create", "id": "worker", "template_id": "sister", "template_revision": 1},
        {"op": "request.create", "id": "req", "title": "R", "owner_id": "lo",
         "workspace": str(s.project)},
    ])


def test_cancel_keeps_the_card_and_a_second_cancel_is_still_clean():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='running',completed_at=NULL WHERE id=?", (CARD,))
            work.cancel_card(con, CARD)
            assert tasks.get(con, CARD)["status"] == "stopped"
            work.cancel_card(con, CARD)
            assert tasks.get(con, CARD)["status"] == "stopped"
    finally:
        s.close()


def test_hard_cancel_removes_the_card_and_its_rows():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            work.delete_card(con, CARD, allow_active=True)
            assert tasks.get(con, CARD) is None
            assert con.execute("SELECT count(*) FROM events WHERE task_id=?", (CARD,)).fetchone()[0] == 0
    finally:
        s.close()


def test_deleting_a_request_with_active_cards_needs_force():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            refused = work.delete_request(con, "req")
            assert refused["status"] == "blocked" and refused["reason"] == "has_active_cards"
            forced = work.delete_request(con, "req", force=True)
            assert forced == {"id": "req", "status": "deleted", "deleted_cards": 1}
            assert tasks.get(con, CARD) is None
    finally:
        s.close()


def test_review_accepts_done_and_rejects_back_to_ready():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='done',completed_at=? WHERE id=?",
                            (int(time.time()), CARD))
            accepted = work.review_card(con, CARD, "accept")
            assert accepted["status"] == "done"
            rejected = work.review_card(con, CARD, "reject", "rework the evidence")
            assert rejected == {"id": CARD, "status": "ready", "generation": 2}
            row = tasks.get(con, CARD)
            assert row["review_feedback"] == "rework the evidence"
    finally:
        s.close()


def test_review_accepts_a_blocked_card_as_finished():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            tasks.block_task(con, CARD, "needs_input", "waiting")
            assert work.review_card(con, CARD, "accept")["status"] == "done"
            assert tasks.get(con, CARD)["status"] == "done"
    finally:
        s.close()


def test_a_declared_result_lists_artifacts_with_a_digest():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        artifact = s.project / "report.txt"
        artifact.write_text("result")
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='done',completed_at=? WHERE id=?",
                            (int(time.time()), CARD))
                tasks.add_event(con, CARD, "submitted", {
                    "summary": "done", "artifacts": ["report.txt"],
                    "artifact_digests": {"report.txt": "abc"}, "uncertain": ["caveat"]},
                    generation=1)
            result = work.card_result(con, CARD)
            assert result["summary"] == "done"
            assert result["uncertain"] == ["caveat"]
            assert result["artifacts"] == [{"path": "report.txt", "bytes": 6,
                                            "digest": "abc", "created_at": result["artifacts"][0]["created_at"]}]
    finally:
        s.close()


def test_a_message_to_a_blocked_card_wakes_it(monkeypatch):
    from misaka.core.network import messages
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            tasks.block_task(con, CARD, "needs_input", "waiting")
            monkeypatch.setattr(messages, "_board_path", lambda: s.database)
            messages._wake_blocked_card(CARD)
            assert tasks.get(con, CARD)["status"] == "ready"
    finally:
        s.close()


def test_posting_to_a_card_asks_the_waker(monkeypatch):
    from misaka.core.network import messages

    class Connection:
        def close(self):
            pass

    woken = []
    monkeypatch.setattr(messages, "connect", lambda: Connection())
    monkeypatch.setattr(messages, "send", lambda *a, **k: "m-1")
    monkeypatch.setattr(messages, "_wake_blocked_card", woken.append)
    target = {"to_addr": "worker", "to_task": CARD, "generation": 1, "to_session": None}
    assert messages.post(target, "go on", summary="go on", sender="lo") == "m-1"
    assert woken == [CARD]


def test_cancelling_a_running_card_closes_its_lease_and_attempt():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            with tasks.write_txn(con):
                con.execute(
                    "UPDATE tasks SET status='running',claim_lock='lock',claim_expires=?,"
                    "worker_pid=123,current_run_id='tr_probe' WHERE id=?",
                    (int(time.time()) + 60, CARD))
                con.execute(
                    "INSERT INTO task_runs(id,task_id,generation,attempt,phase,assignee,status,"
                    "claim_lock,pid,workspace,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("tr_probe", CARD, 1, 1, "worker", "sister", "running", "lock", 123,
                     str(s.project), int(time.time())))
            work.cancel_card(con, CARD)
            row = tasks.get(con, CARD)
            assert row["status"] == "stopped"
            assert row["claim_lock"] is None and row["worker_pid"] is None
            assert con.execute("SELECT status FROM task_runs WHERE id='tr_probe'").fetchone()[0] == "stopped"
    finally:
        s.close()


def test_hard_cancel_detaches_a_parent_and_promotes_its_child():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [
                {"id": CARD, "title": "Parent", "instance_id": "worker", "body": BODY},
                {"id": "t_a00002", "title": "Child", "instance_id": "worker", "body": BODY,
                 "needs": [CARD]},
            ])
            assert tasks.get(con, "t_a00002")["status"] == "todo"
            work.delete_card(con, CARD, allow_active=True, detach=True)
            assert tasks.get(con, CARD) is None
            assert tasks.get(con, "t_a00002")["status"] == "ready"
    finally:
        s.close()


def test_rejecting_a_child_of_done_work_returns_it_to_ready():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [
                {"id": CARD, "title": "Parent", "instance_id": "worker", "body": BODY},
                {"id": "t_a00002", "title": "Child", "instance_id": "worker", "body": BODY,
                 "needs": [CARD]},
            ])
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='done',completed_at=? WHERE id=?",
                            (int(time.time()), CARD))
                con.execute("UPDATE tasks SET status='done',completed_at=? WHERE id='t_a00002'",
                            (int(time.time()),))
            result = work.review_card(con, "t_a00002", "reject", "rework")
            assert result["status"] == "ready"
            assert tasks.dependency_state(con, "t_a00002")[0] == "ready"
    finally:
        s.close()


def test_an_empty_include_returns_only_the_result_envelope():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='done',completed_at=? WHERE id=?",
                            (int(time.time()), CARD))
            result = work.card_result(con, CARD, [])
            assert set(result) == {"id", "status", "finished_at"}
    finally:
        s.close()
