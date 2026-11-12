"""Operator/automation entry to the same state commands exposed to Last Order."""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from misaka.config import home
from misaka.core.platform import tasks, work


def run(args):
    try:
        if args.state_cmd == "bootstrap":
            with closing(tasks.connect(str(home.path("db")))) as con:
                known = con.execute("SELECT 1 FROM templates WHERE id=?", (args.template,)).fetchone()
                config = {}
                if args.model:
                    config["model"] = args.model
                if args.system:
                    config["system"] = args.system
                operations = []
                if known is None:
                    operations.append({"op": "template.create", "id": args.template,
                                       "role": "last-order", "config": config})
                if con.execute("SELECT 1 FROM instances WHERE id=?", (args.instance,)).fetchone() is None:
                    operations.append({"op": "instance.create", "id": args.instance,
                                       "template_id": args.template, "template_revision": 1})
                result = work.apply(con, operations) if operations else {"created": []}
        elif args.state_cmd == "apply":
            text = (
                sys.stdin.read()
                if args.file == "-"
                else Path(args.file).read_text(encoding="utf-8-sig")
            )
            payload = json.loads(text)
            if not isinstance(payload, dict) or set(payload) != {"operations"}:
                raise ValueError('Input must be {"operations": [...]}')
            with closing(tasks.connect(str(home.path("db")))) as con:
                result = work.apply(con, payload["operations"])
        else:
            path = home.path("db")
            if not path.exists():
                result = {"templates": [], "instances": [], "requests": [], "cards": []}
            else:
                with closing(
                    sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                ) as con:
                    con.row_factory = sqlite3.Row
                    tasks.require_schema(
                        con, "tasks", tasks.TASK_SCHEMA_VERSION, populated=True
                    )
                    con.execute("BEGIN")
                    result = work.state(con)
                    con.rollback()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error, RuntimeError) as error:
        print(f"misaka state: {error}", file=sys.stderr)
        return 1
