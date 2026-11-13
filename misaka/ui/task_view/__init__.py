"""Task-view process identity and the single launch contract used by CLI and Panel."""

import sys

from misaka.config import home


def is_task_view(argv):
    words = list(argv or ())
    return any(
        words[i : i + 4] == ["-m", "misaka", "view", "tasks"]
        for i in range(len(words) - 3)
    )


def command(projects, request_id=None, select=None):
    argv = [sys.executable, "-m", "misaka", "view", "tasks"]
    for project in projects:
        argv.extend(["--project", project])
    if request_id:
        argv.extend(["--request", request_id])
    if select:
        argv.extend(["--select", select])
    return argv, {"MISAKA_HOME": str(home.home())}
