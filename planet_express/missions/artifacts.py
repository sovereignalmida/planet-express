"""Mission artifacts: redacted bytes on disk, a row in `mission_artifacts`, an `artifact` event."""

import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any

from planet_express.core.redact import redact
from planet_express.core.store import Store

from .events import insert_event, redact_value


def store_text(store: Store, root: Path, mission_id: str, name: str, text: str, actor: str = "supervisor") -> str:
    """Redact `text`, write it under `root/<mission>/`, record it. Returns the artifact id."""
    data = redact(text).encode("utf-8")                 # whole text at once: a literal may span lines
    name = redact(name)[:256]                            # the name is persisted too, and becomes a filename
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)[:100] or "artifact"
    artifact_id = f"A-{secrets.token_hex(8)}"
    directory = root / mission_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{artifact_id}-{safe}"
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    now = int(store._clock())
    with store._write() as conn:
        conn.execute("INSERT INTO mission_artifacts (id, mission_id, name, sha256, bytes, path) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (artifact_id, mission_id, name, sha, len(data), str(path)))
        insert_event(conn, mission_id, "artifact", actor,
                     {"artifact": artifact_id, "name": name, "sha256": sha, "bytes": len(data)}, now)
    return artifact_id


def store_json(store: Store, root: Path, mission_id: str, name: str, value: Any, actor: str = "supervisor") -> str:
    return store_text(store, root, mission_id, name,
                      json.dumps(redact_value(value), sort_keys=True, indent=2, ensure_ascii=False), actor)
