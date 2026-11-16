"""Completion modes: one-shot, recurring with context, recurring with a fresh instance."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from misaka.core.platform import tasks, work
from tests.work_fixture import Scenario


def _seed(s: Scenario) -> None:
    s.apply([{
        "op": "request.create", "id": "req-repeat", "title": "repeat",
        "owner_id": "lo-1", "workspace": str(s.project),
    }])
    s.apply([{
        "op": "plan.update", "request_id": "req-repeat", "expected_revision": 0,
        "cards": [
            {"id": "t_aa0001", "title": "keep", "body": "ctx", "instance_id": "i-1",
             "completion": "repeat_keep"},
            {"id": "t_ab0001", "title": "fresh", "body": "ctx", "instance_id": "i-2",
             "completion": "repeat_clear"},
            {"id": "t_ac0001", "title": "once", "body": "ctx", "instance_id": "i-3"},
        ],
    }])


def test_repeat_keep_reuses_the_instance_and_clear_gets_a_fresh_one():
    s = Scenario()
    try:
        s.seed()
        _seed(s)
        con = tasks.connect(str(s.database))
        try:
            for card_id in ("t_aa0001", "t_ab0001", "t_ac0001"):
                assert tasks.submit_task(con, card_id)

            keep = tasks.get(con, "t_aa0001")
            assert (keep["status"], keep["completion"], keep["completion_count"]) == (
                "ready", "repeat_keep", 1)
            assert work.instance(con, "i-1")["retired"] == 0

            fresh = tasks.get(con, "t_ab0001")
            assert (fresh["status"], fresh["completion"], fresh["completion_count"]) == (
                "ready", "repeat_clear", 1)
            assert work.instance(con, "i-2")["retired"] == 1

            once = tasks.get(con, "t_ac0001")
            assert (once["status"], once["completion"], once["completion_count"]) == (
                "done", "once", 0)

            assert tasks.submit_task(con, "t_aa0001")
            assert tasks.get(con, "t_aa0001")["completion_count"] == 2

            started = work.dispatch_target(con, "lo-1", "t_ab0001", None)
            assert started["instance_id"] != "i-2"
            assert work.instance(con, started["instance_id"])["session_file"] is None
        finally:
            con.close()
    finally:
        s.close()


def test_a_repeat_clear_card_requires_a_fresh_instance():
    s = Scenario()
    try:
        s.seed()
        s.execute("UPDATE instances SET session_file='already-used' WHERE id='i-1'")
        s.apply([{
            "op": "request.create", "id": "req-clear", "title": "clear",
            "owner_id": "lo-1", "workspace": str(s.project),
        }])
        out = s.apply([{
            "op": "plan.update", "request_id": "req-clear", "expected_revision": 0,
            "cards": [{"id": "t_ad0001", "title": "bad", "body": "x",
                       "instance_id": "i-1", "completion": "repeat_clear"}],
        }], code=1)
        assert "fresh instance" in (out.stderr + out.stdout)
    finally:
        s.close()
