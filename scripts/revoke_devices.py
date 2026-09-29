"""Revoke an operator's trusted dashboard devices from the core account."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.core.store import SchemaTooNewError, Store
from web_auth import OPERATOR_PATTERN


def revoke(operator: str, store_factory=Store) -> int:
    """Bump an operator's device epoch. Returns the new epoch.

    Shared with `dashboard_operators.py reset`, which calls it so that changing a passphrase
    signs the old devices out by itself. It stays a command of its own because revoking
    devices without changing anyone's passphrase is a thing you want to be able to do.
    """
    import config

    store = store_factory(config.ACTIONS_DB)
    store.init()
    return store.revoke_devices(operator)


def main(argv, store_factory=Store) -> int:
    if len(argv) != 1 or OPERATOR_PATTERN.fullmatch(argv[0]) is None:
        print("Usage: revoke_devices.py <operator> (1–32 lowercase letters, digits, _, ., -)", file=sys.stderr)
        return 2
    operator = argv[0]
    try:
        epoch = revoke(operator, store_factory)
    except SchemaTooNewError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Device epoch for {operator}: {epoch}. Every trusted device for this operator must log in again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
