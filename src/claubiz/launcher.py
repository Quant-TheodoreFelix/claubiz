import os
import sys

OPTIONS = {"--home": "CLAUBIZ_HOME", "--lang": "CLAUBIZ_LANG"}


def preload(argv: list[str]) -> None:
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--" or not arg.startswith("-"):
            return
        name, sep, value = arg.partition("=")
        env = OPTIONS.get(name)
        if env and sep:
            os.environ[env] = value
        elif env and index + 1 < len(argv):
            os.environ[env] = argv[index + 1]
            index += 1
        index += 1


def main() -> int:
    argv = sys.argv[1:]
    preload(argv)
    from .cli import main as run

    return run(argv)
