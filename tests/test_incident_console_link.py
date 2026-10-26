"""With the Console on, an incident's button opens a conversation (with the incident id) instead of a Propose mission."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_incident_panel_knows_whether_the_console_is_on():
    assert 'data-console="{{ \'1\' if console_enabled() else \'0\' }}"' in (ROOT / "templates/dashboard.html").read_text()


def test_the_incident_button_posts_the_incident_id_to_the_console_and_goes_there():
    source = (ROOT / "static/incidents.js").read_text()
    assert 'root.dataset.console === "1"' in source and "/api/console/new" in source and "incident_id: item.id" in source
    assert 'window.location.assign("/console/"' in source and "ASK THE CONSOLE" in source
    assert "csrf_token: csrf()" in source.split("async function askConsole")[1].split("function shortDate")[0]       # the CSRF token travels with it
