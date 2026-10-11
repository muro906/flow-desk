from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from app.config import get_settings

SYSTEM_PROMPT = """You triage customer support tickets.

Pick exactly one category:
- billing: charges, invoices, refunds, payment methods
- technical: bugs, errors, outages, something not working
- account: login, access, profile, account settings
- feature_request: asking for something the product does not do
- complaint: dissatisfaction with service or staff, no specific fix requested
- other: none of the above

Write a one or two sentence summary a support agent can act on.
Give your confidence (0.0-1.0) that the category is correct. Be calibrated:
use a low value when the ticket is vague, mixes several issues, or is not a support request."""

# Routing is a fixed table rather than a model output: the model can be wrong about a
# category, but it can never be talked into sending a ticket to a queue that doesn't exist.
ROUTES = {
    "billing": "billing-team",
    "technical": "engineering-support",
    "account": "account-team",
    "feature_request": "product-team",
    "complaint": "customer-success",
    "other": "general-support",
}


class Decision(BaseModel):
    category: Literal["billing", "technical", "account", "feature_request", "complaint", "other"]
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)


class RefusalError(Exception):
    """The model declined the ticket; retrying the same input will not change that."""


class LLMClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=settings.llm_api_key)
        self._model = settings.llm_model

    async def classify(self, subject: str | None, body: str) -> Decision:
        ticket = f"Subject: {subject or '(none)'}\n\n{body}"
        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": ticket}],
            output_config={"effort": "low"},
            output_format=Decision,
        )
        if response.stop_reason == "refusal":
            raise RefusalError(getattr(response.stop_details, "category", None) or "refused")
        return response.parsed_output
