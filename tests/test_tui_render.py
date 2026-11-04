from rich.style import Style
from test_tui import SIZE, make_app
from textual._compositor import ChopsUpdate
from textual.app import App
from textual.widgets import Input, Static, Switch, TextArea

from claubiz.tui import compositor
from claubiz.tui.modals import ImportModal, PlaybookModal
from claubiz.tui.theme import CLAY
from claubiz.workspace import Workspace, WorkspaceConfig


class Overlap(App):
    CSS = """
    Screen { layers: base top; }
    #under { layer: base; width: 5; height: 1; }
    #over { layer: top; width: 20; height: 1; }
    """

    def compose(self):
        yield Static("x", id="under")
        yield Static("가나다라", id="over")


def _line(strip) -> str:
    return "".join(segment.text for segment in strip)


async def test_wide_chars_survive_cuts_from_widgets_below():
    assert compositor.install()
    app = Overlap()
    async with app.run_test(size=(30, 3)) as pilot:
        await pilot.pause()
        comp = app.screen._compositor
        assert 5 in comp.cuts[0]
        original = compositor._original_chops(comp, comp.size.region, lambda y: True)
        assert "다" not in "".join(_line(strip) for strip in original[0].values() if strip is not None)
        assert _line(comp.render_strips()[0]).startswith("가나다라")

        captured = []
        app._display = lambda screen, renderable: captured.append(renderable)
        app.query_one("#over", Static).update("가나다라마")
        await pilot.pause()
        updates = [u for u in captured if isinstance(u, ChopsUpdate)]
        assert updates
        line = updates[-1].chops[0]
        assert "".join(_line(strip) for strip in line.values() if strip is not None).startswith("가나다라마")
        first = next(iter(line.values()))
        assert updates[-1].chop_ends[0][0] == first.cell_length


async def test_hardware_cursor_follows_focused_input():
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        goal = app.query_one("#goal", TextArea)
        await pilot.click("#goal")
        await pilot.pause()
        assert app.cursor_position == goal.cursor_screen_offset
        app.push_screen(PlaybookModal())
        await pilot.pause()
        pid = app.screen.query_one("#p-id", Input)
        assert app.cursor_position == pid.cursor_screen_offset
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is goal and app.cursor_position == goal.cursor_screen_offset
        app.push_screen(ImportModal(ws))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        path = app.screen.query_one("#f-path", Input)
        assert app.cursor_position == path.cursor_screen_offset != (0, 0)


async def test_cursors_are_visible_blocks_in_ansi_theme():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        assert app.ansi_color
        app.push_screen(PlaybookModal())
        await pilot.pause()
        for selector in ("#p-plan", "#p-synth"):
            area = app.screen.query_one(selector, TextArea)
            area.focus()
            await pilot.pause()
            style = area._theme.cursor_style
            assert not style.reverse
            assert style.bgcolor == Style.parse(f"on {CLAY}").bgcolor
        field = app.screen.query_one("#p-id", Input)
        cursor = field.get_component_rich_style("input--cursor")
        assert not cursor.reverse and cursor.bgcolor == Style.parse(f"on {CLAY}").bgcolor


async def test_budget_prefix_and_playbook_options_fit():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        assert str(app.query_one("#cap-prefix", Static).render()).strip() == "$"
        cap = app.query_one("#cap", Input)
        assert cap.value == "3.0"
        assert app.query_one("#cap-prefix").region.right <= cap.region.x
        app.push_screen(PlaybookModal())
        await pilot.pause()
        modal = app.screen
        box = modal.query_one("#playbook-box").content_region
        labels = list(modal.query(".switch-label"))
        switches = list(modal.query(Switch))
        assert len(labels) == len(switches) == 3
        for label in labels:
            assert label.region.width >= len(str(label.render()).strip())
            assert label.region.right <= box.right
        inputs_switch = modal.query_one("#p-inputs", Switch)
        assert inputs_switch.value is False
        await pilot.click(labels[1])
        await pilot.pause()
        assert inputs_switch.value is True


def _cells(strips) -> list[str]:
    from rich.cells import cell_len

    out: list[str] = []
    for strip in strips:
        for segment in strip:
            for ch in segment.text:
                out.append(ch)
                if cell_len(ch) == 2:
                    out.append("")
    return out


async def _dropdown_cells(width: int) -> tuple[list, list, list]:
    from textual.widgets import Select

    app = make_app("acme")
    async with app.run_test(size=(width, 40)) as pilot:
        app.query_one("#goal", TextArea).text = "조사"
        await pilot.press("f5")
        await app.workers.wait_for_complete()
        await pilot.pause()
        select = app.query_one("#pb", Select)
        select.focus()
        await pilot.press("enter")
        await pilot.pause()
        overlay = select.query_one("SelectOverlay")
        region = overlay.region
        expected = [_cells([strip]) for strip in overlay.render_lines(region.reset_offset)]
        comp = app.screen._compositor

        def drawn(chops):
            return [_cells([s for s in chops[y].values() if s is not None])[region.x:region.right]
                    for y in range(region.y, region.bottom)]

        original = drawn(compositor._original_chops(comp, comp.size.region, lambda y: True))
        patched = drawn(comp._render_chops(comp.size.region, lambda y: True))
        return expected, original, patched


async def test_playbook_dropdown_keeps_korean_after_planning():
    from claubiz.playbooks import PlaybookSpec, save_custom_playbook

    Workspace.create(WorkspaceConfig(name="acme"))
    save_custom_playbook(PlaybookSpec(id="partner-scan", title="협력사 발굴", planner_guidance="x", synth_guidance="y"))
    broken = 0
    for width in range(112, 172, 6):
        expected, original, patched = await _dropdown_cells(width)
        assert patched == expected, width
        broken += original != expected
    assert broken
