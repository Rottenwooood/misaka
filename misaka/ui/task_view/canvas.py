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
    "good": "38;5;151",
    "bad": "38;5;203",
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
    "ready": "待处理",
    "todo": "等依赖",
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


def _split_body(body):
    text = str(body or "")
    marker = "## acceptance criteria"
    index = text.lower().find(marker)
    if index < 0:
        return text.strip(), ""
    return text[:index].strip(), text[index + len(marker):].strip()


def _wrap(text, limit):
    rows, current, used = [], "", 0
    for ch in clean(text).replace("\r", ""):
        if ch == "\n":
            rows.append(current.rstrip())
            current, used = "", 0
            continue
        size = width(ch)
        if used + size > limit and current:
            rows.append(current.rstrip())
            current, used = "", 0
        current += ch
        used += size
    if current.strip():
        rows.append(current.rstrip())
    return rows or [""]


class Canvas:
    def __init__(self, snapshot, request_id=None, select_card=None):
        self.x = self.y = 0
        self.selected = select_card
        self.select_card = select_card
        self.request_id = request_id
        self.show_inspector = True
        self.panned = False
        self.offset_x = self.offset_y = 0
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
                if self.request_id is not None and group["id"] != self.request_id:
                    continue
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
                            x=2 + column * 34,
                            y=base_y + 2 + rank * 6,
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
                    self.world_cols, max(map(len, columns.values()), default=1) * 34 + 4
                )
                base_y += max(ranks.values(), default=0) * 6 + 7
            base_y += 2
        self.world_rows = max(1, base_y)
        if self.select_card is not None:
            chosen = next((n for n in self.nodes if n["id"] == self.select_card), None)
            if chosen is not None:
                self.selected = chosen["key"]
            self.select_card = None
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
        self.panned = True
        self.x = min(max(0, self.world_cols - 10), max(0, self.x + dx))
        self.y = min(max(0, self.world_rows - 3), max(0, self.y + dy))

    def locate(self, node, cols, rows):
        self.selected = node["key"]
        self.panned = True
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
            hit = self.hit(x + self.x - self.offset_x, y + self.y - self.offset_y)
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
        elif key == "g" or key == "f":
            self.panned = False
            self.x = self.y = 0
        elif key == "i":
            self.show_inspector = not self.show_inspector
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
        groups = self.snapshot["projects"]
        selected = self.selection()
        request = None
        for project in groups:
            for group in project["requests"]:
                if selected and selected.get("request_id") == group["id"]:
                    request = group
                    break
            if request:
                break
        title = request["title"] if request else "全部请求"
        request_id = request["id"] if request else ""
        s.text(0, 0, "MISAKA / 任务视图 / " + clean(title)
               + (f"  ·  {request_id}" if request_id else ""), "accent")
        if request:
            nodes = request["nodes"]
            counts = {name: 0 for name in ("running", "done", "ready", "blocked", "failed")}
            for tid in nodes:
                status = next((n["status"] for n in self.nodes if n["id"] == tid), "")
                if status == "running":
                    counts["running"] += 1
                elif status in ("done", "archived"):
                    counts["done"] += 1
                elif status in ("blocked", "triage"):
                    counts["blocked"] += 1
                elif status == "failed":
                    counts["failed"] += 1
                else:
                    counts["ready"] += 1
            s.text(0, 1, f"{len(nodes)} 卡 · {counts['done']} 完成 · {counts['running']} 执行中 · "
                         f"{counts['ready']} 待处理 · {counts['blocked']} 受阻 · LO {request['owner_id']} · v{request['revision']}", "dim")
        else:
            s.text(0, 1, "选择一个请求查看依赖图", "dim")
        warning = self.snapshot.get("state_error")
        s.text(0, 2, ("状态库暂不可用；显示旧快照" if warning else
                      "Tab 选卡 · n/p 换组 · hjkl 平移 · i 详情 · f 适配 · q 关闭"),
               "wait" if warning else "dim")
        if cols < 46 or rows < 14:
            s.text(0, 4, "窗口过小，请扩大后查看任务视图。", "wait")
            return s
        inspector_w = 33 if cols >= 100 and self.show_inspector else 0
        graph_w = cols - (inspector_w + 1 if inspector_w else 0)
        graph_h = rows - 6 - (9 if not inspector_w else 0)
        self.offset_x = max(0, (graph_w - (self.world_cols - 2)) // 2)
        self.offset_y = max(0, (graph_h - (self.world_rows - 2)) // 2)
        world = Screen(graph_w, graph_h)
        world.origin_x, world.origin_y = self.x, self.y
        for source, target in self.edges:
            x1, y1 = source["x"] + 15 + self.offset_x, source["y"] + 5 + self.offset_y
            x2, y2 = target["x"] + 15 + self.offset_x, target["y"] + self.offset_y
            mid = (y1 + y2) // 2
            for y in range(max(y1, self.y), min(mid + 1, self.y + world.rows)):
                world.text(x1, y, "│", "dim")
            for x in range(max(min(x1, x2), self.x), min(max(x1, x2) + 1, self.x + graph_w)):
                world.text(x, mid, "─", "dim")
            for y in range(max(mid, self.y), min(y2, self.y + world.rows)):
                world.text(x2, y, "│", "dim")
            world.text(x2, y2 - 1, "▼", "dim")
        for x, y, text_value, color in self.headings:
            world.text(x, y, clipped(text_value, graph_w - x), color)
        for node in self.nodes:
            x, y = node["x"] + self.offset_x, node["y"] + self.offset_y
            if x >= self.x + graph_w or y >= self.y + graph_h:
                continue
            label = "● 执行中" if node["running"] else self.state(node)
            color = (
                "accent" if node["key"] == self.selected
                else "bad" if node["status"] in ("blocked", "triage", "failed")
                else "active" if node["running"]
                else "good" if node["status"] in ("done", "archived")
                else "wait" if node["status"] in ("ready", "todo")
                else "dim"
            )
            world.box(x, y, 30, 5, color)
            for fill_y in range(y + 1, y + 4):
                world.text(x + 1, fill_y, " " * 28)
            world.text(x + 1, y + 1, clipped(node["id"] + "  " + label, 28), color)
            world.text(x + 1, y + 2, clipped(node["title"], 28))
            world.text(x + 1, y + 3, clipped(node["instance_id"], 28), "dim")
        for row in range(world.rows):
            s.cells[row + 3][:graph_w] = world.cells[row][:graph_w]
        node = self.selection()
        detail_x = 0
        detail_y = rows - 10
        detail_w = cols
        detail_h = 8
        if inspector_w:
            detail_x = graph_w + 1
            detail_y = 3
            detail_w = inspector_w
            detail_h = rows - 5
        self._inspector(s, node, detail_x, detail_y, detail_w, detail_h)
        s.text(0, rows - 2, "─" * cols, "dim")
        s.text(
            0,
            rows - 2,
            f"选中 {node['id']} / {node['title']} / {self.state(node)}"
            if node
            else "暂无卡片",
            "accent",
        )
        position = f" · 视口 x={self.x} y={self.y}" if self.x or self.y else ""
        s.text(0, rows - 1, "只读 · 每 0.5 秒刷新 · 任务来自当前状态库 · q 退出" + position, "dim")
        return s

    @staticmethod
    def _inspector(s, node, x, y, w, h):
        if w < 12 or h < 5:
            return
        s.box(x, y, w, h, "dim")
        if node is None:
            s.text(x + 2, y + 2, "未选择卡片", "dim")
            return
        s.text(x + 2, y + 1, clipped(node["id"], w - 4), "dim")
        s.text(x + 2, y + 2, clipped(node["title"], w - 4), "normal")
        s.text(x + 2, y + 3, Canvas.state(node) + " / " + node["instance_id"], "accent")
        line = y + 5
        for label, value in (
            ("依赖", ", ".join(node.get("needs") or []) or "无"),
            ("阻塞", node.get("block_reason") or "无"),
            ("生成", str(node.get("generation") or 1)),
        ):
            s.text(x + 2, line, label, "dim")
            s.text(x + 8, line, clipped(str(value), max(1, w - 10)), "normal")
            line += 1
        terms = Canvas._terms(node)
        if terms:
            for part in _wrap(terms, max(8, w - 4))[:3]:
                s.text(x + 2, line, part, "dim")
                line += 1
        line += 1
        body = node.get("body") or ""
        contract, acceptance = _split_body(body)
        s.text(x + 2, line, "契约", "dim")
        line += 1
        for part in _wrap(contract, max(8, w - 4))[:4]:
            s.text(x + 2, line, part, "normal")
            line += 1
        if line < y + h - 4:
            s.text(x + 2, line, "验收", "dim")
            line += 1
            for part in _wrap(acceptance, max(8, w - 4))[:4]:
                if line >= y + h - 1:
                    break
                s.text(x + 2, line, part, "normal")
                line += 1
        s.text(x + 2, y + h - 1, "i 收起详情", "dim")

def run(reader, select_card=None):
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
    canvas = Canvas(reader.read(), select_card=select_card)
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
                    if key in ("q", "\x1b", " ", "\x03", "\x04"):
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
