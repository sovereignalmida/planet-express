"""Ship's Memory importer: the generic import format (docs/designs/v4-server-memory-export.md).

Inputs: Markdown files with YAML front matter, JSONL files with the same fields, or plain Markdown
notes with no front matter at all. Rules (contracts §13):
  - Everything imported is a `candidate`. A `status` key in the input is an error, not a request.
  - Declared `source` is kept, except `observed` is recorded as `operator-stated`: an import cannot
    prove PE observed anything. The declared value is kept in the card's history.
  - Idempotent by `id` and content: re-importing the same files changes nothing.
  - A confirmed (or deprecated) card is never overwritten. Different content for its id becomes a
    separate conflict candidate linked to it.
  - Cards containing secret-shaped content are rejected, naming the file and field but never echoing
    the content.
  - `incidents/` and `gaps/` directories hold other record kinds and are skipped.

CLI: python -m planet_express.memory.importer <dir-or-file> --db <path> [--dry-run]
"""

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from planet_express.memory import schema, store
from planet_express.memory.models import Card, CardType, Source

SKIPPED_DIRS = frozenset({"incidents", "gaps"})
MARKDOWN_SUFFIXES = (".md", ".markdown")
SOURCE_CAP = {"observed": "operator-stated"}  # nothing imported ranks above operator-stated
_STEP_RE = re.compile(r"^ {0,3}(\d{1,3})[.)]\s+(\S.*?)\s*$")
_CHECK_RE = re.compile(r"^\s+check:\s*(\S.*?)\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#\s+(\S.*?)\s*#*\s*$")


@dataclass(frozen=True)
class Rejected:
    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.message}"


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: int = 0
    skipped: int = 0
    rejected: list[Rejected] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        text = (f"created {self.created}, updated {self.updated}, unchanged {self.unchanged}, "
                f"conflicts {self.conflicts}, skipped {self.skipped}, rejected {len(self.rejected)}")
        return f"{text} (dry run, nothing written)" if self.dry_run else text


class _Fields(BaseModel):
    """What an input file may say. `status` is deliberately absent, and extras are errors."""
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = None
    type: CardType
    scope: list[str] = Field(default_factory=list)
    source: Source
    confidence_note: str | None = None
    evidence: list[str] = Field(default_factory=list)
    review_after: date | None = None
    supersedes: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)


class _JsonlFields(_Fields):
    statement: str | None = None
    steps: list[dict[str, Any]] | None = None
    body: str | None = None


@dataclass
class _Parsed:
    fields: dict
    statement: str
    steps: list[dict]
    heading: str | None
    location: str


class _Bad(Exception):
    pass


def _format_errors(err: ValidationError) -> str:
    parts = []
    for item in err.errors():
        where = ".".join(str(p) for p in item["loc"])
        msg = item["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


def parse_body(body: str, with_steps: bool, take_heading: bool) -> tuple[str, list[dict], str | None]:
    """Split a Markdown body into (statement, steps, heading). Steps are numbered lines, each optionally
    followed by an indented `check:` line. Only procedure cards are searched for steps."""
    lines = body.split("\n")
    heading = None
    if take_heading:
        first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
        if first is not None and (m := _HEADING_RE.match(lines[first])):
            heading = m.group(1)
            del lines[first]
    statement_lines: list[str] = []
    steps: list[dict] = []
    for line in lines:
        if with_steps and (m := _STEP_RE.match(line)):
            steps.append({"text": m.group(2)})
        elif with_steps and steps and (m := _CHECK_RE.match(line)):
            if "check" in steps[-1]:
                raise _Bad(f"steps.{len(steps) - 1}.check: more than one check for the same step")
            steps[-1]["check"] = m.group(1)
        else:
            statement_lines.append(line)
    return "\n".join(statement_lines).strip(), steps, heading


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-._")[:128]


def parse_markdown(text: str, location: str, stem: str = "") -> _Parsed:
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        # A plain note: no front matter, so nothing to validate beyond what we can infer.
        statement, _, heading = parse_body(text, with_steps=False, take_heading=True)
        card_id = _slug(stem)
        if not card_id:
            raise _Bad("front matter: none, and the file name cannot be turned into an id")
        fields = {"id": card_id, "type": "fact", "source": "operator-stated"}
        return _Parsed(fields, statement, [], heading, location)
    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        raise _Bad("front matter: opening '---' has no closing '---' line")
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise _Bad("front matter: invalid YAML: " + str(exc).splitlines()[0]) from exc
    if not isinstance(meta, dict):
        raise _Bad("front matter: must be a mapping of fields")
    body = "\n".join(lines[end + 1:])
    statement, steps, heading = parse_body(
        body, with_steps=meta.get("type") == "procedure", take_heading=not meta.get("title"))
    return _Parsed(meta, statement, steps, heading, location)


def parse_jsonl_line(line: str, location: str) -> _Parsed:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise _Bad(f"invalid JSON: {exc.msg} (column {exc.colno})") from exc
    if not isinstance(data, dict):
        raise _Bad("each line must be a JSON object")
    if "status" in data:
        raise _Bad("status: not accepted; imported cards are always candidates")
    statement, steps = data.pop("statement", None), data.pop("steps", None)
    body = data.pop("body", None)
    heading = None
    if body is not None:
        if statement is not None or steps is not None:
            raise _Bad("body: give either 'body' or 'statement'/'steps', not both")
        statement, steps, heading = parse_body(
            str(body), with_steps=data.get("type") == "procedure", take_heading=not data.get("title"))
    return _Parsed(data, statement or "", steps or [], heading, location)


def build_card(parsed: _Parsed, now: datetime) -> tuple[Card, str]:
    """Validate and turn parsed input into a candidate Card. Returns (card, history detail)."""
    fields = dict(parsed.fields)
    if "status" in fields:
        raise _Bad("status: not accepted; imported cards are always candidates, remove it")
    try:
        meta = _Fields(**fields)
    except ValidationError as exc:
        raise _Bad(_format_errors(exc)) from exc
    source = SOURCE_CAP.get(meta.source, meta.source)
    notes = [f"imported from {parsed.location}"]
    if source != meta.source:
        notes.append(f"declared source: {meta.source}, recorded as {source}")
    if meta.confidence_note:
        try:
            store.check_text_no_secrets(meta.id, "confidence_note", meta.confidence_note)
        except store.SecretShapedContent as exc:
            raise _Bad(str(exc)) from exc
        notes.append(f"note: {meta.confidence_note}")
    statement = parsed.statement
    if not statement and parsed.steps:
        statement = parsed.heading or meta.title or meta.id
    try:
        card = Card(
            id=meta.id, title=meta.title or parsed.heading or meta.id, type=meta.type, scope=meta.scope,
            statement=statement, steps=parsed.steps, evidence=meta.evidence, source=source,
            status="candidate", created_at=now, review_after=meta.review_after,
            supersedes=meta.supersedes, conflicts_with=meta.conflicts_with)
    except ValidationError as exc:
        raise _Bad(_format_errors(exc)) from exc
    return card, "; ".join(notes)


def _iter_files(path: Path, report: ImportReport):
    if path.is_file():
        yield path
        return
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        if item.suffix.lower() in (*MARKDOWN_SUFFIXES, ".jsonl"):
            if SKIPPED_DIRS & set(item.relative_to(path).parts[:-1]):
                report.skipped += 1
                continue
            yield item


def _items(path: Path, report: ImportReport):
    """Yield (location, _Parsed | _Bad). Parse errors stay with their file or line."""
    for file in _iter_files(path, report):
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            yield str(file), _Bad(f"cannot read file: {exc.__class__.__name__}")
            continue
        if file.suffix.lower() == ".jsonl":
            # Split on \n only: str.splitlines() also breaks on U+2028/U+2029/U+0085, which are legal
            # inside a JSON string and must not cut a record in two.
            for number, line in enumerate(text.split("\n"), 1):
                line = line.removesuffix("\r")
                if not line.strip():
                    continue
                location = f"{file}:{number}"
                try:
                    yield location, parse_jsonl_line(line, location)
                except _Bad as exc:
                    yield location, exc
        else:
            try:
                yield str(file), parse_markdown(text, str(file), file.stem)
            except _Bad as exc:
                yield str(file), exc


def _conflict_id(card_id: str, content_hash: str) -> str:
    return f"{card_id[:100]}--import-{content_hash[:8]}"


def _import_one(conn, card: Card, detail: str, actor: str, now: datetime, report: ImportReport) -> None:
    existing = store.get_card(conn, card.id)
    if existing is None or existing.status == "candidate":
        outcome = store.upsert_candidate(conn, card, actor, now=now, detail=detail)
        setattr(report, outcome, getattr(report, outcome) + 1)
        return
    incoming = card.content_hash()
    if store.stored_hash(conn, card.id) == incoming:
        report.unchanged += 1
        return
    try:  # full validation: the extra edge must not push conflicts_with past its bound
        conflict = Card(**{**card.model_dump(), "id": _conflict_id(card.id, incoming),
                           "conflicts_with": list(dict.fromkeys([card.id, *card.conflicts_with]))})
    except ValidationError as exc:
        raise _Bad(f"cannot record a conflict with existing card {card.id!r}: {_format_errors(exc)}") from exc
    outcome = store.upsert_candidate(
        conn, conflict, actor, now=now,
        detail=f"{detail}; conflicts with {existing.status} card {card.id}, which was not changed")
    if outcome == "unchanged":
        report.unchanged += 1
        return
    store.mark_conflict(conn, card.id, conflict.id, actor, now=now)
    report.conflicts += 1


def import_path(conn: sqlite3.Connection, path, *, actor: str = "importer:cli", dry_run: bool = False,
                now: datetime | None = None) -> ImportReport:
    """Import every card under `path` (a directory or one file). The schema must already exist.
    Bad cards are reported and skipped; good ones are imported. Commits unless dry_run."""
    path = Path(path)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    report = ImportReport(dry_run=dry_run)
    if not path.exists():
        report.rejected.append(Rejected(str(path), "no such file or directory"))
        return report
    seen: dict[str, str] = {}
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")  # take the write lock before any status is read
    conn.execute("SAVEPOINT memory_import")
    try:
        for location, parsed in _items(path, report):
            if isinstance(parsed, _Bad):
                report.rejected.append(Rejected(location, str(parsed)))
                continue
            try:
                card, detail = build_card(parsed, now)
            except _Bad as exc:
                report.rejected.append(Rejected(location, str(exc)))
                continue
            if card.id in seen:
                report.rejected.append(Rejected(
                    location, f"duplicate id {card.id!r} in this import (first seen at {seen[card.id]})"))
                continue
            seen[card.id] = location
            try:
                _import_one(conn, card, detail, actor, now, report)
            except _Bad as exc:
                report.rejected.append(Rejected(location, str(exc)))
            except store.CardExistsError:
                # e.g. an earlier conflict candidate the operator has since confirmed or deprecated:
                # that decision stands, and the rest of the batch carries on.
                report.skipped += 1
            except store.SecretShapedContent as exc:
                report.rejected.append(Rejected(location, str(exc)))
    except BaseException:
        conn.execute("ROLLBACK TO memory_import")
        conn.execute("RELEASE memory_import")
        if own:
            conn.execute("ROLLBACK")
        raise
    if dry_run:
        conn.execute("ROLLBACK TO memory_import")
    conn.execute("RELEASE memory_import")
    if own:
        conn.execute("ROLLBACK" if dry_run else "COMMIT")
    elif not dry_run:
        conn.commit()
    return report


def render_markdown(card: Card) -> str:
    """The card as one front-matter Markdown file, loadable by this importer."""
    if card.type == "procedure" and any(_STEP_RE.match(ln) for ln in card.statement.split("\n")):
        raise ValueError("a procedure statement may not contain numbered lines; they would be read as steps")
    meta: dict[str, Any] = {
        "id": card.id, "title": card.title, "type": card.type, "scope": list(card.scope),
        "source": card.source, "evidence": list(card.evidence), "supersedes": list(card.supersedes),
        "conflicts_with": list(card.conflicts_with),
    }
    if card.review_after:
        meta["review_after"] = card.review_after
    parts = [card.statement]
    if card.steps:
        parts.append("\n".join(f"{n}. {s.text}\n   check: {s.check}" for n, s in enumerate(card.steps, 1)))
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=None, width=100)
    return f"---\n{front}---\n" + "\n\n".join(parts) + "\n"


def render_jsonl(card: Card) -> str:
    """The card as one JSONL line, loadable by this importer."""
    data: dict[str, Any] = {
        "id": card.id, "title": card.title, "type": card.type, "scope": card.scope, "source": card.source,
        "evidence": card.evidence, "supersedes": card.supersedes, "conflicts_with": card.conflicts_with,
        "statement": card.statement,
    }
    if card.steps:
        data["steps"] = [s.model_dump() for s in card.steps]
    if card.review_after:
        data["review_after"] = card.review_after.isoformat()
    return json.dumps(data, ensure_ascii=True)


def _open_db(db: Path, dry_run: bool) -> sqlite3.Connection:
    if not dry_run:
        conn = sqlite3.connect(db)
        schema.init_schema(conn)
        return conn
    # A dry run must not create or alter the database file: work on an in-memory copy.
    conn = sqlite3.connect(":memory:")
    if db.exists():
        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            source.backup(conn)
        finally:
            source.close()
    schema.init_schema(conn)
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m planet_express.memory.importer",
        description="Import Ship's Memory cards (Markdown with front matter, JSONL, or plain notes) as candidates.")
    parser.add_argument("path", help="directory or file to import")
    parser.add_argument("--db", required=True, help="SQLite database file")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    parser.add_argument("--actor", default="importer:cli", help="history actor (default: importer:cli)")
    args = parser.parse_args(argv)
    try:
        conn = _open_db(Path(args.db), args.dry_run)
        report = import_path(conn, args.path, actor=args.actor, dry_run=args.dry_run)
        conn.close()
    except (schema.FTSUnavailable, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if store.redact_is_shape_only():
        print("warning: no Planet Express config found, so site-specific secrets are unknown; cards were "
              "screened by shape only (key names, bearer tokens, private keys, common token formats)",
              file=sys.stderr)
    for item in report.rejected:
        print(f"rejected {item}", file=sys.stderr)
    print(report.summary())
    return 1 if report.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
