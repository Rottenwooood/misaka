"""Card bodies are one Markdown document, rendered as written."""

from misaka.ui.task_view.canvas import _markdown_lines


def test_body_markdown_keeps_structure_and_acceptance_wording():
    source = "# 任务\n\n正文 **加粗**。\n\n## 验收\n\n- [ ] 产物存在\n\n```text\nexit 0\n```"
    lines = _markdown_lines(source, 60)
    joined = "\n".join(lines)
    assert "任务" in joined
    assert "加粗" in joined
    assert "验收" in joined
    assert "[ ] 产物存在" in joined
    assert "exit 0" in joined
    assert "##" not in joined
