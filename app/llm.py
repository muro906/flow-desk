from typing import Literal

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from app.config import get_settings

Category = Literal["billing", "technical", "account", "feature_request", "complaint", "other"]

# Routing is a fixed table rather than a model output: the model can be wrong about a
# category, but it can never be talked into sending a ticket to a queue that doesn't exist.
ROUTES: dict[str, str] = {
    "billing": "billing-team",
    "technical": "engineering-support",
    "account": "account-team",
    "feature_request": "product-team",
    "complaint": "customer-success",
    "other": "general-support",
}

CLASSIFY_PROMPT = """You triage customer support tickets.

Pick exactly one category:
- billing: charges, invoices, refunds, payment methods
- technical: bugs, errors, outages, something not working
- account: login, access, profile, account settings
- feature_request: asking for something the product does not do
- complaint: dissatisfaction with service or staff, no specific fix requested
- other: none of the above

Give your confidence (0.0-1.0) that the category is correct. Be calibrated:
use a low value when the ticket is vague, mixes several issues, or is not a support request."""

SUMMARY_PROMPT = """Summarise this customer support ticket in one or two sentences for the
agent who will handle it: what happened, and what the customer wants."""


class Classified(BaseModel):
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)


class Summary(BaseModel):
    summary: str


class Decision(BaseModel):
    """A complete triage outcome, as cached and as produced by the rules fallback."""

    category: Category
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)


class TransientLLMError(Exception):
    """Worth retrying: the same request may succeed in a moment (timeout, 429, 5xx)."""


class PermanentLLMError(Exception):
    """Not worth retrying: the same request will fail the same way (4xx, refusal)."""


class LLMClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.llm_model
        # SDK retries are off (one attempt): the worker owns the retry policy, and
        # stacking two policies would multiply attempts and hide them from the count.
        http_options = types.HttpOptions(
            timeout=60_000, retry_options=types.HttpRetryOptions(attempts=1)
        )
        self._client = (
            genai.Client(api_key=settings.llm_api_key, http_options=http_options).aio
            if settings.llm_api_key
            else None
        )

    async def classify(self, subject: str | None, body: str) -> Classified:
        return await self._parse(CLASSIFY_PROMPT, subject, body, Classified)

    async def summarise(self, subject: str | None, body: str) -> str:
        return (await self._parse(SUMMARY_PROMPT, subject, body, Summary)).summary

    async def _parse[T: BaseModel](
        self, system: str, subject: str | None, body: str, schema: type[T]
    ) -> T:
        if self._client is None:
            raise PermanentLLMError("LLM_API_KEY is not set")
        try:
            response = await self._client.models.generate_content(
                model=self._model,
                contents=f"Subject: {subject or '(none)'}\n\n{body}",
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=schema,
                    thinking_config=types.ThinkingConfig(thinking_level="low"),
                ),
            )
        except httpx.TransportError as exc:  # connection failures and timeouts
            raise TransientLLMError(f"connection: {exc!r}") from exc
        except genai_errors.APIError as exc:
            if exc.code in (408, 429) or exc.code >= 500:
                raise TransientLLMError(f"{exc.code}: {exc.message}") from exc
            raise PermanentLLMError(f"{exc.code}: {exc.message}") from exc

        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            raise PermanentLLMError(f"prompt blocked: {feedback.block_reason.name}")
        if not isinstance(response.parsed, schema):
            reason = response.candidates[0].finish_reason if response.candidates else None
            raise PermanentLLMError(f"no usable output (finish_reason={reason})")
        return response.parsed
