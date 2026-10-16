"""Deterministic parsing of a pasted to-do list into tasks and assignees.

Pure functions, no I/O and no model call — the paste flow runs this FIRST and
only falls back to the LLM for text this cannot make sense of. Two reasons:

1. **It works when the LLM does not.** Paste-import used to be dead whenever
   `OPENROUTER_API_KEY`/`TASK_PARSE_MODEL` were unset or the provider was down,
   even though the format people actually paste (a name header, then one task per
   line) needs no language model at all.
2. **It identifies people far better.** The model used to be handed the roster
   and asked to copy the right UUID back. Copying a 36-character id out of a long
   list is exactly what language models are worst at: a near-miss id was dropped
   to "needs an assignee", and a plausible-but-wrong one silently assigned the
   task to the wrong person. Name resolution now happens here, in code, against
   the caller's roster — whichever path produced the header.

Ambiguity is never guessed. If a header could be two people, it resolves to
nobody and the UI asks: a blank assignee costs one click, the wrong assignee
costs someone a task they never see.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.schemas.task import ParsedTask

# Separators that close an assignee header. The en dash and em dash are in here
# because word processors and chat apps silently substitute them for a typed
# hyphen, and a header that came through Docs or WhatsApp must still parse.
# The literals are kept as characters rather than escapes so the set stays
# readable; ruff's ambiguous-character rule is what they are here to catch.
_SEPARATORS = "-–—:|=>»"  # noqa: RUF001 (dash variants are the point)

# Tree-drawing and bullet prefixes on a task line. Stripped one at a time so a
# nested tree row unwraps without a greedy pattern eating real words.
_BULLET = re.compile(
    r"^\s*(?:[├└][─—–-]*|[│┃┆┊╎]|[-*•·▪▫◦‣>+])\s*"  # noqa: RUF001 (dash/bullet variants are the point)
)
_NUMBERING = re.compile(r"^\s*(?:\(?\d{1,2}[.):]|[a-z][.)])\s+", re.IGNORECASE)
# A rule/divider line: only punctuation, no words.
_DIVIDER = re.compile(r"^[\s\-–—_=*~#·.]+$")  # noqa: RUF001 (dash variants are the point)
# Markdown emphasis and heading marks around a header ("**Tushar**", "### Arkit").
_EMPHASIS = re.compile(r"(?:\*\*|__|\*|_|`|#)+")
# A line wholly wrapped in balanced emphasis. Unwrapped BEFORE bullet stripping,
# because "*" is both an emphasis mark and a bullet: without this, "**Tushar**"
# loses its asterisks to the bullet pattern and is filed as a task called
# "Tushar" instead of heading Tushar's group.
_WRAPPED = re.compile(r"^(\*\*\*|\*\*|\*|___|__|_|`)(?P<text>.+?)\1$")
# Trailing parenthetical or bracketed qualifier: the "(Design)" in
# "Tushar (Design)".
_QUALIFIER = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]\s*$")
# A label wholly inside brackets: "[Arkit]", "(Tushar)". Unwrapped rather than
# treated as a trailing qualifier, which would delete the entire label.
_FULLY_BRACKETED = re.compile(r"^[\(\[\{]\s*(?P<text>[^\)\]\}]+?)\s*[\)\]\}]$")

# Headers that mean "nobody in particular" rather than naming a person.
_UNASSIGNED = frozenset(
    {
        "general",
        "general task",
        "general tasks",
        "common",
        "common tasks",
        "misc",
        "miscellaneous",
        "other",
        "others",
        "unassigned",
        "team",
        "team tasks",
        "shared",
        "backlog",
        "todo",
        "to do",
        "tasks",
        "all",
        "everyone",
    }
)

# A header line is short. Longer than this and it is prose, whatever it ends in.
_MAX_HEADER_CHARS = 48
_MAX_HEADER_WORDS = 5
# An unknown name followed by a separator is still treated as a header so its
# tasks group together - but only when it is this short, so a trailing-dash task
# line does not swallow the lines beneath it.
_MAX_UNKNOWN_HEADER_WORDS = 3

# First words that mark a line as work, not a person. "Fix login bug -" ends in a
# separator and is three alphabetic words, so shape alone cannot tell it from a
# name; the verb can.
_TASK_VERBS = frozenset(
    {
        "add",
        "build",
        "call",
        "change",
        "check",
        "clean",
        "complete",
        "configure",
        "connect",
        "create",
        "debug",
        "delete",
        "deploy",
        "design",
        "do",
        "document",
        "draft",
        "finish",
        "fix",
        "follow",
        "handle",
        "implement",
        "improve",
        "integrate",
        "investigate",
        "make",
        "migrate",
        "move",
        "prepare",
        "push",
        "refactor",
        "release",
        "remove",
        "rename",
        "replace",
        "research",
        "review",
        "revamp",
        "send",
        "set",
        "setup",
        "ship",
        "start",
        "sync",
        "test",
        "update",
        "upgrade",
        "verify",
        "write",
    }
)

# How far a header may be from a name and still be the same person, in single-
# character edits. Expressed as a typo budget rather than a similarity ratio
# because the rule people actually want is "one typo": "Akshay" for Akshae and
# "Supria" for Supriya are each one edit out, while a ratio tuned loose enough to
# catch them also starts matching genuinely different short names.
_MAX_TYPOS_SHORT = 1
_MAX_TYPOS_LONG = 2
_LONG_NAME_CHARS = 8
# Below this length a prefix or fuzzy match is noise ("Ar" matches far too much).
_MIN_PARTIAL_CHARS = 3


def normalize(value: str) -> str:
    """Casefold, drop accents, and reduce every run of punctuation to one space.
    "D'Souza, María-José" → "d souza maria jose"."""
    decomposed = unicodedata.normalize("NFKD", value)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", bare.lower()).strip()


@dataclass(frozen=True)
class _Person:
    employee_id: uuid.UUID
    name: str
    key: str  # normalized full name
    tokens: tuple[str, ...]


class RosterIndex:
    """Name → employee resolution over the caller's visible roster.

    Tiers are tried most to least certain, and each tier needs exactly ONE
    winner. Two people called Akshay mean an Akshay header resolves to neither.
    """

    def __init__(self, roster: Sequence[tuple[uuid.UUID, str]]) -> None:
        self._people = [
            _Person(rid, name, key, tuple(key.split()))
            for rid, name in roster
            if (key := normalize(name))
        ]

    def __len__(self) -> int:
        return len(self._people)

    def resolve(self, label: str) -> uuid.UUID | None:
        """The one person this header names, or None if nobody or several do."""
        key = normalize(label)
        if not key or is_unassigned_label(key):
            return None

        for candidates in self._tiers(key):
            unique = {p.employee_id for p in candidates}
            if len(unique) == 1:
                return next(iter(unique))
            if unique:
                return None  # ambiguous at this confidence - do not guess below it
        return None

    def _tiers(self, key: str) -> list[list[_Person]]:
        """Candidate sets from most to least certain."""
        long_enough = len(key) >= _MIN_PARTIAL_CHARS
        return [
            # 1. The whole name, as written.
            [p for p in self._people if p.key == key],
            # 2. The header is one of their name parts ("Tushar" → Tushar Mehta),
            #    or their whole name appears inside a longer header.
            [p for p in self._people if key in p.tokens],
            [p for p in self._people if len(p.tokens) > 1 and p.key in key],
            # 3. A name part starts with the header ("Supri" → Supriya).
            [p for p in self._people if long_enough and self._prefix_match(p, key)],
            # 4. A typo away from a name part.
            [p for p in self._people if long_enough and self._fuzzy_match(p, key)],
        ]

    @staticmethod
    def _prefix_match(person: _Person, key: str) -> bool:
        return person.key.startswith(key) or any(t.startswith(key) for t in person.tokens)

    @staticmethod
    def _fuzzy_match(person: _Person, key: str) -> bool:
        return any(_within_typo_budget(key, t) for t in (person.key, *person.tokens))


def _edit_distance(left: str, right: str) -> int:
    """Levenshtein distance. Names are short, so the plain row-by-row DP is fast
    enough and is far easier to reason about than a similarity score."""
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for i, lc in enumerate(left, start=1):
        current = [i]
        for j, rc in enumerate(right, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (lc != rc)))
        previous = current
    return previous[-1]


def _within_typo_budget(key: str, target: str) -> bool:
    """Whether two names are close enough to be the same one misspelt: one edit
    for a short name, two for a long one. A length gap wider than the budget
    cannot be closed, so it is rejected before the DP runs."""
    shorter = min(len(key), len(target))
    if shorter < _MIN_PARTIAL_CHARS:
        return False
    budget = _MAX_TYPOS_LONG if shorter >= _LONG_NAME_CHARS else _MAX_TYPOS_SHORT
    if abs(len(key) - len(target)) > budget:
        return False
    return _edit_distance(key, target) <= budget


def is_unassigned_label(label: str) -> bool:
    """Whether a header means "nobody in particular" rather than a person."""
    return normalize(label) in _UNASSIGNED


@dataclass
class PasteResult:
    """What the deterministic pass made of the paste.

    `structured` is the honest signal of whether this parser understood the text
    or merely split it on newlines: it is the caller's cue to prefer the LLM.
    """

    tasks: list[ParsedTask] = field(default_factory=list)
    headers_found: int = 0
    bulleted_lines: int = 0
    plain_lines: int = 0

    @property
    def structured(self) -> bool:
        """True when the paste carried real structure — at least one assignee
        header, or a clear majority of lines bulleted. A wall of prose gives
        neither, and is better handed to the model."""
        if not self.tasks:
            return False
        if self.headers_found:
            return True
        total = self.bulleted_lines + self.plain_lines
        return total > 0 and self.bulleted_lines * 2 >= total


def _strip_qualifiers(core: str) -> str:
    """ "[Tushar]" -> "Tushar"; "Tushar (Design)" -> "Tushar".

    Unwrapping comes first: a label that is entirely bracketed looks exactly like
    a trailing qualifier, and trimming it as one leaves nothing at all.
    """
    text = core.strip()
    while (match := _FULLY_BRACKETED.match(text)) is not None:
        text = match.group("text").strip()
    trimmed = _QUALIFIER.sub("", text).strip()
    # Keep the trim only if it left something behind - otherwise what looked like
    # a qualifier was the whole label.
    return trimmed or text


def _unwrap_emphasis(line: str) -> str:
    """Peel balanced markdown emphasis off a whole line. A one-sided "* " stays
    put - that is a bullet, not emphasis."""
    text = line.strip()
    while (match := _WRAPPED.match(text)) is not None:
        text = match.group("text").strip()
    return text


def _strip_bullet(line: str) -> tuple[str, bool]:
    """(body, had_bullet) — peels nested tree/bullet/number prefixes."""
    body, had = line, False
    while True:
        stripped = _BULLET.sub("", body, count=1)
        stripped = _NUMBERING.sub("", stripped, count=1)
        if stripped == body:
            return body.strip(), had
        body, had = stripped, True


def _header_label(line: str, index: RosterIndex) -> str | None:
    """The assignee label this line declares, or None if it is a task.

    A line qualifies when it is short and either closes with a separator
    ("Tushar -", "Arkit:") or names someone on the roster outright ("Tushar" on
    its own line, with the tasks bulleted beneath it).
    """
    text = _EMPHASIS.sub("", line).strip()
    if not text:
        return None
    core = text.rstrip(" \t" + _SEPARATORS)
    had_separator = len(core) < len(text)
    core = _strip_qualifiers(core).rstrip(" \t" + _SEPARATORS).strip()
    if not core or len(core) > _MAX_HEADER_CHARS:
        return None

    words = core.split()
    if len(words) > _MAX_HEADER_WORDS:
        return None
    # An imperative opener makes this work, not a person — "Fix login bug -".
    if normalize(words[0]) in _TASK_VERBS:
        return None

    if index.resolve(core) is not None or is_unassigned_label(core):
        return core
    if not had_separator:
        return None  # a bare line only heads a group when it names someone known
    # An unknown name with a separator still heads its group, so the tasks stay
    # together and the UI shows one label to fix instead of N orphan rows.
    if len(words) <= _MAX_UNKNOWN_HEADER_WORDS and all(w.isalpha() for w in words):
        return core
    return None


def _clean_title(body: str) -> str:
    """A task title from a line body: no bullet, no trailing separator, collapsed
    whitespace, bounded to the column width."""
    title = _EMPHASIS.sub("", body).strip()
    title = re.sub(r"\s+", " ", title).strip(" \t" + _SEPARATORS).strip()
    return title[:256]


def parse_paste(text: str, roster: Sequence[tuple[uuid.UUID, str]]) -> PasteResult:
    """Split a pasted list into tasks, grouped under the assignee headers it
    declares. Headers resolve against `roster`; an unresolved one is carried
    through as a label for the user to fix rather than dropped."""
    index = RosterIndex(roster)
    result = PasteResult()
    label: str | None = None
    assignee: uuid.UUID | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or _DIVIDER.match(line):
            continue
        body, had_bullet = _strip_bullet(_unwrap_emphasis(line))
        if not body:
            continue
        # A bulleted line is a task even if it reads like a name: the bullet is
        # the author saying so.
        if not had_bullet and (header := _header_label(body, index)) is not None:
            label = None if is_unassigned_label(header) else header
            assignee = index.resolve(header)
            result.headers_found += 1
            continue
        title = _clean_title(body)
        if not title:
            continue
        if had_bullet:
            result.bulleted_lines += 1
        else:
            result.plain_lines += 1
        result.tasks.append(ParsedTask(title=title, assignee_id=assignee, assignee_name=label))
    return result


def resolve_named_tasks(
    items: Sequence[tuple[str, str | None]], roster: Sequence[tuple[uuid.UUID, str]]
) -> list[ParsedTask]:
    """Turn (title, assignee_name) pairs into tasks, resolving each name here.

    Used for the LLM fallback: the model reports the header text it saw and
    nothing more, and the id is decided against the caller's own roster. The
    model is never given ids, so it cannot invent one or hand a task to the wrong
    person.
    """
    index = RosterIndex(roster)
    out: list[ParsedTask] = []
    for raw_title, raw_name in items:
        title = _clean_title(raw_title or "")
        if not title:
            continue
        name = (raw_name or "").strip()
        label = None if not name or is_unassigned_label(name) else name[:128]
        out.append(
            ParsedTask(
                title=title,
                assignee_id=index.resolve(name) if name else None,
                assignee_name=label,
            )
        )
    return out
