#!/usr/bin/env python3
"""envfile_exec.py ENVFILE [ENVFILE...] -- COMMAND [ARG...]

Load systemd-style environment files, then exec COMMAND. systemd's EnvironmentFile reads values
literally; `sh` sourcing expands `$` and so corrupts values like Werkzeug password hashes
(`scrypt:32768:8:1$salt$hash`). Init scripts on hosts without systemd use this instead of `. file`.
"""
import os
import re
import shlex
import sys

ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load(path: str) -> dict[str, str]:
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle.read().splitlines():
            if not line.strip() or line.lstrip().startswith(("#", ";")):
                continue
            match = ASSIGNMENT.fullmatch(line)
            if not match:
                raise SystemExit(f"{path}: expected KEY=value assignments only")
            values[match[1]] = " ".join(shlex.split(match[2], comments=False))
    return values


def main(argv: list[str]) -> None:
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        raise SystemExit(__doc__)
    split = argv.index("--")
    for path in argv[:split]:
        os.environ.update(load(path))
    os.execvp(argv[split + 1], argv[split + 1:])


if __name__ == "__main__":
    main(sys.argv[1:])
