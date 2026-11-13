"""Read-only views bypass agent bootstrap, credential checks and project initialization."""

import json
import os
import sys
from pathlib import Path

from misaka.config import home
from misaka.core.platform.task_snapshot import SnapshotReader


def run(args):
    if args.snapshot and any(size < 1 for size in args.snapshot):
        print("--snapshot 的列数和行数必须为正整数", file=sys.stderr)
        return 2
    projects = [
        str(Path(p).expanduser().resolve()) for p in (args.project or [os.getcwd()])
    ]
    if args.pane:
        from misaka.ui.panel import client
        from misaka.ui.task_view import command

        argv, env = command(projects, request_id=args.request, select=args.select)
        try:
            client.check_protocol(client.request("ping",timeout=2))
            result = client.request(
                "pane.create",
                {"argv": argv, "cwd": projects[0], "title": "Tasks · 只读", "env": env},
            )
        except (OSError, RuntimeError, ConnectionError) as error:
            print(f"无法打开画布面板，请先运行 misaka panel：{error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    reader = SnapshotReader(projects, home.path("db"), request_id=args.request)
    if args.json:
        snapshot = reader.read()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return int(
            snapshot["state_status"] == "error"
            or any(
                p["errors"]
                or any(n["errors"] for n in p["nodes"])
                or any(g["errors"] for g in p["requests"])
                for p in snapshot["projects"]
            )
        )
    from misaka.ui.task_view.canvas import Canvas
    from misaka.ui.task_view.canvas import run as run_canvas

    if args.snapshot:
        print(Canvas(reader.read(), select_card=args.select).render(*args.snapshot).plain())
        return 0
    return run_canvas(reader, select_card=args.select)
