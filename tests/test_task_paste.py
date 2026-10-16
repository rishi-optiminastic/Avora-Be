"""Deterministic paste-import parsing — the part that used to need the LLM.

Pure functions, so these run with no DB, no network and no model: that is the
point. Paste-import used to die whenever OpenRouter was unconfigured or down,
and its assignee matching was whatever the model managed to copy out of a roster
of UUIDs. Both are now code, and code can be tested.
"""

from __future__ import annotations

import uuid

from app.services.task_paste import (
    RosterIndex,
    is_unassigned_label,
    normalize,
    parse_paste,
    resolve_named_tasks,
)

TUSHAR = uuid.uuid4()
ARKIT = uuid.uuid4()
SUPRIYA = uuid.uuid4()
AKSHAE = uuid.uuid4()

ROSTER = [
    (TUSHAR, "Tushar Mehta"),
    (ARKIT, "Arkit Shah"),
    (SUPRIYA, "Supriya Yenpure"),
    (AKSHAE, "Akshae Pawar"),
]


def _titles(text: str) -> list[str]:
    return [t.title for t in parse_paste(text, ROSTER).tasks]


def _assigned(text: str) -> list[tuple[str, uuid.UUID | None]]:
    return [(t.title, t.assignee_id) for t in parse_paste(text, ROSTER).tasks]


# -- The format people actually paste --------------------------------------- #


def test_parses_the_sample_format_and_resolves_each_header() -> None:
    result = parse_paste(
        "Tushar -\n"
        "Circle deployment on server\n"
        "DB data migration from prod to container DB\n"
        "\n"
        "Arkit -\n"
        "Onboarding flow for agency and brand\n"
        "\n"
        "General tasks -\n"
        "Backup for DB container\n",
        ROSTER,
    )
    assert result.structured
    assert [(t.title, t.assignee_id) for t in result.tasks] == [
        ("Circle deployment on server", TUSHAR),
        ("DB data migration from prod to container DB", TUSHAR),
        ("Onboarding flow for agency and brand", ARKIT),
        ("Backup for DB container", None),  # 'General tasks' names nobody
    ]
    # The unassigned group carries no label either, so the UI defaults it to the
    # caller instead of showing "General tasks" as a person to fix.
    assert result.tasks[-1].assignee_name is None


def test_strips_bullets_numbering_and_tree_characters() -> None:
    assert _titles(
        "Tushar -\n"
        "- First thing\n"
        "* Second thing\n"
        "• Third thing\n"
        "1. Fourth thing\n"
        "2) Fifth thing\n"
        "├── Sixth thing\n"
        "│  └── Seventh thing\n"
    ) == [
        "First thing",
        "Second thing",
        "Third thing",
        "Fourth thing",
        "Fifth thing",
        "Sixth thing",
        "Seventh thing",
    ]


def test_ignores_blank_and_divider_lines() -> None:
    assert _titles("Tushar -\n\n-----\nReal task\n=====\n\n") == ["Real task"]


# -- Header shapes ---------------------------------------------------------- #


def test_accepts_the_separators_word_processors_substitute() -> None:
    """A paste out of Docs or WhatsApp has an en dash or em dash, not a hyphen."""
    headers = ("Tushar -", "Tushar-", "Tushar –", "Tushar —", "Tushar:", "Tushar |")  # noqa: RUF001 (dash variants are the point)
    for header in headers:
        assert _assigned(f"{header}\nA task") == [("A task", TUSHAR)], header


def test_accepts_markdown_emphasis_and_qualifiers_around_a_name() -> None:
    for header in ("**Tushar**", "### Tushar", "__Tushar__ -", "Tushar (Design)", "[Tushar]"):
        assert _assigned(f"{header}\nA task") == [("A task", TUSHAR)], header


def test_a_bare_name_line_heads_a_group_when_it_is_someone_on_the_roster() -> None:
    assert _assigned("Tushar\n- A task\n- Another") == [("A task", TUSHAR), ("Another", TUSHAR)]


def test_a_bare_unknown_line_is_a_task_not_a_header() -> None:
    """Without a separator, only a known name may head a group — otherwise the
    first line of an unheaded list would be eaten as a header."""
    assert _titles("Write the release notes\nShip the build") == [
        "Write the release notes",
        "Ship the build",
    ]


def test_a_task_line_ending_in_a_dash_is_not_mistaken_for_a_name() -> None:
    """'Fix login bug -' is three alphabetic words closed by a separator, exactly
    the shape of a header. The imperative verb is what tells them apart."""
    result = parse_paste("Tushar -\nFix login bug -\nUpdate the docs -\nReal work", ROSTER)
    assert result.headers_found == 1
    assert [(t.title, t.assignee_id) for t in result.tasks] == [
        ("Fix login bug", TUSHAR),
        ("Update the docs", TUSHAR),
        ("Real work", TUSHAR),
    ]


def test_a_bulleted_line_is_always_a_task_even_when_it_reads_like_a_name() -> None:
    assert _assigned("Arkit -\n- Tushar -\n") == [("Tushar", ARKIT)]


def test_prose_is_not_treated_as_a_header() -> None:
    long_line = "We should probably revisit the whole onboarding thing at some point -"
    assert long_line.rstrip(" -") in _titles(f"Tushar -\n{long_line}")[0]


# -- Identifying the person ------------------------------------------------- #


def test_matches_on_first_name_last_name_and_full_name() -> None:
    index = RosterIndex(ROSTER)
    assert index.resolve("Tushar") == TUSHAR
    assert index.resolve("Mehta") == TUSHAR
    assert index.resolve("Tushar Mehta") == TUSHAR
    assert index.resolve("tushar mehta") == TUSHAR


def test_matches_a_prefix_and_a_typo() -> None:
    index = RosterIndex(ROSTER)
    assert index.resolve("Supri") == SUPRIYA  # prefix
    assert index.resolve("Supria") == SUPRIYA  # one letter missing
    assert index.resolve("Akshay") == AKSHAE  # misspelt, as people do


def test_ignores_case_punctuation_and_accents() -> None:
    index = RosterIndex([(TUSHAR, "María-José D'Souza")])
    assert index.resolve("maria jose d souza") == TUSHAR
    assert index.resolve("MARIA-JOSE") == TUSHAR
    assert normalize("María-José D'Souza") == "maria jose d souza"


def test_too_short_a_fragment_matches_nobody() -> None:
    """'Su' is a prefix of Supriya, but of plenty else too. Below three
    characters a partial is noise, not an identification."""
    assert RosterIndex(ROSTER).resolve("Su") is None


def test_an_ambiguous_header_resolves_to_nobody_rather_than_guessing() -> None:
    """Two people called Akshay means an 'Akshay' header identifies neither. A
    blank assignee costs one click; the wrong one hands someone a task they
    never see."""
    other = uuid.uuid4()
    index = RosterIndex([(AKSHAE, "Akshay Pawar"), (other, "Akshay Kumar")])
    assert index.resolve("Akshay") is None
    # Enough to tell them apart, and it resolves again.
    assert index.resolve("Akshay Kumar") == other


def test_an_unresolved_header_still_groups_its_tasks_and_keeps_the_label() -> None:
    """Someone not on the caller's roster must not scatter their tasks into
    unlabelled rows - one label to fix beats five orphans."""
    result = parse_paste("Priyanka -\nFirst task\nSecond task", ROSTER)
    assert [(t.assignee_id, t.assignee_name) for t in result.tasks] == [
        (None, "Priyanka"),
        (None, "Priyanka"),
    ]


def test_general_headers_mean_nobody() -> None:
    for label in ("General", "General Tasks", "Misc", "Unassigned", "Others", "Team"):
        assert is_unassigned_label(label), label
    assert not is_unassigned_label("Tushar")


# -- Structure signal (when to fall back to the model) ---------------------- #


def test_structured_is_false_for_a_wall_of_prose() -> None:
    result = parse_paste(
        "So in the meeting we agreed that the deployment needs looking at, and "
        "someone has to own the migration before Friday.",
        ROSTER,
    )
    assert not result.structured  # the caller hands this to the LLM


def test_structured_is_true_for_bullets_with_no_header() -> None:
    result = parse_paste("- First thing\n- Second thing\n- Third thing", ROSTER)
    assert result.structured
    assert len(result.tasks) == 3


def test_structured_is_false_when_nothing_parsed() -> None:
    assert not parse_paste("\n\n-----\n", ROSTER).structured


def test_titles_are_bounded_to_the_column_width() -> None:
    assert len(_titles("Tushar -\n" + "x" * 400)[0]) == 256


# -- The LLM fallback's name resolution ------------------------------------- #


def test_resolve_named_tasks_matches_names_in_code_not_in_the_model() -> None:
    resolved = resolve_named_tasks(
        [
            ("Deploy circle", "Tushar"),
            ("Onboarding flow", "arkit shah"),
            ("Backup the DB", "General tasks"),
            ("Something else", None),
            ("Unknown owner", "Priyanka"),
        ],
        ROSTER,
    )
    assert [(t.title, t.assignee_id, t.assignee_name) for t in resolved] == [
        ("Deploy circle", TUSHAR, "Tushar"),
        ("Onboarding flow", ARKIT, "arkit shah"),
        ("Backup the DB", None, None),  # general → nobody, and no label to fix
        ("Something else", None, None),
        ("Unknown owner", None, "Priyanka"),  # carried through for the user
    ]


def test_resolve_named_tasks_drops_rows_with_no_usable_title() -> None:
    resolved = resolve_named_tasks([("", "Tushar"), ("   ", None), ("Real", None)], ROSTER)
    assert [t.title for t in resolved] == ["Real"]


def test_an_empty_roster_resolves_nobody_without_blowing_up() -> None:
    result = parse_paste("Tushar -\nA task", [])
    assert [(t.assignee_id, t.assignee_name) for t in result.tasks] == [(None, "Tushar")]
