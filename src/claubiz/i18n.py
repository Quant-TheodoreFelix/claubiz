import json
import os
import plistlib
import sys
import tomllib
from functools import cache
from importlib import resources
from pathlib import Path

LANG_ENV = "CLAUBIZ_LANG"
SUPPORTED = ("ko", "en")
DEFAULT = "ko"
OUTPUT_LANGUAGES = {"ko": "Korean", "en": "English"}
MAX_CONFIG_BYTES = 256 * 1024


def normalize(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    code = value.strip().lower().replace("_", "-").split(".")[0].split("-")[0]
    return code if code in SUPPORTED else None


def _config_file() -> Path:
    raw = os.environ.get("CLAUBIZ_HOME", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".claubiz"
    return base / "config.toml"


def _config_language() -> tuple[bool, str | None]:
    path = _config_file()
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            return False, None
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False, None
    ui = data.get("ui")
    return True, normalize(ui.get("language")) if isinstance(ui, dict) else None


def _macos_language() -> str | None:
    path = Path.home() / "Library" / "Preferences" / ".GlobalPreferences.plist"
    try:
        with path.open("rb") as handle:
            languages = plistlib.load(handle).get("AppleLanguages") or []
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return normalize(languages[0]) if languages else None


def _windows_language() -> str | None:
    import ctypes

    buffer = ctypes.create_unicode_buffer(85)
    try:
        length = ctypes.WinDLL("kernel32").GetUserDefaultLocaleName(buffer, len(buffer))
    except (AttributeError, OSError):
        return None
    return normalize(buffer.value) if length else None


def system_language() -> str | None:
    if sys.platform == "darwin":
        found = _macos_language()
        if found:
            return found
    if sys.platform == "win32":
        return _windows_language()
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value and value not in ("C", "POSIX") and not value.startswith("C."):
            return normalize(value)
    return None


@cache
def language() -> str:
    chosen = normalize(os.environ.get(LANG_ENV))
    if chosen:
        return chosen
    exists, configured = _config_language()
    if exists:
        return configured or DEFAULT
    return system_language() or DEFAULT


@cache
def catalog(lang: str) -> dict[str, str]:
    if lang == DEFAULT:
        return {}
    raw = resources.files("claubiz").joinpath("locales", f"{lang}.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"잘못된 번역 파일: {lang}.json")
    return data


def tr(text: str, /, **values: object) -> str:
    translated = catalog(language()).get(text, text)
    return translated.format(**values) if values else translated
