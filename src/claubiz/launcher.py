import os
import sys

OPTIONS = {"--home": "CLAUBIZ_HOME", "--lang": "CLAUBIZ_LANG"}


def preload(argv: list[str]) -> None:
    for index, arg in enumerate(argv):
        if arg == "--":
            return
        for option, env in OPTIONS.items():
            if arg == option and index + 1 < len(argv):
                os.environ[env] = argv[index + 1]
            elif arg.startswith(option + "="):
                os.environ[env] = arg.split("=", 1)[1]


def main() -> int:
    argv = sys.argv[1:]
    preload(argv)
    from .cli import main as run

    return run(argv)
