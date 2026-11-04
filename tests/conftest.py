import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUBIZ_HOME", str(home))
    return home
