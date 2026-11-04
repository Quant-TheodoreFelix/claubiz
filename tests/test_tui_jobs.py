import asyncio
import json

from rich.cells import cell_len
from test_tui import SIZE, make_app
from textual.widgets import Button, DataTable, Input, Select, Static, TextArea

from claubiz.tui.jobs import ActivityLog
from claubiz.tui.modals import ConfirmModal, ExportModal, RunImportModal
from claubiz.workspace import Workspace, WorkspaceConfig


async def _until(pilot, condition, tries=300):
    for _ in range(tries):
        if condition():
            return
        await pilot.pause(0.02)
    raise AssertionError("condition not reached")


def _hold(app, *tasks):
    fake = app.runtime.llm
    fake.hold = asyncio.Event()
    fake.held = set(tasks)
    return fake


async def _start_planned(app, pilot, goal="시장 조사"):
    app.query_one("#goal", TextArea).text = goal
    await pilot.press("f5")
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.press("f6")


async def test_switching_workspace_keeps_run_and_allows_parallel_runs():
    Workspace.create(WorkspaceConfig(name="alpha"))
    Workspace.create(WorkspaceConfig(name="beta"))
    app = make_app("alpha")
    async with app.run_test(size=SIZE) as pilot:
        fake = _hold(app, "t1")
        app.query_one("#goal", TextArea).text = "알파 조사"
        await pilot.press("f9")
        await _until(pilot, lambda: ("start", "t1") in fake.order)
        await pilot.pause(1.1)
        goal = app.query_one("#goal", TextArea)
        assert goal.read_only and app.query_one("#pb", Select).disabled
        assert not app.query_one("#model", Select).disabled
        state = str(app.query_one("#run-state", Static).render())
        assert "실행 중" in state and "00:0" in state

        app.query_one("#ws-list").highlighted = 1
        await pilot.pause()
        assert app.ws.name == "beta"
        assert app.jobs["alpha"].phase == "running" and app.jobs["alpha"].worker.is_running
        assert not goal.read_only and goal.text == ""
        card = app.query_one("#jobs-card", Static)
        assert card.display and "alpha" in str(card.render()) and "실행 중" in str(card.render())

        goal.text = "베타 조사"
        await pilot.press("f9")
        await _until(pilot, lambda: fake.order.count(("start", "t1")) == 2)
        assert {job.phase for job in app.jobs.values()} == {"running"}
        fake.hold.set()
        await _until(pilot, lambda: all(job.phase == "completed" for job in app.jobs.values()))
        assert Workspace.open("alpha").recent_runs()[0]["status"] == "completed"
        assert Workspace.open("beta").recent_runs()[0]["status"] == "completed"
        await pilot.pause()
        assert "완료" in str(card.render())

        await pilot.click("#jobs-card", offset=(3, 1))
        await pilot.pause()
        assert app.ws.name == "alpha"
        assert goal.text == "알파 조사" and not goal.read_only
        assert not card.display
        tasks = app.query_one("#tasks", DataTable)
        assert tasks.row_count == 1 and "완료" in str(tasks.get_row_at(0)[4])


async def test_log_written_while_hidden_keeps_full_width():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        fake = _hold(app, "t4")
        await _start_planned(app, pilot)
        await _until(pilot, lambda: ("start", "t4") in fake.order)
        await pilot.press("f3")
        await pilot.pause()
        log = app.query_one("#log", ActivityLog)
        fake.hold.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause(0.2)
        width = log.scrollable_content_region.width
        assert width > 30
        options = app.console.options.update_width(width)
        expected = sum(len(app.console.render_lines(log._entry(stamp, body), options))
                       for stamp, body in log.buffer.lines)
        narrow = app.console.options.update_width(10)
        squeezed = sum(len(app.console.render_lines(log._entry(stamp, body), narrow))
                       for stamp, body in log.buffer.lines)
        assert len(log.buffer.lines) >= 8
        assert len(log.lines) == expected < squeezed
        assert max(line.cell_length for line in log.lines) <= width
        assert all(cell_len(line.text.rstrip()) <= width for line in log.lines)


async def test_history_row_updates_while_running():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        fake = _hold(app, "t4")
        await _start_planned(app, pilot)
        await _until(pilot, lambda: ("start", "t4") in fake.order)
        await pilot.pause()
        run_id = app.jobs["acme"].run_id
        row = app.query_one("#runs", DataTable).get_row(run_id)
        assert row[4] == "3/4" and "실행 중" in str(row[1])
        assert sum(t["status"] == "done" for t in Workspace.open("acme").load_run(run_id)["tasks"]) == 3
        fake.hold.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        row = app.query_one("#runs", DataTable).get_row(run_id)
        assert row[4] == "4/4" and "완료" in str(row[1])


async def test_resume_cancelled_run_from_history():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        fake = _hold(app, "t4")
        await _start_planned(app, pilot)
        await _until(pilot, lambda: ("start", "t4") in fake.order)
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        fake.hold = None
        run_id = app.jobs["acme"].run_id
        assert app.jobs["acme"].phase == "cancelled"
        assert not app.query_one("#goal", TextArea).read_only
        assert "중단됨" in str(app.query_one("#runs", DataTable).get_row(run_id)[1])

        await pilot.press("f3")
        app.query_one("#runs", DataTable).focus()
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await pilot.pause()
        job = app.jobs["acme"]
        assert job.resumed and job.phase == "planned" and job.run_id == run_id
        assert str(app.query_one("#exec", Button).label).startswith("재개")
        statuses = [str(row[4]) for row in (app.query_one("#tasks", DataTable).get_row_at(i) for i in range(4))]
        assert sum("완료" in s for s in statuses) == 3
        before = len(fake.calls)
        await pilot.press("f6")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert {c[1] for c in fake.calls[before:]} == {"t4", "synth"}
        assert Workspace.open("acme").load_run(run_id)["status"] == "completed"
        assert app.query_one("#runs", DataTable).row_count == 1


async def test_model_change_during_run():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        fake = _hold(app, "t4")
        await _start_planned(app, pilot)
        await _until(pilot, lambda: ("start", "t4") in fake.order)
        app.query_one("#model", Select).value = "claude-sonnet-5-5"
        await pilot.pause()
        job = app.jobs["acme"]
        assert job.orch.settings.models.synthesizer.model == "claude-sonnet-5-5"
        assert any("모델 변경" in body for _, body in job.log.lines)
        fake.hold.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        synth = [m for _, tid, m in fake.models if tid == "synth"]
        assert synth == ["claude-sonnet-5-5"]
        assert app.model_pref == "claude-sonnet-5-5"
        app.query_one("#ws-list").highlighted = 0
        await pilot.pause()
        assert app.query_one("#model", Select).value == "claude-sonnet-5-5"


async def test_import_other_workspace_run_and_export(tmp_path):
    Workspace.create(WorkspaceConfig(name="alpha"))
    Workspace.create(WorkspaceConfig(name="beta"))
    app = make_app("alpha")
    async with app.run_test(size=SIZE) as pilot:
        app.query_one("#goal", TextArea).text = "알파 조사"
        await pilot.press("f9")
        await app.workers.wait_for_complete()
        await pilot.pause()
        source_run = app.jobs["alpha"].run_id

        await pilot.press("f3")
        app.query_one("#runs", DataTable).focus()
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ExportModal)
        out = tmp_path / "out"
        out.mkdir()
        modal.query_one("#x-path", Input).value = str(out)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, ExportModal)
        files = list(out.iterdir())
        assert len(files) == 1 and "알파 조사" in files[0].name
        assert app.export_dir.endswith("out/")

        app.query_one("#ws-list").highlighted = 1
        await pilot.pause()
        await pilot.press("f4")
        await pilot.pause()
        await pilot.click("#import-run")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, RunImportModal) and len(modal.entries) == 1
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, RunImportModal)
        names = [f.name for f in Workspace.open("beta").inputs()]
        assert names == [f"alpha-{source_run}.md"]
        assert app.query_one("#inputs", DataTable).row_count == 1


async def test_delete_input_with_backspace(tmp_path):
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    for name in ("memo.md", "other.md"):
        doc = tmp_path / name
        doc.write_text(f"{name} 내용", encoding="utf-8")
        ws.import_file(doc, "internal")
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        app.query_one("#goal", TextArea).text = "자료 조사"
        await pilot.press("f5")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.jobs["acme"].phase == "planned"

        await pilot.press("f4")
        table = app.query_one("#inputs", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmModal)
        assert len(Workspace.open("acme").inputs()) == 2

        table.move_cursor(row=1)
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("n")
        await pilot.pause()
        assert len(Workspace.open("acme").inputs()) == 2
        await pilot.press("backspace")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert [f.name for f in Workspace.open("acme").inputs()] == ["memo.md"]
        assert not (ws.root / "inputs" / "other.md").exists()
        assert table.row_count == 1 and table.cursor_row == 0

        await pilot.press("f8")
        await pilot.pause()
        assert app.jobs["acme"].phase == "rejected"
        await pilot.press("backspace")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert Workspace.open("acme").inputs() == [] and table.row_count == 0
        lines = [json.loads(line) for line in app.runtime.audit.path.read_text(encoding="utf-8").splitlines()]
        removed = [e["data"] for e in lines if e["event"] == "input.removed"]
        assert [e["name"] for e in removed] == ["other.md", "memo.md"]
        assert all(len(e["sha256"]) == 64 and e["via"] == "tui" for e in removed)
        assert app.runtime.audit.verify()[0]
