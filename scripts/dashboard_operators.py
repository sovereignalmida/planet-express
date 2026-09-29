"""Provision Airlock operators in the root-owned dashboard environment file."""

import argparse
import getpass
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_auth
from scripts import revoke_devices

ENV_FILE = "/etc/planetexpress-dashboard.env"
# The unit holding the credentials this script writes. `reset` restarts it itself, because a
# reset is not finished while the old passphrase still works.
RESTART_UNIT = "casa-dashboard"
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env(text):
    """Read single-line systemd environment assignments without evaluating them."""
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", ";")):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            raise ValueError("Expected single-line environment assignments")
        key, value = match.groups()
        try:
            # No interpolation, shell execution, or inline comment stripping.
            words = shlex.split(value, comments=False)
        except ValueError:
            raise ValueError(f"Invalid quoting in {key}") from None
        values[key] = " ".join(words)
    return values


def render_env(text, values):
    """Preserve unchanged lines, comments and key order; remove absent keys."""
    original = parse_env(text)
    lines, seen = [], set()
    for line in text.splitlines(keepends=True):
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if not match:
            lines.append(line)
            continue
        key = match[1]
        seen.add(key)
        if key not in values:
            continue
        if original[key] == values[key]:
            lines.append(line)
        else:
            lines.append(f"{key}={_quote(values[key])}\n")
    for key, value in values.items():
        if key not in seen:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}={_quote(value)}\n")
    return "".join(lines)


def _quote(value):
    if any(char in value for char in "\n\r\0"):
        raise ValueError("Environment values must be single-line")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def operator_keys(name):
    prefix = "PE_OPERATOR_" + name.upper().replace(".", "_").replace("-", "_")
    return prefix + "_PASSPHRASE_HASH", prefix + "_TOTP_SECRET"


def list_operators(values):
    return list(web_auth.load_operators(values))


def apply_operator_change(values, action, name, *, passphrase=None, totp_secret=None):
    """Return a new environment, enforcing the same names and collisions as web_auth."""
    if web_auth.OPERATOR_PATTERN.fullmatch(name) is None:
        raise ValueError("Invalid operator name: use 1–32 lowercase letters, digits, _, . or -")
    operators = web_auth.load_operators(values)
    if action == "add":
        if name in operators:
            raise ValueError("Operator already exists")
        if len(operators) >= 3:
            raise ValueError("At most 3 operators are allowed")
    elif action in {"reset", "remove"}:
        if name not in operators:
            raise ValueError("Operator does not exist")
    else:
        raise ValueError("Unknown operator change")
    result = dict(values)
    hash_key, secret_key = operator_keys(name)
    names = list(operators)
    if action == "remove":
        names.remove(name)
        result.pop(hash_key, None)
        result.pop(secret_key, None)
    else:
        if passphrase is None or totp_secret is None:
            raise ValueError("Passphrase and TOTP secret are required")
        if any(web_auth.verify_passphrase(op.passphrase_hash, passphrase)
               for op in operators.values() if op.name != name):
            raise ValueError("Passphrase is already used by another operator")
        result[hash_key] = web_auth.hash_passphrase(passphrase)
        result[secret_key] = totp_secret
        if action == "add":
            names.append(name)
    result["PE_OPERATORS"] = ",".join(names)
    web_auth.load_operators(result)
    return result


def _read_env():
    result = subprocess.run(["sudo", "cat", ENV_FILE], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return result.stdout
    missing = subprocess.run(["sudo", "test", "!", "-e", ENV_FILE], capture_output=True, check=False)
    if missing.returncode == 0:
        return ""
    raise SystemExit("Could not read dashboard environment file")


def _write_env(text):
    fd, path = tempfile.mkstemp(prefix="pe-dashboard-", suffix=".env")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        subprocess.run(["sudo", "install", "-m", "600", "-o", "root", "-g", "root", path, ENV_FILE],
                       check=True, capture_output=True)
    finally:
        os.unlink(path)


def main(argv=None):
    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit("Don't run this as root (or via sudo); run as the unprivileged install user.")
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "list"):
        subcommands.add_parser(action)
    for action in ("add", "reset", "remove"):
        subcommands.add_parser(action).add_argument("name")
    args = parser.parse_args(argv)
    try:
        text = _read_env()
        values = parse_env(text)
        names = list_operators(values)
        if args.action == "list":
            for name in names:
                print(name)
            return
        action, name = args.action, getattr(args, "name", None)
        if action == "init":
            if not values.get("PE_DASHBOARD_SECRET_KEY"):
                values["PE_DASHBOARD_SECRET_KEY"] = secrets.token_urlsafe(48)
            if len(values["PE_DASHBOARD_SECRET_KEY"]) < 32:
                raise ValueError("PE_DASHBOARD_SECRET_KEY must contain at least 32 characters")
            if not names:
                name = input("First operator name: ").strip()
                action = "add"
        uri = None
        if action in {"add", "reset"}:
            passphrase = getpass.getpass("New passphrase (at least 12 characters): ")
            confirmation = getpass.getpass("Repeat passphrase: ")
            if passphrase != confirmation:
                raise ValueError("Passphrases do not match")
            secret = web_auth.new_totp_secret()
            values = apply_operator_change(values, action, name, passphrase=passphrase, totp_secret=secret)
            uri = web_auth.provisioning_uri(name, secret)
        elif action == "remove":
            values = apply_operator_change(values, action, name)
        rendered = render_env(text, values)
        if action == "reset":
            # Here, and not earlier: after the new passphrase has been read and accepted,
            # so a typo or a mismatched confirmation cannot sign every device out for a reset
            # that then refuses to happen. And before the write, because leaving the old
            # devices signed in is a password reset that does not end the sessions it was
            # performed to end. If this fails -- the install user cannot reach core's
            # database -- nothing has changed yet and the operator can act on that.
            try:
                epoch = revoke_devices.revoke(name)
            except Exception as exc:  # noqa: BLE001 -- any failure here must stop the reset
                raise SystemExit(
                    f"Could not revoke {name}'s trusted devices ({exc}).\n"
                    f"The passphrase has NOT been changed. Run this as the core user, or run\n"
                    f"scripts/revoke_devices.py {name} first and try again.") from None
            print(f"Device epoch for {name}: {epoch}. Trusted devices must log in again.")
            # A second bump follows the restart below; see the note there.
        if rendered != text:
            _write_env(rendered)
        # Before the restart and the second revocation, both of which can fail. The new TOTP
        # secret is live the moment the file is written, so an exit between here and there
        # would leave the operator with an active secret they were never shown -- locked out
        # by the recovery path rather than helped by it.
        if uri:
            print(uri)
            qrencode = shutil.which("qrencode")
            if qrencode:
                # Never fatal. The URI above is the credential; this only draws it. Since this
                # block moved ahead of the restart, a qrencode that exits nonzero would
                # otherwise abort a reset that has already written the new credentials --
                # leaving the old passphrase working and the second revocation skipped,
                # because a picture failed to render.
                try:
                    subprocess.run([qrencode, "-t", "ANSIUTF8"], input=uri, text=True,
                                   check=False)
                except OSError as exc:
                    print(f"(could not draw the QR code: {exc})")

        if action == "reset":
            # The running dashboard still holds the OLD passphrase until it restarts, and it
            # mints tokens against a live epoch lookup. So a sign-in with the old passphrase,
            # in the gap between the bump above and the restart, walks away with a token
            # carrying the NEW epoch -- one that survives the reset it was supposed to end.
            # Restart first so the old credentials stop working, then bump again to strand
            # anything issued during the gap.
            try:
                subprocess.run(["sudo", "systemctl", "restart", RESTART_UNIT],
                               check=True, capture_output=True)
            except (subprocess.CalledProcessError, OSError) as exc:
                raise SystemExit(
                    f"The passphrase was changed, but restarting {RESTART_UNIT} failed ({exc}).\n"
                    f"The old passphrase still works until it restarts. Run:\n"
                    f"  sudo systemctl restart {RESTART_UNIT}\n"
                    f"  scripts/revoke_devices.py {name}") from None
            try:
                epoch = revoke_devices.revoke(name)
            except Exception as exc:  # noqa: BLE001 -- the recovery matters more than the type
                raise SystemExit(
                    f"The passphrase was changed and {RESTART_UNIT} restarted, but the second\n"
                    f"device revocation failed ({exc}). A session that signed in with the OLD\n"
                    f"passphrase during the restart may still be trusted. Run:\n"
                    f"  scripts/revoke_devices.py {name}") from None
            print(f"Restarted {RESTART_UNIT}. Device epoch for {name}: {epoch}.")
        if action != "reset":
            print(f"Restart with: sudo systemctl restart {RESTART_UNIT}")
        if action != "reset":
            print("scripts/revoke_devices.py <name> (run as the core user) signs out trusted "
                  "devices without changing a passphrase.")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except subprocess.CalledProcessError:
        raise SystemExit("Dashboard provisioning command failed") from None


if __name__ == "__main__":
    main()
