"""A card that expects deliveries does not run half-connected."""
import os
from contextlib import closing

from tests.work_fixture import BODY, Scenario


def seed(scenario):
    scenario.apply([
        {"op": "template.create", "id": "lo", "role": "last-order",
         "config": {"system": "coordinate"}},
        {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
        {"op": "template.create", "id": "sister", "role": "sister",
         "config": {"system": "work", "tools": ["read"]}},
        {"op": "instance.create", "id": "worker", "template_id": "sister",
         "template_revision": 1},
        {"op": "request.create", "id": "req", "title": "Watch", "owner_id": "lo",
         "workspace": str(scenario.project)},
        {"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [{
            "id": "t_a00001", "title": "One", "instance_id": "worker", "body": BODY,
            "subscriptions": [{"source": "tests", "type": "test.failed"}],
        }]},
    ])


def test_a_card_with_subscriptions_refuses_to_start_without_a_centre(tmp_path, monkeypatch):
    from misaka.core.network import dispatch
    from misaka.core.platform import tasks as db

    monkeypatch.delenv("MISAKA_CENTER_URL", raising=False)
    monkeypatch.delenv("MISAKA_CENTER_TOKEN", raising=False)
    s = Scenario()
    try:
        seed(s)
        with closing(db.connect(s.database)) as con:
            row = db.get(con, "t_a00001")
            generation = int(row["generation"])
            assert db.claim(con, "t_a00001", "lock-1", ttl_seconds=60,
                            generation=generation, pid=os.getpid(),
                            worker_identity="test")
            claimed = db.get(con, "t_a00001")
            assert claimed["status"] == "running"
            assert dispatch._apply_subscriptions(con, claimed, generation, "lock-1") is False
            after = db.get(con, "t_a00001")
            assert after["status"] == "ready"
            kinds = [r[0] for r in con.execute(
                "SELECT kind FROM events WHERE task_id='t_a00001'")]
            assert "subscriptions_unconfigured" in kinds
    finally:
        s.close()
