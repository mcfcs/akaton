"""A cheap check for whether a document is worth an LLM call at all.

`should_use_llm` asks whether the deterministic extraction is *thin*, not whether the
document is *relevant*, and one of its triggers is `unknown_category`. That inverts the
budget: an off-topic page — precisely the one the verifier is about to reject with
`NO_COMPETITION` — is guaranteed to reach the model, while a clean event page at 0.95
confidence never does.

This runs first, so the model only ever sees documents that could plausibly be a
competition. It is deliberately generous: the job is to exclude the obviously irrelevant,
not to make the accept decision, which `verify_event` still owns.
"""

from __future__ import annotations

from akaton.domain.models import DocumentContext
from akaton.processing.classifier import (
    ACTION_TERMS,
    COMPETITION_TERMS,
    HEADLINE_RECAP_TERMS,
    HEADLINE_RESULT_TERMS,
)
from akaton.processing.normalize import fold_text

# Words that mean a contest on their own. A page saying "competition" may be thin, but
# thin is exactly what the model is for.
CONTEST_TERMS = (
    "competition",
    "contest",
    "olympiad",
    "call for entries",
    "call for applications",
    "call for participants",
)

# Words that are too common to stand alone — "business challenges", "pitch deck", "hack"
# in an unrelated sense — so they need something to actually do alongside them.
WEAK_CONTEST_HINTS = (
    "challenge",
    "hack",
    "pitch",
    "case study",
    "tilt",
)


def looks_like_old_news(title: str | None, snippet: str | None = None) -> bool:
    """True when a search result's own headline says the competition already happened.

    Runs before the fetch, on what the engine gave us. A search result title *is* a
    headline, which is the one place the tense reliably survives — the same finding that
    `classify_document` is built on. Catching it here saves a fetch, an extraction and
    possibly a model call on a document that is going to be rejected anyway.

    Deliberately conservative: any call to action anywhere in the title or snippet stands
    the result down, because a live announcement may well mention last year's winners.
    """
    if not title:
        return False
    headline = fold_text(title).casefold()
    context = fold_text("\n".join(filter(None, (title, snippet)))).casefold()
    if any(term in context for term in ACTION_TERMS):
        return False
    return any(term in headline for term in HEADLINE_RESULT_TERMS + HEADLINE_RECAP_TERMS)


def is_plausibly_relevant(context: DocumentContext) -> bool:
    """True when a document could be a competition announcement."""
    haystack = fold_text(
        "\n".join(filter(None, (context.title, context.snippet, context.text)))
    ).casefold()
    if not haystack.strip():
        return False
    if any(term in haystack for term in COMPETITION_TERMS + CONTEST_TERMS):
        return True
    return any(hint in haystack for hint in WEAK_CONTEST_HINTS) and any(
        term in haystack for term in ACTION_TERMS
    )
