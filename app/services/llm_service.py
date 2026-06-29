"""LLM access via OpenRouter (OpenAI-compatible chat-completions).

External call → lives in the service layer (Layering §4), over the same `httpx`
pattern as `EmailService`. The API key comes from Settings only; we never log the
prompt, the response body, or the key (Security rule 5.6). Text-only — screenshot
*images* are never sent; only their already-extracted OCR text reaches the model.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.eod import EodDraftContent
from app.schemas.task import ParsedTask

logger = get_logger("app.llm")


def _loads_lenient(text: str) -> dict[str, Any] | None:
    """Parse a JSON object the model may have wrapped in a ```fence``` or prose.
    Tries the raw text, then the outermost {...} span (which skips code fences)."""
    candidates = [text.strip()]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _strip_fences(text: str) -> str:
    """Drop a leading/trailing ```fence``` for the prose fallback summary."""
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if "\n" in t:
            first, rest = t.split("\n", 1)
            t = rest if first.strip().lower() in ("", "json") else t
    return t.removesuffix("```").strip()


_SYSTEM_PROMPT = (
    "You are an assistant that writes a concise, factual End-of-Day work summary "
    "for one employee, from the structured signals provided (their tasks for the "
    "day, time worked, and text read from their screen via OCR). Write in third "
    "person, past tense, plain professional English. Do not invent work that the "
    "signals do not support; if signal is thin, say so briefly and lower the "
    "confidence. Never include raw OCR fragments, secrets, or personal data. "
    "Respond with ONLY a JSON object matching exactly this shape: "
    '{"summary": string (2-5 sentence markdown narrative), '
    '"worked_on": string[] (areas/projects/tools), '
    '"tasks_completed": string[] (titles of tasks finished today), '
    '"blockers": string[] (anything stuck or at risk, may be empty), '
    '"confidence": integer 0-100 (how well the signals support the summary)}. '
    "Output the raw JSON object only — no markdown code fences, no prose around it."
)


_TASK_PARSE_PROMPT = (
    "You convert a pasted, free-form to-do list into structured tasks. The paste "
    "groups tasks under assignee headers like 'Tushar -' or 'Arkit-'; a header "
    "such as 'General Tasks -', 'General -', or text with no name means UNASSIGNED. "
    "Each non-empty line under a header is ONE task — its title. Rules: "
    "(1) Strip leading bullets, numbering, and tree characters (├──, └──, │, -, *, •). "
    "(2) Ignore decorative/separator lines and obvious section prose that is not a task. "
    "(3) Keep the title concise (max 256 chars); do not invent tasks. "
    "(4) Match each assignee header to exactly one person from the provided ROSTER "
    "(match on first name / partial, case-insensitive). Put that person's id in "
    "assignee_id. If the header matches no one or is general/unassigned, set "
    "assignee_id to null. NEVER output an id that is not in the ROSTER. "
    "(5) Also echo the header text you used in assignee_name (or null if unassigned). "
    'Respond with ONLY a JSON object of this exact shape: {"tasks": [{"title": string, '
    '"assignee_id": string-uuid-or-null, "assignee_name": string-or-null}]}. '
    "No markdown, no code fences, no prose around the JSON."
)


def _to_parsed_task(item: Any, by_id: dict[str, str]) -> ParsedTask | None:
    """Validate one model-emitted task. Drops anything without a usable title and
    nulls an assignee id the model invented (not in the caller's roster)."""
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    raw_id = item.get("assignee_id")
    assignee_id: uuid.UUID | None = None
    assignee_name: str | None = None
    if isinstance(raw_id, str) and raw_id in by_id:
        assignee_id = uuid.UUID(raw_id)
        assignee_name = by_id[raw_id]
    else:
        raw_name = item.get("assignee_name")
        assignee_name = raw_name if isinstance(raw_name, str) and raw_name.strip() else None
    return ParsedTask(
        title=title.strip()[:256], assignee_id=assignee_id, assignee_name=assignee_name
    )


class LlmError(Exception):
    """Raised when the LLM provider could not produce a usable response."""


class LlmService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def task_parse_ready(self) -> bool:
        """Whether paste-import parsing is configured (key + a model)."""
        return self._settings.task_parse_configured

    async def generate_eod(self, context: str) -> EodDraftContent:
        """Summarise one employee's day. Returns a validated draft; falls back to
        a plain summary if the model's JSON doesn't parse."""
        content = await self._complete(
            context, system_prompt=_SYSTEM_PROMPT, model=self._settings.eod_model
        )
        parsed = _loads_lenient(content)
        if parsed is not None:
            try:
                return EodDraftContent.model_validate(parsed)
            except ValueError:
                pass
        # Model didn't return usable JSON — keep readable prose as the summary.
        return EodDraftContent(summary=_strip_fences(content))

    async def parse_tasks(
        self, text: str, roster: Sequence[tuple[uuid.UUID, str]]
    ) -> list[ParsedTask]:
        """Turn a pasted blob into structured tasks. The roster (id, name) is the
        caller's *visible* people; the model resolves each name header to an id
        from it, and we keep only ids that are actually in the roster — a
        fabricated/out-of-scope id is dropped to null (never trust the model)."""
        by_id = {str(rid): name for rid, name in roster}
        roster_lines = "\n".join(f"- {rid}: {name}" for rid, name in roster) or "- (no one)"
        prompt = f"ROSTER (id: name):\n{roster_lines}\n\nPASTED TASKS:\n{text}"
        content = await self._complete(
            prompt,
            system_prompt=_TASK_PARSE_PROMPT,
            model=self._settings.effective_task_parse_model,
        )
        parsed = _loads_lenient(content)
        raw_tasks = parsed.get("tasks") if isinstance(parsed, dict) else None
        if not isinstance(raw_tasks, list):
            raise LlmError("llm response malformed")
        return [task for item in raw_tasks if (task := _to_parsed_task(item, by_id)) is not None]

    async def _complete(self, user_content: str, *, system_prompt: str, model: str) -> str:
        s = self._settings
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{s.openrouter_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {s.openrouter_api_key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise LlmError("llm provider unreachable") from exc
        if response.status_code >= 400:
            # Status only — never echo the body (it can contain the prompt/PII).
            logger.warning("llm request failed (%s)", response.status_code)
            raise LlmError(f"llm request failed ({response.status_code})")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LlmError("llm response malformed") from exc
        if not isinstance(text, str):
            raise LlmError("llm response malformed")
        return text
