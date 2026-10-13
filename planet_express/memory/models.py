"""Ship's Memory card models (v4 slice 2).

Cards are bounded text, not transcripts. Everything here is strict: unknown fields are rejected,
enums are closed, and the size bounds are enforced in bytes so a multi-byte statement cannot
slip past the 4 KiB limit.
"""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CardType = Literal["fact", "procedure", "constraint", "decision"]
Source = Literal["observed", "operator-stated", "researched", "inferred"]
Status = Literal["candidate", "confirmed", "deprecated"]
EdgeKind = Literal["supersedes", "conflicts_with"]

MAX_STATEMENT_BYTES = 4096
MAX_STEPS = 20
MAX_TITLE_CHARS = 200
MAX_SCOPE_TAGS = 16
MAX_EVIDENCE = 32
MAX_EDGES = 16

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_TAG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}:\S{1,128}$")
ACTOR_KINDS = ("operator", "runtime", "system", "importer")
_ACTOR_RE = re.compile(r"^(operator|runtime|system|importer):[A-Za-z0-9._@-]{1,64}$")


def parse_actor(actor: str) -> tuple[str, str]:
    """Split `kind:name`. The kind is what authorisation decisions are made on."""
    if not isinstance(actor, str) or not _ACTOR_RE.match(actor):
        raise ValueError(f"invalid actor {actor!r}: expected one of {', '.join(ACTOR_KINDS)} as 'kind:name'")
    kind, name = actor.split(":", 1)
    return kind, name


def _utc(value: datetime) -> datetime:
    """Naive datetimes are taken as UTC; everything is stored and compared in UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Step(_Strict):
    text: str = Field(min_length=1, max_length=1000)
    check: str = Field(min_length=1, max_length=500)

    @field_validator("text", "check")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value:
            raise ValueError("must be one non-blank line")
        return value


def _slug_list(values: list[str], what: str) -> list[str]:
    if len(values) > MAX_EDGES:
        raise ValueError(f"{what}: at most {MAX_EDGES} entries")
    for value in values:
        if not SLUG_RE.match(value):
            raise ValueError(f"{what}: {value!r} is not a card id slug")
    return list(dict.fromkeys(values))


class Card(_Strict):
    id: str
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    type: CardType
    scope: list[str] = Field(default_factory=list)
    statement: str
    steps: list[Step] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    source: Source
    status: Status = "candidate"
    created_at: datetime
    last_verified_at: datetime | None = None
    review_after: date | None = None
    supersedes: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError("id must be a slug: lowercase letters, digits, '.', '_' or '-', at most 128 characters")
        return value

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value:
            raise ValueError("title must be one non-blank line")
        return value

    @field_validator("statement")
    @classmethod
    def _statement_bounds(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("statement must not be empty")
        if len(value.encode()) > MAX_STATEMENT_BYTES:
            raise ValueError(f"statement exceeds {MAX_STATEMENT_BYTES} bytes")
        return value

    @field_validator("scope")
    @classmethod
    def _scope_tags(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_SCOPE_TAGS:
            raise ValueError(f"scope: at most {MAX_SCOPE_TAGS} tags")
        for tag in value:
            if not _TAG_RE.match(tag):
                raise ValueError(f"scope tag {tag!r} must look like 'namespace:value' (for example 'stack:media')")
        return list(dict.fromkeys(value))

    @field_validator("evidence")
    @classmethod
    def _evidence_refs(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_EVIDENCE:
            raise ValueError(f"evidence: at most {MAX_EVIDENCE} references")
        for ref in value:
            if not ref.strip() or len(ref) > 256 or "\n" in ref:
                raise ValueError("evidence references must be one non-blank line of at most 256 characters")
        return value

    @field_validator("supersedes")
    @classmethod
    def _supersedes_slugs(cls, value: list[str]) -> list[str]:
        return _slug_list(value, "supersedes")

    @field_validator("conflicts_with")
    @classmethod
    def _conflicts_slugs(cls, value: list[str]) -> list[str]:
        return _slug_list(value, "conflicts_with")

    @field_validator("created_at", "last_verified_at")
    @classmethod
    def _utc_datetimes(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def _cross_field(self) -> "Card":
        if self.type == "procedure":
            if not self.steps:
                raise ValueError("steps: a procedure needs at least one step, each with a check")
            if len(self.steps) > MAX_STEPS:
                raise ValueError(f"steps: at most {MAX_STEPS} steps")
        elif self.steps:
            raise ValueError(f"steps: only procedure cards have steps, not {self.type}")
        if self.id in self.supersedes:
            raise ValueError("supersedes: a card cannot supersede itself")
        if self.id in self.conflicts_with:
            raise ValueError("conflicts_with: a card cannot conflict with itself")
        return self

    def content_hash(self) -> str:
        """Identity of what the card SAYS, ignoring status and timestamps. Importer idempotence rests on it."""
        payload = {
            "title": self.title, "type": self.type, "scope": sorted(self.scope), "statement": self.statement,
            "steps": [s.model_dump() for s in self.steps], "evidence": self.evidence, "source": self.source,
            "review_after": self.review_after.isoformat() if self.review_after else None,
            "supersedes": sorted(self.supersedes), "conflicts_with": sorted(self.conflicts_with),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()


class Edge(_Strict):
    src: str
    dst: str
    kind: EdgeKind
    created_at: datetime


class HistoryEntry(_Strict):
    seq: int
    card_id: str
    at: datetime
    actor: str
    action: str
    from_status: Status | None = None
    to_status: Status | None = None
    detail: str = ""
