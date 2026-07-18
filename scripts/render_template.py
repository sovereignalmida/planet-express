"""
scripts/render_template.py — render a systemd unit .template file.

Used by deploy.sh instead of raw sed: an independent Codex review of this spec found
sed substitution corrupts values containing '&', '\\', or the 's|...|...|' delimiter
itself, and even correctly-substituted values containing a space would silently break
an unquoted ExecStart=/WorkingDirectory= line at systemd parse/start time (e.g. an
install under "/home/me/Planet Express" would have ExecStart parse "/home/me/Planet"
as the binary). string.Template does plain literal substitution -- no metacharacter
risk -- and the characters that would break systemd's unquoted parsing are rejected
outright here with a clear error, rather than producing a unit file that silently
fails to start.

Usage:
    python render_template.py <template_path> <INSTALL_DIR> <RUN_USER> <RUN_GROUP> <CONFIG_FILE>
"""

import string
import sys

# These directives are written unquoted in the .service.template files (WorkingDirectory=,
# ExecStart=, Environment=CASA_CONFIG=...) -- systemd word-splits on whitespace there, and
# none of these characters are otherwise meaningful in a real install path.
UNSAFE_CHARS = set(" \t\n\"'$`\\")


def render(template_text: str, **values: str) -> str:
    for key, value in values.items():
        bad = sorted(UNSAFE_CHARS & set(value))
        if bad:
            raise ValueError(
                f"{key}={value!r} contains {bad} -- the systemd unit directives this fills in "
                f"are not quoted, so this would silently produce a broken unit file. Use a "
                f"path/value without spaces or shell/quoting metacharacters."
            )
    return string.Template(template_text).substitute(**values)


def main() -> None:
    template_path, install_dir, run_user, run_group, config_file = sys.argv[1:6]
    with open(template_path) as f:
        template_text = f.read()
    rendered = render(
        template_text,
        INSTALL_DIR=install_dir,
        RUN_USER=run_user,
        RUN_GROUP=run_group,
        CONFIG_FILE=config_file,
    )
    sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
