"""The bundled CPD assessment papers, one per department.

Transcribed from HR's "All Dept Assessment - CPD" document. Each paper is 20
questions worth a mark each, split across five competencies: Technical
Knowledge (8), Problem Solving (3), Communication (3), Interpersonal
Effectiveness (3) and Cultural Alignment & Ownership (3). The pass mark is
60%, which is where the document's own capability scale puts "L3 Competent".

Two things worth knowing before using them:

* **The Marketing paper has no answer key.** The source document never
  carried one. Its questions therefore import UNKEYED, and the test cannot be
  published until HR marks the answers - `AssessmentService.publish` refuses,
  because a question with an empty key marks everybody wrong. Every other
  paper carries the document's own key.
* **The "process flow" questions are ordinary single-answer questions.** The
  four lettered steps are part of the prompt and the options are the four
  candidate orderings, so the key is just an index like any other.

Data, not code: the papers live in `app/data/cpd/*.json` so HR's wording can
be fixed without touching Python. Every file is validated through
`AssessmentWrite` here, so a malformed paper fails loudly on load rather than
quietly creating a broken test.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.assessment import AssessmentWrite

#: Where the transcribed papers live, beside this package.
PAPERS_DIR = Path(__file__).resolve().parent.parent / "data" / "cpd"

#: What every paper is expected to contain. Asserted by the tests rather than
#: enforced here: a department that genuinely needs a 15-question paper should
#: not have to edit code to get one.
QUESTIONS_PER_PAPER = 20
COMPETENCIES = (
    "Technical Knowledge",
    "Problem Solving",
    "Communication",
    "Interpersonal Effectiveness",
    "Cultural Alignment & Ownership",
)


def paper_paths() -> list[Path]:
    """Every bundled paper, in a stable order."""
    return sorted(PAPERS_DIR.glob("*.json"))


def load_paper(path: Path) -> AssessmentWrite:
    """One paper, validated. Raises on anything malformed."""
    return AssessmentWrite.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_papers() -> list[AssessmentWrite]:
    """Every bundled paper, validated."""
    return [load_paper(path) for path in paper_paths()]
