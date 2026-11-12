"""A finished card's history is marked, once, with a label."""
from contextlib import closing

from tests.work_fixture import BODY, Scenario


def seed(scenario):
    scenario.apply([
        {"op": "template.create", "id": "lo", "role": "last-order", "config": {"system": "c"}},
        {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
        {"op": "template.create", "id": "sister", "role": "sister", "config": {"system": "w"}},
        {"op": "instance.create", "id": "worker", "template_id": "sister", "template_revision": 1},
        {"op": "request.create", "id": "req", "title": "R", "owner_id": "lo",
         "workspace": str(scenario.project)},
        {"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [{
            "id": "t_a00001", "title": "Write the report", "instance_id": "worker", "body": BODY}]},
    ])


def test_tagging_records_the_card_and_instance(tmp_path):
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        seed(s)
        with closing(tasks.connect(s.database)) as con:
            first = work.tag_experience(con, "t_a00001")
            assert first == {"card_id": "t_a00001", "instance_id": "worker",
                             "label": "Write the report"}
            work.tag_experience(con, "t_a00001", "renamed")
            rows = con.execute("SELECT card_id,instance_id,label FROM experience_tags").fetchall()
        assert [tuple(row) for row in rows] == [("t_a00001", "worker", "renamed")]
    finally:
        s.close()
