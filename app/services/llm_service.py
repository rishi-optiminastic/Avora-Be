"""LLM access via OpenRouter (OpenAI-compatible chat-completions).

External call → lives in the service layer (Layering §4), over the same `httpx`
pattern as `EmailService`. The API key comes from Settings only; we never log the
prompt, the response body, or the key (Security rule 5.6).

Mostly text (tasks, activity, OCR text). The opt-in EOD *vision* path
(`describe_screen`) is the one exception: when enabled, a few sampled screenshot
images per day are sent to a vision model to extract on-screen work context. It is
off unless `eod_vision_active`, and the vision prompt forbids transcribing secrets.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.eod import EodDraftContent

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
    "for one employee, from the structured signals provided: their tasks for the "
    "day, time worked, text read from their screen via OCR, and (when present) a "
    "visual screen-context section summarising what their screenshots show. Prefer "
    "the visual screen-context and tasks over raw OCR, which is noisy. Write in "
    "third person, past tense, plain professional English, using the pronouns given "
    "in the context for the employee — never guess their gender from their name. "
    "Be specific about the "
    "projects and tools the signals show, but do not invent work the signals do "
    "not support; if signal is thin, say so briefly and lower the confidence. "
    "Never include raw OCR fragments, secrets, or personal data. "
    "Respond with ONLY a JSON object matching exactly this shape: "
    '{"summary": string (2-5 sentence markdown narrative), '
    '"worked_on": string[] (areas/projects/tools), '
    '"tasks_completed": string[] (titles of tasks finished today), '
    '"blockers": string[] (anything stuck or at risk, may be empty), '
    '"confidence": integer 0-100 (how well the signals support the summary)}. '
    "Output the raw JSON object only — no markdown code fences, no prose around it."
)


_VISION_PROMPT = (
    "You analyze ONE screenshot of an employee's screen (it may span multiple "
    "monitors placed side by side) and report, factually, what work it shows. Read "
    "code, editor tabs, terminals, browser tabs, document and ticket titles, and "
    "app chrome. Respond with ONLY a JSON object of this exact shape: "
    '{"apps": string[] (named apps/tools visible, e.g. "VS Code", "Chrome", "Slack"), '
    '"projects": string[] (repo / project / client / product names visible), '
    '"working_on": string (one concise, factual sentence on what they appear to be doing), '
    '"detail": string (a few more specifics — files, features, tickets, pages)}. '
    "NEVER transcribe secrets, tokens, passwords, API keys, or private messages. If "
    "the screen is idle, empty, or ambiguous, return empty arrays and an empty "
    "working_on. Output the raw JSON object only — no code fences, no prose."
)


# Extraction only: the model reports the header text it saw and never an id.
# It used to be handed the roster and asked to copy the matching UUID back, which
# is the one thing language models are reliably bad at - a near-miss id was
# dropped to "needs an assignee" and a plausible-but-wrong one silently gave the
# task to someone else. Names are now resolved in `task_paste`, against the
# caller's own roster, so the roster never leaves the system at all.
_TASK_PARSE_PROMPT = (
    "You convert a pasted, free-form to-do list into structured tasks. The paste "
    "usually groups tasks under assignee headers like 'Tushar -' or 'Arkit-'; a "
    "header such as 'General Tasks -', 'General -', or text with no name means "
    "unassigned. Each non-empty line under a header is ONE task — its title. Rules: "
    "(1) Strip leading bullets, numbering, and tree characters (├──, └──, │, -, *, •). "
    "(2) Ignore decorative/separator lines and obvious section prose that is not a task. "
    "(3) Keep the title concise (max 256 chars); do not invent tasks. "
    "(4) For each task, copy the assignee header it sat under, VERBATIM and with "
    "nothing added, into assignee_name. Use null when it sat under no header or "
    "under a general/unassigned one. Never put an id, email, or guess in that "
    "field — only text that actually appears in the paste. "
    'Respond with ONLY a JSON object of this exact shape: {"tasks": [{"title": string, '
    '"assignee_name": string-or-null}]}. '
    "No markdown, no code fences, no prose around the JSON."
)


def _to_named_task(item: Any) -> tuple[str, str | None] | None:
    """One model-emitted task as (title, assignee_name). Drops anything without a
    usable title; the name is resolved to a person by the caller, not here."""
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    raw_name = item.get("assignee_name")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
    return title.strip()[:256], name


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

    async def describe_screen(self, *, image_b64: str, content_type: str) -> dict[str, Any]:
        """Vision: extract structured work context from ONE screenshot. Returns
        {apps, projects, working_on, detail} (lists/strings), or empties if the
        model didn't return usable JSON. Only the sampled EOD frames reach this."""
        content = await self._complete_vision(
            image_b64=image_b64,
            content_type=content_type,
            system_prompt=_VISION_PROMPT,
            user_text="Describe what work this screen shows.",
            model=self._settings.effective_eod_vision_model,
        )
        parsed = _loads_lenient(content)
        if not isinstance(parsed, dict):
            return {}
        apps = [str(a) for a in parsed.get("apps", []) if isinstance(a, str)]
        projects = [str(p) for p in parsed.get("projects", []) if isinstance(p, str)]
        working_on = parsed.get("working_on")
        detail = parsed.get("detail")
        return {
            "apps": apps,
            "projects": projects,
            "working_on": working_on if isinstance(working_on, str) else "",
            "detail": detail if isinstance(detail, str) else "",
        }

    async def parse_tasks(self, text: str) -> list[tuple[str, str | None]]:
        """Extract (title, assignee_name) pairs from a pasted blob.

        Only the fallback for text the deterministic parser could not read. The
        model sees the paste and nothing else - no roster, no ids - and the names
        it reports are matched to people by `task_paste.resolve_named_tasks`.
        """
        content = await self._complete(
            f"PASTED TASKS:\n{text}",
            system_prompt=_TASK_PARSE_PROMPT,
            model=self._settings.effective_task_parse_model,
        )
        parsed = _loads_lenient(content)
        raw_tasks = parsed.get("tasks") if isinstance(parsed, dict) else None
        if not isinstance(raw_tasks, list):
            raise LlmError("llm response malformed")
        return [task for item in raw_tasks if (task := _to_named_task(item)) is not None]

    async def _complete(self, user_content: str, *, system_prompt: str, model: str) -> str:
        return await self._post_chat(
            model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )

    async def _complete_vision(
        self,
        *,
        image_b64: str,
        content_type: str,
        system_prompt: str,
        user_text: str,
        model: str,
    ) -> str:
        """Chat completion with one inline image (OpenAI-compatible image_url with a
        base64 data URI). Used only by the opt-in EOD vision path."""
        data_uri = f"data:{content_type};base64,{image_b64}"
        return await self._post_chat(
            model,
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
            http_timeout=90.0,
        )

    async def _post_chat(
        self, model: str, messages: list[dict[str, Any]], *, http_timeout: float = 60.0
    ) -> str:
        s = self._settings
        payload = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
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
