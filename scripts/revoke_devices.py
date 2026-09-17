"""Revoke an operator's trusted dashboard devices from the core account."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.core.store import SchemaTooNewError, Store
from web_auth import OPERATOR_PATTERN


def main(argv, store_factory=Store) -> int:
    if len(argv) != 1 or OPERATOR_PATTERN.fullmatch(argv[0]) is None:
        print("Usage: revoke_devices.py <operator> (1–32 lowercase letters, digits, _, ., -)", file=sys.stderr)
        return 2
    import config

    operator = argv[0]
    store = store_factory(config.ACTIONS_DB)
    try:
        store.init()
    except SchemaTooNewError as exc:
        print(exc, file=sys.stderr)
        return 1
    epoch = store.revoke_devices(operator)
    print(f"Device epoch for {operator}: {epoch}. Every trusted device for this operator must log in again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
