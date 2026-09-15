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

ENV_FILE = "/etc/planetexpress-dashboard.env"
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
        if rendered != text:
            _write_env(rendered)
        if uri:
            print(uri)
            qrencode = shutil.which("qrencode")
            if qrencode:
                subprocess.run([qrencode, "-t", "ANSIUTF8"], input=uri, text=True, check=True)
        print("Restart with: sudo systemctl restart casa-dashboard")
        print("scripts/revoke_devices.py <name> (run as the core user) signs out trusted devices.")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except subprocess.CalledProcessError:
        raise SystemExit("Dashboard provisioning command failed") from None


if __name__ == "__main__":
    main()
