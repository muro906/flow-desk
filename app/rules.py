"""Keyword routing for when the LLM provider is unavailable."""

from app.llm import Decision

KEYWORDS: dict[str, tuple[str, ...]] = {
    "billing": ("invoice", "refund", "charge", "billed", "payment", "billing", "subscription"),
    "account": ("login", "log in", "password", "sign in", "locked out", "2fa", "account"),
    "technical": ("error", "bug", "crash", "broken", "not working", "outage", "down"),
    "feature_request": ("feature", "would be great", "could you add", "suggestion", "wish"),
    "complaint": ("disappointed", "unacceptable", "terrible", "worst", "complain"),
}


def triage(subject: str | None, body: str) -> Decision:
    text = f"{subject or ''} {body}".casefold()
    category = next(
        (name for name, words in KEYWORDS.items() if any(word in text for word in words)),
        "other",
    )
    # Confidence 0.0 sends every rules decision to human review: a keyword match is a
    # suggestion for the reviewer, never something to route on unchecked.
    return Decision(category=category, summary=" ".join(body.split())[:280], confidence=0.0)
