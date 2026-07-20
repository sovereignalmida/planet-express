"""
Tests for scripts/render_template.py's render() -- no real host, no sudo.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest

from render_template import render


def test_render_substitutes_all_placeholders():
    text = "WorkingDirectory=$INSTALL_DIR\nUser=$RUN_USER\nGroup=$RUN_GROUP\n" \
           "Environment=CASA_CONFIG=$CONFIG_FILE\n"
    out = render(
        text,
        INSTALL_DIR="/home/someuser/planet-express",
        RUN_USER="someuser",
        RUN_GROUP="someuser",
        CONFIG_FILE="/etc/planetexpress/config.yaml",
    )
    assert "WorkingDirectory=/home/someuser/planet-express" in out
    assert "User=someuser" in out
    assert "Environment=CASA_CONFIG=/etc/planetexpress/config.yaml" in out


def test_render_does_not_corrupt_ampersand():
    # Regression test: a raw `sed -e "s|$X|$value|g"` would treat '&' in the
    # replacement as "the whole match" -- an independent Codex review found this
    # could corrupt a real install path. string.Template does plain literal
    # substitution, so it must survive as-is. (Backslash is a separate case --
    # it's legitimately unsafe in systemd's unquoted directive syntax and is
    # correctly rejected; see test_render_rejects_other_unsafe_characters.)
    out = render("$INSTALL_DIR", INSTALL_DIR="/srv/a&b/c", RUN_USER="", RUN_GROUP="", CONFIG_FILE="")
    assert out == "/srv/a&b/c"


def test_render_rejects_path_with_space():
    # Regression test: an independent Codex review found that a space in INSTALL_DIR
    # would silently produce a broken (unquoted) ExecStart= line at systemd parse
    # time -- this must be a loud error at render time instead.
    with pytest.raises(ValueError, match="INSTALL_DIR"):
        render("$INSTALL_DIR", INSTALL_DIR="/home/me/Planet Express", RUN_USER="", RUN_GROUP="", CONFIG_FILE="")


@pytest.mark.parametrize("bad_char", ["\t", "\n", '"', "'", "$", "`", "\\", "%"])
def test_render_rejects_other_unsafe_characters(bad_char):
    with pytest.raises(ValueError):
        render("$CONFIG_FILE", INSTALL_DIR="", RUN_USER="", RUN_GROUP="", CONFIG_FILE=f"/etc/pe{bad_char}x.yaml")


def test_render_substitutes_dashboard_port():
    out = render(
        "Environment=CASA_DASHBOARD_PORT=$DASHBOARD_PORT",
        INSTALL_DIR="", RUN_USER="", RUN_GROUP="", CONFIG_FILE="", DASHBOARD_PORT="8420",
    )
    assert out == "Environment=CASA_DASHBOARD_PORT=8420"


def test_render_ignores_dashboard_port_when_template_does_not_reference_it():
    # casa-planetexpress.service.template / casa-stacks.service.template don't
    # contain $DASHBOARD_PORT -- render_template.py's CLI always passes it (empty
    # string when not given), and this must stay a harmless no-op for those two.
    text = "WorkingDirectory=$INSTALL_DIR\n"
    out = render(
        text,
        INSTALL_DIR="/home/someuser/planet-express", RUN_USER="", RUN_GROUP="", CONFIG_FILE="",
        DASHBOARD_PORT="8420",
    )
    assert out == "WorkingDirectory=/home/someuser/planet-express\n"
