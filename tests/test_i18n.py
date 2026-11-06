import os

import pytest

from claubiz import cli, i18n, launcher
from claubiz.schedule import ScheduleError, parse_schedule


def test_language_resolution(monkeypatch, tmp_path):
    i18n.language.cache_clear()
    try:
        monkeypatch.setenv("CLAUBIZ_LANG", "en_US.UTF-8")
        assert i18n.language() == "en"
        i18n.language.cache_clear()
        monkeypatch.delenv("CLAUBIZ_LANG")
        monkeypatch.setenv("CLAUBIZ_HOME", str(tmp_path))
        (tmp_path / "config.toml").write_text('[ui]\ntheme = "claubiz"\n', encoding="utf-8")
        assert i18n.language() == "ko"
        i18n.language.cache_clear()
        (tmp_path / "config.toml").write_text('[ui]\nlanguage = "en"\n', encoding="utf-8")
        assert i18n.language() == "en"
        i18n.language.cache_clear()
        (tmp_path / "config.toml").unlink()
        monkeypatch.setattr(i18n, "system_language", lambda: "en")
        assert i18n.language() == "en"
    finally:
        i18n.language.cache_clear()
    assert i18n.normalize("ko-KR") == "ko" and i18n.normalize("fr_FR") is None and i18n.normalize(None) is None


def test_tr_formats_named_values():
    assert i18n.tr("{hours}시간마다", hours=6) == "6시간마다"


@pytest.mark.parametrize(("text", "expected"), [
    ("daily 09:00", "매일 09:00"),
    ("weekdays 8:30am", "평일 08:30"),
    ("Mon, Thu 10:00", "매주 월,목 10:00"),
    ("mon wed fri 3:30 pm, 18:00", "매주 월,수,금 15:30, 18:00"),
    ("weekends 9am", "주말 09:00"),
    ("every 6 hours", "6시간마다"),
    ("12am", "매일 00:00"),
    ("weekly mon 9am", "매주 월 09:00"),
])
def test_english_schedule_syntax(text, expected):
    assert parse_schedule(text).describe() == expected


@pytest.mark.parametrize("text", ["weekly 09:00", "매주 9am", "weekly"])
def test_weekly_needs_days(text):
    with pytest.raises(ScheduleError):
        parse_schedule(text)


def test_preload_reads_only_leading_global_options(monkeypatch):
    monkeypatch.setenv("CLAUBIZ_LANG", "ko")
    monkeypatch.setenv("CLAUBIZ_HOME", "/unchanged")
    launcher.preload(["--lang", "en", "run", "--home", "/other", "--lang=ko"])
    assert os.environ["CLAUBIZ_LANG"] == "en" and os.environ["CLAUBIZ_HOME"] == "/unchanged"
    launcher.preload(["--version", "--home=/data", "tui"])
    assert os.environ["CLAUBIZ_HOME"] == "/data"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--la", "en", "tui"])
