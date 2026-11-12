"""A fresh home can be given its first Last Order without hand-written JSON."""
from contextlib import closing

from tests.work_fixture import Scenario


def test_bootstrap_creates_a_template_and_an_instance_once():
    s = Scenario()
    try:
        from misaka.core.platform import tasks
        s.cli("state", "bootstrap", "--model", "test/model")
        s.cli("state", "bootstrap", "--model", "test/model")
        with closing(tasks.connect(s.database)) as con:
            templates = con.execute("SELECT id,revision,role FROM templates").fetchall()
            instances = con.execute("SELECT id,template_id FROM instances").fetchall()
            config = con.execute("SELECT config_json FROM templates").fetchone()[0]
        assert [tuple(row) for row in templates] == [("last-order", 1, "last-order")]
        assert [tuple(row) for row in instances] == [("lo", "last-order")]
        assert "test/model" in config
    finally:
        s.close()
