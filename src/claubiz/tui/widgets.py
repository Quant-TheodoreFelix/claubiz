from urllib.parse import urlsplit

from rich.text import Text
from textual import events
from textual.app import App
from textual.binding import Binding
from textual.geometry import Offset
from textual.message import Message
from textual.widgets import DataTable, Markdown, MarkdownViewer, OptionList, Static

from .theme import CLAY, ERROR, FAINT, MUTED, STATUS_STYLES, SUCCESS, WARNING

MAX_URL_LENGTH = 2048


def safe_web_url(href: str) -> str | None:
    url = href.strip()
    if not url or len(url) > MAX_URL_LENGTH or any(ord(ch) < 32 or ch.isspace() for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    return url


def open_link(app: App, href: str, viewer: MarkdownViewer | None = None) -> None:
    if href.startswith("#") and viewer is not None:
        viewer.document.goto_anchor(href[1:])
        return
    url = safe_web_url(href)
    if url is None:
        app.notify("웹 주소(http, https)가 아닌 링크는 열지 않음", severity="warning")
        return
    app.open_url(url)
    app.notify(url[:90], title="브라우저로 열기", timeout=3)


class SafeMarkdownViewer(MarkdownViewer):
    def __init__(self, markdown: str = "", **kwargs) -> None:
        super().__init__(markdown, open_links=False, **kwargs)

    async def _on_markdown_link_clicked(self, message: Markdown.LinkClicked) -> None:
        message.stop()
        message.prevent_default()
        open_link(self.app, message.href, self)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} MB"


class RunsTable(DataTable):
    BINDINGS = [
        Binding("ctrl+enter", "app.follow_up", "이어서 작업 또는 재개"),
        Binding("c", "app.export_report", "보고서 복사"),
        Binding("delete,backspace", "app.delete_run", "기록 제거"),
    ]


class InputsTable(DataTable):
    BINDINGS = [Binding("delete,backspace", "app.delete_input", "자료 제거")]


class JobsCard(Static):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.targets: list[tuple[int, int, str]] = []

    def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        for start, end, name in self.targets:
            if start <= offset.y < end:
                event.stop()
                self.app.action_goto_ws(name)
                return


class WorkspaceList(OptionList):
    BINDINGS = [Binding("delete,backspace", "app.delete_workspace", "작업 영역 제거")]

    class MenuRequested(Message):
        def __init__(self, index: int, offset: Offset) -> None:
            super().__init__()
            self.index = index
            self.offset = offset

    async def _on_click(self, event: events.Click) -> None:
        if event.button != 3:
            return
        event.prevent_default()
        event.stop()
        index = event.style.meta.get("option")
        if index is not None and not self.disabled:
            self.post_message(self.MenuRequested(index, event.screen_offset))


def status_text(status: str) -> Text:
    icon, label, color = STATUS_STYLES.get(status, ("○", status, MUTED))
    text = Text()
    text.append(icon + " ", style=color)
    text.append(label, style=color if status not in ("pending", "rejected") else FAINT)
    return text


class BudgetMeter(Static):
    def show(self, spent: float, soft: float, hard: float) -> None:
        width = 14
        ratio = 0.0 if hard <= 0 else min(spent / hard, 1.0)
        filled = round(ratio * width)
        color = ERROR if spent >= hard else (WARNING if spent >= soft else SUCCESS)
        text = Text()
        text.append("이번 주기 ", style=MUTED)
        text.append("━" * filled, style=color)
        text.append("━" * (width - filled), style=FAINT)
        text.append(f"  ${spent:,.2f}", style=f"bold {color}")
        text.append(f" / ${hard:,.0f}", style=MUTED)
        self.update(text)


def brand() -> Text:
    text = Text()
    text.append("◆ ", style=f"bold {CLAY}")
    text.append("claubiz", style="bold")
    return text
