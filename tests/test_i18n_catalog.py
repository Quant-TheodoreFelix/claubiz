import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "claubiz"
CATALOG = SRC / "locales" / "en.json"
HANGUL = re.compile(r"[\uac00-\ud7a3]")
FIELD = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)(?:[!:][^{}]*)?\}(?!\})")
MARKUP = re.compile(r"\[/?[a-z ]*\]")
EXEMPT = {
    "i18n.py": None,
    "schedule.py": {"월화수목금토일", "매일", "평일", "주말", "요일", "매주", "반", "오후", "오전"},
    "knowledge.py": None,
    "prompts.py": None,
    "render.py": {"### 확인되지 않은 링크"},
    "workspace.py": {"\n---\n실행 "},
}


def _is_tr(node: ast.Call) -> bool:
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "tr") or (isinstance(func, ast.Attribute) and func.attr == "tr")


def scan() -> tuple[set[str], list[str], list[str]]:
    msgids: set[str] = set()
    dynamic: list[str] = []
    leftovers: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wrapped: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_tr(node):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    msgids.add(node.args[0].value)
                    wrapped.add(id(node.args[0]))
                else:
                    dynamic.append(f"{rel}:{node.lineno}")
        exempt = EXEMPT.get(path.name, set())
        if exempt is None:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"):
                wrapped.update(id(arg) for arg in node.args)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in wrapped:
                text = node.value
                if HANGUL.search(text) and text not in exempt and not text.startswith("^"):
                    leftovers.append(f"{rel}:{node.lineno} {text[:40]!r}")
    return msgids, dynamic, leftovers


def test_messages_are_literal_and_wrapped():
    _, dynamic, leftovers = scan()
    assert not dynamic, f"tr()에는 문자열 리터럴만 넘겨야 함: {dynamic[:20]}"
    assert not leftovers, f"tr()로 감싸지 않은 한국어 문자열 {len(leftovers)}개: {leftovers[:40]}"


def test_english_catalog_is_complete_and_consistent():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    msgids, _, _ = scan()
    missing = sorted(msgids - catalog.keys())
    unused = sorted(catalog.keys() - msgids)
    assert not missing, f"영어 번역 없음 {len(missing)}개: {missing[:40]}"
    assert not unused, f"쓰이지 않는 번역 {len(unused)}개: {unused[:40]}"
    for source, english in catalog.items():
        assert english.strip(), source
        assert not HANGUL.search(english), (source, english)
        assert set(FIELD.findall(source)) == set(FIELD.findall(english)), (source, english)
        assert sorted(MARKUP.findall(source)) == sorted(MARKUP.findall(english)), (source, english)


def test_english_cli_output():
    env = {**os.environ, "CLAUBIZ_LANG": "en"}
    result = subprocess.run([sys.executable, "-m", "claubiz", "--help"], capture_output=True, encoding="utf-8",
                            env=env, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    assert not HANGUL.search(result.stdout), result.stdout


ENGLISH_TUI = r"""
import asyncio
import re
import sys

sys.path.insert(0, sys.argv[1])
from test_tui import SIZE, make_app

from claubiz.workspace import Workspace, WorkspaceConfig

HANGUL = re.compile(r"[가-힣]")


async def main():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    seen = []
    async with app.run_test(size=SIZE) as pilot:
        for key in ("f2", "f3", "f4", "f1", "f10", "f2"):
            await pilot.press(key)
            await pilot.pause()
            if app.screen is not app.screen_stack[0]:
                await pilot.press("escape")
                await pilot.pause()
            seen.append(app.export_screenshot())
    text = re.sub(r"<[^>]+>", " ", "\n".join(seen))
    leftovers = sorted(set(HANGUL.findall(text)))
    print("HANGUL", "".join(leftovers))


asyncio.run(main())
"""


def test_english_tui_renders_without_korean(tmp_path):
    env = {**os.environ, "CLAUBIZ_LANG": "en", "CLAUBIZ_HOME": str(tmp_path / "home"), "HOME": str(tmp_path),
           "USERPROFILE": str(tmp_path)}
    tests_dir = str(Path(__file__).resolve().parent)
    result = subprocess.run([sys.executable, "-c", ENGLISH_TUI, tests_dir], capture_output=True, encoding="utf-8",
                            env=env, check=False, timeout=180)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "HANGUL " in result.stdout, result.stdout
    assert result.stdout.strip().endswith("HANGUL"), result.stdout[-500:]
