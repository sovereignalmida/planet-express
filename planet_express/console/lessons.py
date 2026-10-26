"""What a conversation learned, offered to the operator as memory candidates.

The agent may write `lessons.json` in its workspace: a few short statements worth remembering about this host. They are UNTRUSTED
agent output: strict, bounded, scope names checked against the host inventory, secret-shaped text refused by the memory store. They
become CANDIDATE cards (source `inferred`, actor `runtime:console-<conversation>`) and stay quarantined: nothing is injected into any
session until the operator keeps it (`ConsoleApi.lesson_decide`). The agent cannot keep, edit or discard a card.
"""
import hashlib
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from planet_express.console import plan as plan_mod
from planet_express.console.plan import NAME_RE, SERVICE_RE, PlanError
from planet_express.memory.models import Card

LESSONS_FILE = "lessons.json"
MAX_LESSONS_BYTES = 32 * 1024
MAX_PER_TURN = 5
MAX_PER_CONVERSATION = 20
MAX_STATEMENT = 600

NO_MEMORY_INSTRUCTIONS = "Ship's Memory is not available on this host, so there is nowhere to keep a lesson: do not write lessons.json.\n"

LESSONS_INSTRUCTIONS = (
    "When you learn something durable about THIS host that a future investigation would want (a cause, a quirk, a dependency, a thing that is "
    "safe or unsafe), you may write lessons.json in the current directory: {\"lessons\": [{\"title\": short, \"statement\": one or two plain "
    "sentences, \"stacks\": [names], \"services\": [\"stack/service\"]}]}. At most 5 per turn. Write only what you checked here, never a guess, never "
    "a password, key or token. These are only CANDIDATES: the operator decides whether anything is remembered, and nothing you write is "
    "used by any later session until they do. Do not write lessons for one-off facts about this conversation.\n")


class LessonError(PlanError):
    """A refused lessons file (the same shape of refusal as a plan: it never echoes the agent's text)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Lesson(_Strict):
    title: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    statement: Annotated[StrictStr, Field(min_length=1, max_length=MAX_STATEMENT)]
    stacks: Annotated[list[StrictStr], Field(max_length=4)] = []
    services: Annotated[list[StrictStr], Field(max_length=8)] = []


class Lessons(_Strict):
    lessons: Annotated[list[Lesson], Field(min_length=1, max_length=MAX_PER_TURN)]


def parse(doc: Any, inventory: Any = None) -> Lessons:
    try:
        out = Lessons.model_validate(doc)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise LessonError(f"the lessons are not valid: {first['type']}") from None
    for lesson in out.lessons:
        if not all(NAME_RE.fullmatch(s) for s in lesson.stacks) or not all(SERVICE_RE.fullmatch(s) for s in lesson.services):
            raise LessonError("a lesson names a stack or service in a form that is not a plain name")
        if (lesson.stacks or lesson.services) and inventory is None:
            raise LessonError("a lesson names a stack or service, and this host's inventory is not available to check it")
        if inventory is not None and (not set(lesson.stacks) <= set(inventory.stacks) or not set(lesson.services) <= set(inventory.services)):
            raise LessonError("a lesson names a stack or service this host does not have")
    return out


def load_file(workspace: str, inventory: Any = None) -> Lessons:
    try:
        doc = plan_mod.read_doc(workspace, LESSONS_FILE, MAX_LESSONS_BYTES, what="lessons")
    except PlanError as exc:
        raise LessonError(str(exc)) from None
    return parse(doc, inventory)


def card_id(conversation_id: str, lesson: Lesson) -> str:
    """Stable for the same lesson in the same conversation: writing it again changes nothing."""
    return "cl-" + hashlib.sha256(f"{conversation_id}\0{lesson.statement}".encode()).hexdigest()[:16]


def to_card(conversation_id: str, lesson: Lesson, now: datetime) -> Card:
    tags = [f"stack:{s}" for s in lesson.stacks] + [f"service:{s}" for s in lesson.services]
    title = next((line.strip() for line in lesson.title.splitlines() if line.strip()), "lesson")
    for scope in (tags, []):
        try:
            return Card(id=card_id(conversation_id, lesson), title=title, type="fact", scope=scope, statement=lesson.statement,
                        source="inferred", created_at=now, evidence=[f"conversation:{conversation_id}"])
        except ValueError:
            continue                    # an odd scope tag must not cost the lesson its text: it is offered without a scope
    raise ValueError("the lesson cannot be made into a card")


def actor(conversation_id: str) -> str:
    return f"runtime:console-{conversation_id}"[:70]


__all__ = [
    "LESSONS_FILE",
    "LESSONS_INSTRUCTIONS",
    "MAX_PER_CONVERSATION",
    "MAX_STATEMENT",
    "NO_MEMORY_INSTRUCTIONS",
    "Lesson",
    "LessonError",
    "Lessons",
    "actor",
    "card_id",
    "load_file",
    "parse",
    "to_card",
]
