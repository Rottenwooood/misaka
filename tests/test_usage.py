"""Usage reads the board's token ledger and attempt rows, adding no accounting."""
import json
import time
from contextlib import closing
from types import SimpleNamespace

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


def _priced(monkeypatch):
    cost = SimpleNamespace(input=1.0, output=2.0, cacheRead=0.0, cacheWrite=0.0)
    registry = SimpleNamespace(find=lambda provider, model: SimpleNamespace(cost=cost))
    from misaka.core.platform import usage
    monkeypatch.setattr(usage, "_registry", lambda: registry)


def _run(con, card_id, start, end, status="done", failure=None):
    with con:
        con.execute(
            "INSERT INTO task_runs(id,task_id,generation,attempt,phase,assignee,status,"
            "workspace,started_at,completed_at,failure_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"tr_{card_id}_{start}", card_id, 1, 1, "worker", "sister", status,
             "/tmp", start, end, failure))


def _event(con, card_id, payload, created_at=100):
    con.execute(
        "INSERT INTO events(task_id,kind,payload,generation,created_at) VALUES(?,?,?,?,?)",
        (card_id, "budget_usage", json.dumps(payload), 1, created_at))


def test_card_usage_sums_tokens_prices_cost_and_wall_time(monkeypatch):
    from misaka.core.platform import tasks, usage, work
    s = Scenario()
    try:
        _seed(s)
        _priced(monkeypatch)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            _run(con, CARD, 100, 160)
            _event(con, CARD, {"totalTokens": 1500, "provider": "fixture", "model": "local",
                               "usage": {"input": 1000, "output": 500}})
            report = usage.report(con, card_id=CARD)
            assert report["tokens"] == {"in": 1000, "out": 500, "cache_read": 0,
                                        "cache_write": 0, "total": 1500}
            assert report["cost"]["amount"] == 0.002
            assert report["wall_time_s"] == 60
            assert report["model_calls"] == 1 and report["attempts"] == 1
            assert report["model"] == "local"
    finally:
        s.close()


def test_an_unpriced_model_reports_a_null_amount(monkeypatch):
    from misaka.core.platform import tasks, usage, work
    s = Scenario()
    try:
        _seed(s)
        monkeypatch.setattr(usage, "_registry", lambda: None)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            _event(con, CARD, {"totalTokens": 10, "provider": "p", "model": "m",
                               "usage": {"input": 10, "output": 0}})
            assert usage.report(con, card_id=CARD)["cost"]["amount"] is None
    finally:
        s.close()


def test_request_and_instance_reports_sum_each_card(monkeypatch):
    from misaka.core.platform import tasks, usage, work
    s = Scenario()
    try:
        _seed(s)
        _priced(monkeypatch)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [
                {"id": CARD, "title": "One", "instance_id": "worker", "body": BODY},
                {"id": "t_a00002", "title": "Two", "instance_id": "worker", "body": BODY},
            ])
            _event(con, CARD, {"totalTokens": 100, "provider": "fixture", "model": "local",
                               "usage": {"input": 100, "output": 0}})
            _event(con, "t_a00002", {"totalTokens": 50, "provider": "fixture", "model": "local",
                                     "usage": {"input": 50, "output": 0}})
            request = usage.report(con, request_id="req")
            assert request["cards"] == 2
            assert request["tokens"]["total"] == 150
            assert [row["card_id"] for row in request["by_card"]] == [CARD, "t_a00002"]
            instance = usage.report(con, instance_id="worker")
            assert instance["tokens"]["total"] == 150
            board = usage.report(con)
            assert board["total"]["cards"] == 2
            assert board["by_model"][0]["model"] == "local"
    finally:
        s.close()


def test_live_usage_reports_only_running_cards_and_leases():
    from misaka.core.platform import tasks, usage, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            now = int(time.time())
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='running',started_at=? WHERE id=?",
                            (now - 30, CARD))
            _event(con, CARD, {"totalTokens": 70, "provider": "p", "model": "m",
                               "usage": {"input": 70, "output": 0}}, created_at=now)
            con.execute(
                "INSERT INTO budget_reservations(id,task_id,generation,tokens,expires_at,created_at) "
                "VALUES('bl_x',?,1,?,?,?)", (CARD, 30, now + 60, now))
            live = usage.report(con, live_only=True)
            assert len(live) == 1
            assert live[0]["card_id"] == CARD
            assert live[0]["tokens_settled"] == 70
            assert live[0]["tokens_leased"] == 30
    finally:
        s.close()


def test_a_card_that_never_ran_reports_zero():
    from misaka.core.platform import tasks, usage, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            report = usage.report(con, card_id=CARD)
            assert report["tokens"] == {"in": 0, "out": 0, "cache_read": 0,
                                        "cache_write": 0, "total": 0}
            assert report["cost"]["amount"] == 0.0
            assert report["model_calls"] == 0 and report["attempts"] == 0
    finally:
        s.close()


def test_an_unknown_identifier_is_an_error_not_an_empty_report():
    import pytest

    from misaka.core.platform import tasks, usage
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            with pytest.raises(ValueError, match="Card not found: t_abcdef"):
                usage.report(con, card_id="t_abcdef")
            with pytest.raises(ValueError, match="Work request not found: r_missing"):
                usage.report(con, request_id="r_missing")
            with pytest.raises(ValueError, match="Instance not found: ghost"):
                usage.report(con, instance_id="ghost")
    finally:
        s.close()


def test_total_tokens_includes_cache_reads_and_writes(monkeypatch):
    from misaka.core.platform import tasks, usage, work
    s = Scenario()
    try:
        _seed(s)
        _priced(monkeypatch)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            _event(con, CARD, {"totalTokens": 999999, "provider": "fixture", "model": "local",
                               "usage": {"input": 10, "output": 5,
                                         "cacheRead": 100, "cacheWrite": 20}})
            tokens = usage.report(con, card_id=CARD)["tokens"]
            assert tokens == {"in": 10, "out": 5, "cache_read": 100,
                              "cache_write": 20, "total": 135}
    finally:
        s.close()
