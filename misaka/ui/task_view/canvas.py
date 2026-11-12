"""Live, read-only card canvas. Runs inside an ordinary herdr-compatible PTY pane."""

import codecs
import json
import os
import re
import select
import shutil
import signal
import sys
import time
import unicodedata

COLORS = {
    "normal": "38;5;189",
    "dim": "38;5;245",
    "accent": "38;5;204",
    "active": "38;5;151",
    "wait": "38;5;222",
}


def clean(text):
    return "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Cs") else ch
        for ch in str(text)
    )


def clipped(text, limit):
    result, used = [], 0
    for ch in clean(text):
        size = width(ch)
        if used + size > limit:
            break
        result.append(ch)
        used += size
    return "".join(result)


def width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class Screen:
    def __init__(self, cols, rows):
        self.cols, self.rows = max(1, cols), max(1, rows)
        cols, rows = self.cols, self.rows
        self.origin_x = self.origin_y = 0
        self.cells = [[(" ", "normal") for _ in range(cols)] for _ in range(rows)]

    def text(self, x, y, text, color="normal"):
        x, y = x - self.origin_x, y - self.origin_y
        if not 0 <= y < self.rows:
            return
        for ch in clean(text):
            size = width(ch)
            if size == 0:
                # Preserve combining marks attached to a visible character.
                if 0 < x <= self.cols:
                    prev, c = self.cells[y][x - 1]
                    if prev:
                        self.cells[y][x - 1] = (prev + ch, c)
                continue
            if 0 <= x and x + size <= self.cols:
                self.cells[y][x] = (ch, color)
                if size == 2:
                    self.cells[y][x + 1] = ("", color)
            x += size

    def box(self, x, y, w, h, color="dim"):
        self.text(x, y, "┌" + "─" * (w - 2) + "┐", color)
        self.text(x, y + h - 1, "└" + "─" * (w - 2) + "┘", color)
        for row in range(y + 1, y + h - 1):
            self.text(x, row, "│", color)
            self.text(x + w - 1, row, "│", color)

    def ansi(self):
        lines = []
        for row in self.cells:
            current, result = None, []
            for ch, color in row:
                if not ch:
                    continue
                if color != current:
                    result.append("\x1b[" + COLORS[color] + "m")
                    current = color
                result.append(ch)
            lines.append("".join(result) + "\x1b[0m")
        # Cursor-address rows: no newline-induced scrolling at the terminal's last cell.
        return (
            "\x1b[?2026h"
            + "".join(f"\x1b[{i + 1};1H" + line for i, line in enumerate(lines))
            + "\x1b[?2026l"
        )

    def plain(self):
        return "\n".join("".join(ch for ch, _ in row).rstrip() for row in self.cells)


STATE = {
    "ready": "待调度",
    "todo": "待处理",
    "review": "待验收",
    "done": "完成",
    "failed": "失败",
    "stopped": "已停止",
    "blocked": "受阻",
    "held": "暂缓",
    "triage": "待分诊",
    "archived": "已归档",
    "unknown": "未知",
}


class Canvas:
    def __init__(self, snapshot):
        self.x = self.y = 0
        self.selected = None
        self.drag = None
        self.revision = 0
        self.snapshot = None
        self.update(snapshot)

    def update(self, snapshot):
        if snapshot == self.snapshot:
            return False
        self.snapshot = snapshot
        self.revision += 1
        self.nodes, self.groups, self.edges, self.headings = [], [], [], []
        base_y, self.world_cols = 0, 80
        for project in snapshot["projects"]:
            self.headings.append((0, base_y, project["path"], "accent"))
            base_y += 2
            for error in project["errors"]:
                self.headings.append(
                    (0, base_y, "项目读取失败 / 旧快照：" + error, "wait")
                )
                base_y += 1
            by_id = {n["id"]: n for n in project["nodes"]}
            if not project["requests"]:
                self.headings.append(
                    (
                        2,
                        base_y,
                        "尚无卡片 · 与 Last Order 讨论任务后会出现在这里",
                        "dim",
                    )
                )
                base_y += 3
            for index, group in enumerate(project["requests"]):
                ranks = group["ranks"]
                columns = {}
                for tid in group["nodes"]:
                    columns.setdefault(ranks[tid], []).append(tid)
                count_running = sum(
                    by_id[tid]["running"] is True for tid in group["nodes"]
                )
                all_done = bool(group["nodes"]) and all(
                    by_id[tid]["status"] in ("done", "archived")
                    and not by_id[tid]["errors"]
                    for tid in group["nodes"]
                )
                state = "尚未拆分" if not group["nodes"] else "全部完成" if all_done else f"{count_running} 执行中"
                if group["errors"]:
                    state += " / 计划异常"
                self.headings.append(
                    (
                        2,
                        base_y,
                        f"{group['id']} · {group['title']} / LO {group['owner_id']} / v{group['revision']} / {state}",
                        "wait" if group["errors"] else "dim",
                    )
                )
                start = len(self.nodes)
                for rank, tids in sorted(columns.items()):
                    for column, tid in enumerate(tids):
                        node = dict(
                            by_id[tid],
                            key=(project["path"], tid),
                            x=2 + column * 42,
                            y=base_y + 2 + rank * 8,
                        )
                        parents = [by_id.get(p) for p in node["needs"]]
                        node["waiting"] = bool(node["needs"]) and any(
                            p is None or p["status"] != "done" or p["errors"]
                            for p in parents
                        )
                        self.nodes.append(node)
                if group["nodes"]:
                    self.groups.append(start)
                positions = {n["id"]: n for n in self.nodes[start:]}
                for tid in group["nodes"]:
                    for parent in by_id[tid]["needs"]:
                        if parent in positions:
                            self.edges.append((positions[parent], positions[tid]))
                self.world_cols = max(
                    self.world_cols, max(map(len, columns.values()), default=1) * 42 + 4
                )
                base_y += max(ranks.values(), default=0) * 8 + 9
            base_y += 2
        self.world_rows = max(1, base_y)
        if not any(n["key"] == self.selected for n in self.nodes):
            self.selected = self.nodes[0]["key"] if self.nodes else None
        return True

    def selection(self):
        return next((n for n in self.nodes if n["key"] == self.selected), None)

    def hit(self, x, y):
        return next(
            (
                n
                for n in self.nodes
                if n["x"] <= x < n["x"] + 38 and n["y"] <= y < n["y"] + 5
            ),
            None,
        )

    def pan(self, dx, dy):
        self.x = min(max(0, self.world_cols - 10), max(0, self.x + dx))
        self.y = min(max(0, self.world_rows - 3), max(0, self.y + dy))

    def locate(self, node, cols, rows):
        self.selected = node["key"]
        self.x = max(0, node["x"] - max(0, (cols - 38) // 2))
        self.y = max(0, node["y"] - max(0, (rows - 12) // 2))

    def mouse(self, code, x, y, end, cols, rows):
        y -= 3
        if end == "m":
            self.drag = None
            return
        if not (0 <= x < cols and 0 <= y < rows - 6):
            return
        if code & 64:
            self.pan(0, 3 if code & 1 else -3)
        elif code & 32:
            if self.drag:
                px, py = self.drag
                self.pan(px - x, py - y)
                self.drag = (x, y)
        elif code & 3 == 0:
            hit = self.hit(x + self.x, y + self.y)
            self.drag = (x, y) if hit is None else None
            if hit:
                self.selected = hit["key"]

    def key(self, key, cols, rows):
        directions = {
            "h": (-6, 0),
            "l": (6, 0),
            "k": (0, -3),
            "j": (0, 3),
            "\x1b[D": (-6, 0),
            "\x1b[C": (6, 0),
            "\x1b[A": (0, -3),
            "\x1b[B": (0, 3),
        }
        if key in directions:
            self.pan(*directions[key])
        elif key == "g":
            self.x = self.y = 0
        elif self.nodes:
            current = next(
                (i for i, n in enumerate(self.nodes) if n["key"] == self.selected), 0
            )
            target = None
            if key == "\t":
                target = self.nodes[(current + 1) % len(self.nodes)]
            elif key in ("n", "p"):
                ordered = self.groups if key == "n" else list(reversed(self.groups))
                target_index = next(
                    (
                        i
                        for i in ordered
                        if (i > current if key == "n" else i < current)
                    ),
                    ordered[0],
                )
                target = self.nodes[target_index]
            elif key == "a":
                target = next(
                    (
                        self.nodes[(current + i + 1) % len(self.nodes)]
                        for i in range(len(self.nodes))
                        if self.nodes[(current + i + 1) % len(self.nodes)]["running"]
                        is True
                    ),
                    None,
                )
            if target:
                self.locate(target, cols, rows)

    @staticmethod
    def state(node):
        if node["errors"]:
            return "旧快照" if node["stale"] else "读取异常"
        if node["running"]:
            return "执行中"
        if node["status"] in ("ready", "todo") and node["waiting"]:
            return "等依赖"
        return STATE.get(node["status"], node["status"])

    @staticmethod
    def _terms(node):
        """What this card may touch, must not run, and listens for, on one line."""
        def listed(values, cap):
            head = ", ".join(values[:cap])
            return head + (f" +{len(values) - cap}" if len(values) > cap else "")

        scope = json.loads(node.get("fs_scope") or "[]")
        bans = json.loads(node.get("forbidden_commands") or "[]")
        subs = json.loads(node.get("subscriptions") or "[]")
        parts = []
        if scope:
            parts.append("范围 " + listed(scope, 3))
        if bans:
            parts.append("禁令 " + listed(bans, 2))
        if subs:
            parts.append("订阅 " + "、".join(
                f"{item.get('source')}/{item.get('type')}" for item in subs[:2]))
        return " · ".join(parts)

    def render(self, cols, rows):
        s = Screen(cols, rows)
        s.text(0, 0, "MISAKA / 任务画布 / 只读 / 任务请求与卡片依赖", "accent")
        s.text(
            0,
            1,
            "方向键/hjkl 平移 · 拖空白平移 · Tab 选卡 · n/p 换组 · a 执行中 · g 起点 · q 退出",
            "dim",
        )
        warning = self.snapshot["state_error"]
        diagnostics = sum(
            len(p["errors"])
            + sum(bool(n["errors"]) for n in p["nodes"])
            + sum(bool(g["errors"]) for g in p["requests"])
            for p in self.snapshot["projects"]
        )
        s.text(
            0,
            2,
            f"视口 x={self.x} y={self.y} · {cols}x{rows} · v{self.revision} · 异常 {diagnostics}"
            + (" · " + warning if warning else ""),
            "wait" if warning or diagnostics else "dim",
        )
        if cols < 42 or rows < 13:
            s.text(0, 4, "窗口过小，请扩大后查看画布。", "wait")
            return s
        world = Screen(cols, rows - 6)
        world.origin_x, world.origin_y = self.x, self.y
        for source, target in self.edges:
            x1, y1 = source["x"] + 19, source["y"] + 5
            x2, y2 = target["x"] + 19, target["y"]
            mid = (y1 + y2) // 2
            for y in range(max(y1, self.y), min(mid + 1, self.y + world.rows)):
                world.text(x1, y, "│", "dim")
            for x in range(
                max(min(x1, x2), self.x), min(max(x1, x2) + 1, self.x + cols)
            ):
                world.text(x, mid, "─", "dim")
            for y in range(max(mid, self.y), min(y2, self.y + world.rows)):
                world.text(x2, y, "│", "dim")
            world.text(x2, y2 - 1, "▼", "dim")
        for x, y, title, color in self.headings:
            world.text(x, y, title, color)
        for node in self.nodes:
            x, y = node["x"], node["y"]
            color = (
                "accent"
                if node["key"] == self.selected
                else "active"
                if node["running"]
                else "dim"
            )
            world.box(x, y, 38, 5, color)
            indicator = (
                "● 执行中"
                if node["running"]
                else "○ 未执行"
                if node["running"] is False
                else "? 执行未知"
            )
            world.text(x + 2, y + 1, clipped(node["id"] + " " + indicator, 34), color)
            world.text(x + 2, y + 2, clipped(node["title"], 34))
            world.text(
                x + 2,
                y + 3,
                clipped(self.state(node) + " / " + node["instance_id"], 34),
                "wait" if node["errors"] else "dim",
            )
        for row in range(world.rows):
            s.cells[row + 3] = world.cells[row]
        node = self.selection()
        s.text(0, rows - 3, "─" * cols, "dim")
        s.text(
            0,
            rows - 2,
            f"选中 {node['id']} / {node['title']} / {self.state(node)}"
            if node
            else "暂无卡片",
            "accent",
        )
        detail = "每 0.5 秒刷新 · 任务来自当前状态库 · 关闭只退出查看"
        if node:
            detail = (
                "；".join(node["errors"])
                or node["block_reason"]
                or self._terms(node)
                or detail
            )
        s.text(
            0,
            rows - 1,
            detail,
            "wait" if node and (node["errors"]) else "dim",
        )
        return s


def run(reader):
    if os.name != "posix":
        raise SystemExit("交互画布目前需要 POSIX 终端；请用 --json 或 --snapshot。")
    import termios
    import tty

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            "需要交互式终端；静态预览请用 --snapshot COLS ROWS 或 --json。"
        )
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    canvas = Canvas(reader.read())
    next_refresh = time.monotonic() + 0.5
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    pending_since = 0
    old_handler = signal.getsignal(signal.SIGWINCH)
    exit_signals = (signal.SIGTERM, signal.SIGHUP)  # windows-footgun: ok, run() returns above on non-posix
    old_exit_handlers = {sig: signal.getsignal(sig) for sig in exit_signals}

    def terminate(sig, _frame):
        raise SystemExit(128 + sig)

    for sig in exit_signals:
        signal.signal(sig, terminate)
    dirty = True

    def resize(_sig, _frame):
        nonlocal dirty
        dirty = True

    signal.signal(signal.SIGWINCH, resize)
    mouse = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([mM])")
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1002h\x1b[?1006h")
        sys.stdout.flush()
        while True:
            now = time.monotonic()
            if now >= next_refresh:
                dirty = canvas.update(reader.read()) or dirty
                next_refresh = now + 0.5
            cols, rows = shutil.get_terminal_size((100, 35))
            if dirty:
                sys.stdout.write(canvas.render(cols, rows).ansi())
                sys.stdout.flush()
                dirty = False
            if not select.select([fd], [], [], 0.1)[0]:
                if pending and time.monotonic() - pending_since > 0.3:
                    pending = ""  # Ignore a lone Escape or abandoned control sequence.
                continue
            data = os.read(fd, 65536)
            if not data:
                break
            pending += decoder.decode(data)
            pending_since = time.monotonic()
            while pending:
                match = mouse.match(pending)
                if match:
                    code, x, y, end = match.groups()
                    canvas.mouse(int(code), int(x) - 1, int(y) - 1, end, cols, rows)
                    pending = pending[match.end() :]
                elif pending.startswith("\x1b"):
                    if pending.startswith("\x1b[<") or len(pending) < 3:
                        break
                    canvas.key(pending[:3], cols, rows)
                    pending = pending[3:]
                else:
                    key, pending = pending[0], pending[1:]
                    if key in ("q", "\x03", "\x04"):
                        return
                    canvas.key(key, cols, rows)
                dirty = True
    finally:
        try:
            sys.stdout.write(
                "\x1b[?1002l\x1b[?1006l\x1b[?2026l\x1b[?7h\x1b[?25h\x1b[?1049l\x1b[0m"
            )
            sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
            signal.signal(signal.SIGWINCH, old_handler)
            for sig, handler in old_exit_handlers.items():
                signal.signal(sig, handler)
