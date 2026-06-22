"""LLM access via OpenRouter (OpenAI-compatible chat-completions).

External call → lives in the service layer (Layering §4), over the same `httpx`
pattern as `EmailService`. The API key comes from Settings only; we never log the
prompt, the response body, or the key (Security rule 5.6). Text-only — screenshot
*images* are never sent; only their already-extracted OCR text reaches the model.
"""

from __future__ import annotations

import json

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.eod import EodDraftContent

logger = get_logger("app.llm")

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
    '"confidence": integer 0-100 (how well the signals support the summary)}.'
)


class LlmError(Exception):
    """Raised when the LLM provider could not produce a usable response."""


class LlmService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def generate_eod(self, context: str) -> EodDraftContent:
        """Summarise one employee's day. Returns a validated draft; falls back to
        a plain summary if the model's JSON doesn't parse."""
        content = await self._complete(context)
        try:
            return EodDraftContent.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValueError):
            # Model ignored the JSON instruction — keep the prose as the summary.
            return EodDraftContent(summary=content.strip())

    async def _complete(self, user_content: str) -> str:
        s = self._settings
        payload = {
            "model": s.eod_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
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
