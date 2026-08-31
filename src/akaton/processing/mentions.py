"""What a social post is doing when it says the word "hackathon".

The page classifier treats any occurrence of the word as an announcement. In a community
group that is wrong far more often than it is right: people ask whether one is coming up,
look for teammates for one they have already joined, complain about one that has ended,
and post job openings that read exactly like a call for entries. Only some of those are
an event, and the rest are not noise either — a question that names a competition is
evidence the competition exists.

This was written against Facebook and lived in `discovery/facebook_parse.py`. Reddit
needs the identical judgement, so the vocabulary and the ladder moved here and both
platforms feed it their own cleaned text. What stayed behind is genuinely
platform-specific: Facebook's chrome lines, its `/events/` carve-out, its relative
timestamps.

The classifier is deterministic and reads no model. At roughly fourteen seconds a call
with `llm_concurrency: 1`, classifying a sixty-post run would be fourteen minutes of
serialised model time to decide what a regex settles, and it would invert the discipline
`processing/relevance.py` exists to enforce. The model is spent on the resolved page.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from akaton.domain.models import MentionLead
from akaton.processing.classifier import (
    ACTION_TERMS,
    COMPETITION_TERMS,
    RECAP_TERMS,
    RESULT_TERMS,
    classify_category,
)
from akaton.processing.links import is_form_url, should_follow_url
from akaton.processing.locale import detect_country

YEAR_RE = re.compile(r"\b20\d{2}\b")
NAMED_HACK_RE = re.compile(r"\bhack(?:athon|[a-z]*\d|\s*\d)", re.IGNORECASE)

# Defined in processing.classifier so the relevance gate shares one vocabulary.
ACTION_HINTS = ACTION_TERMS + (
    "register",
    "registration",
    "applications open",
    "application deadline",
    "apply now",
    "sign up",
    "deadline",
    "prize",
    "prizes",
    "stipend",
    "now open",
    "open for",
    "submission",
    "submissions",
)
QUESTION_HINTS = (
    "is there any",
    "are there any",
    "any upcoming",
    "anyone know",
    "know any",
    "recommend a",
    "looking for upcoming",
    "looking for hackathons",
    "looking for competitions",
    "saan may",
    "may hackathon ba",
    "may competition ba",
    "meron bang",
    # Taglish interrogatives. "po" marks a polite question and rarely appears in an
    # announcement, which is what makes these safe as standalone signals.
    "pwede po ba",
    "pwede ba",
    "meron po ba",
    "ano po",
    "paano po",
    "saan po",
    "kailan po",
    "sino po",
    "tanong lang",
    "ask lang",
    "pahelp",
    "help po",
    "may alam ba",
    "may alam po",
)
TEAMMATE_HINTS = (
    "looking for teammate",
    "looking for teammates",
    "looking for team",
    "need teammates",
    "need members",
    "need a teammate",
    "slot available",
    "anyone joining",
    "hanap team",
    "hanap teammate",
    "looking for members",
)
JOB_HINTS = (
    "we are hiring",
    "job opening",
    "we're hiring",
    "apply for this role",
    "internship opening",
    # An internship call reads like a competition announcement — "turn bold ideas into
    # reality", a deadline, student eligibility — but there is nothing to compete in.
    "internship-eligible",
    "internship program",
    "internship programme",
    "internship opportunity",
    "summer internship",
    "on-the-job training",
    "ojt",
    "now hiring",
    "career opportunity",
)

# Kinds that name a competition without announcing one. Each is a lead: the competition
# is real, the thread is simply not its announcement, so the name is worth searching for.
LEAD_KINDS = frozenset({"question", "question_with_link", "teammate", "recap"})

# ---------------------------------------------------------------------------
# Naming the competition a mention is about
# ---------------------------------------------------------------------------
#
# The words a name is built from, walking left from the head term. Getting the
# boundaries right is the whole job: too greedy and the search query is a sentence, too
# timid and it is the word "hackathon", which returns the internet.

# Hard boundaries. Reaching one of these ends the name. Tagalog function words sit
# alongside the English determiners because the posts are Taglish: "sa egov hackaton"
# needs "sa" to be a wall exactly as "the eGov hackathon" needs "the".
STOPWORDS = frozenset(
    {
        # English determiners, prepositions, conjunctions
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "some",
        "any",
        "every",
        "in",
        "on",
        "at",
        "of",
        "for",
        "to",
        "from",
        "with",
        "about",
        "by",
        "into",
        "and",
        "or",
        "but",
        "if",
        "as",
        "than",
        "then",
        "so",
        "because",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "i",
        "we",
        "you",
        "he",
        "she",
        "it",
        "they",
        "me",
        "us",
        "them",
        "my",
        "our",
        "your",
        "his",
        "her",
        "its",
        "their",
        # Verbs and adverbs that introduce a mention rather than belong to the name
        "joining",
        "join",
        "joined",
        "attend",
        "attending",
        "attended",
        "register",
        "registered",
        "registering",
        "participate",
        "participating",
        "entering",
        "looking",
        "searching",
        "asking",
        "know",
        "knows",
        "recommend",
        "planning",
        "upcoming",
        "next",
        "last",
        "recent",
        "past",
        "future",
        "current",
        "ongoing",
        "anyone",
        "someone",
        "everyone",
        "nobody",
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "which",
        "there",
        "here",
        "still",
        "also",
        "just",
        "only",
        "not",
        "no",
        "yes",
        "please",
        "thanks",
        "hi",
        "hello",
        # Tagalog / Taglish function words. "po" and "ba" in particular mark a polite
        # question and never belong to a competition's name.
        "sa",
        "ng",
        "nga",
        "na",
        "po",
        "ba",
        "may",
        "meron",
        "yung",
        "ang",
        "mga",
        "ako",
        "ka",
        "siya",
        "kami",
        "kayo",
        "sila",
        "nila",
        "niya",
        "namin",
        "natin",
        "ito",
        "iyon",
        "yan",
        "dito",
        "doon",
        "kung",
        "kapag",
        "para",
        "pero",
        "ay",
        "din",
        "rin",
        "lang",
        "lamang",
        "daw",
        "raw",
        "pala",
        "kasi",
        "naman",
        "hindi",
        "wala",
        "mayroon",
        "pwede",
        "puwede",
        "sana",
        "baka",
        "tapos",
        "ano",
        "sino",
        "saan",
        "kailan",
        "paano",
        "bakit",
        "alam",
        "tanong",
    }
)

# Words that belong to a name but cannot identify one. "national hackathon" and "coding
# hackathon" are not searchable; "DOST national hackathon" is. So these are carried along
# and simply do not count towards the name having any content of its own.
WEAK_TOKENS = frozenset(
    {
        "national",
        "nationwide",
        "international",
        "global",
        "regional",
        "local",
        "annual",
        "yearly",
        "online",
        "virtual",
        "onsite",
        "hybrid",
        "free",
        "open",
        "student",
        "students",
        "college",
        "university",
        "school",
        "youth",
        "junior",
        "coding",
        "code",
        "programming",
        "tech",
        "technology",
        "it",
        "ai",
        "data",
        "big",
        "great",
        "good",
        "best",
        "first",
        "second",
        "third",
        "new",
        "another",
    }
)

# Terms that name the *kind* of thing, which a mention is anchored on. Multi-word heads
# are listed longest-first so "business case competition" is not read as "competition".
HEAD_TERMS: tuple[tuple[str, ...], ...] = (
    ("business", "case", "competition"),
    ("case", "competition"),
    ("case", "challenge"),
    ("pitch", "competition"),
    ("startup", "competition"),
    ("innovation", "challenge"),
    ("hackathon",),
    ("hackaton",),
    ("ideathon",),
    ("datathon",),
    ("codefest",),
    ("competition",),
    ("challenge",),
    ("tilt",),
)
# How far left to walk. Four is enough for "DOST Regional Science Innovation Hackathon"
# and short enough that a run-on sentence cannot become the query.
MAX_QUALIFIER_TOKENS = 4
# Every word that appears in a head term. A head word cannot be what identifies a name:
# "case competition" is a kind of thing, not the name of one.
HEAD_WORDS = frozenset(word for head in HEAD_TERMS for word in head)
# Spellings that mean the same head. The philhacks corpus carries "egov hackaton" and
# "egov hackathon" from three different people about one competition; without this they
# are three lead keys, and the repeated pings this feature exists to avoid.
HEAD_ALIASES = {"hackaton": "hackathon", "hackathons": "hackathon"}

# No "." in the character class. Including it made "register." a single token, which both
# hid the sentence boundary from the punctuation check and stopped "register" matching
# the stopword list — so "did you register. Hackathon starts today" produced a name.
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'&-]*")
MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
# "may" is the Tagalog word for "there is" and appears in almost every Taglish question
# in the corpus, so on its own it is not evidence of the month. A nearby day or year is.
AMBIGUOUS_MONTHS = frozenset({"may", "march"})
# How far either side of the name to look for something that dates it. Wide enough for
# "the eGov hackathon happening this September", tight enough that a year mentioned in
# an unrelated sentence does not attach itself.
EDITION_HINT_WINDOW = 40


@dataclass(frozen=True)
class NameSpan:
    """A competition name lifted out of a mention, with whatever dates it sat near."""

    text: str
    normalized: str
    edition_hint: str | None = None

    @property
    def query(self) -> str:
        """What to search for.

        Built from the normalized form, not the surface one. Three people in the real
        corpus wrote "egov hackaton", and searching a misspelling back at the engines is
        strictly worse than searching the spelling they all fold onto. Case is dropped
        with it, which costs nothing: the engines are case-insensitive.

        The edition hint is appended when it is not already in the name — it is what
        makes a query for a specific run precise rather than returning every year of it.
        """
        extra = " ".join(
            part for part in (self.edition_hint or "").split() if part not in self.normalized
        )
        return f"{self.normalized} {extra}".strip()


def _is_head(tokens: list[str], index: int) -> int:
    """Length of the head term starting at `index`, or 0.

    The final word may be plural: "any upcoming hackathons" has to reach the same bare
    head that "any upcoming hackathon" does, or it slips through as a name.
    """
    for head in HEAD_TERMS:
        end = index + len(head)
        if end > len(tokens):
            continue
        candidate = [token.casefold() for token in tokens[index:end]]
        last = candidate[-1]
        if last.endswith("s") and last[:-1] in HEAD_WORDS:
            candidate[-1] = last[:-1]
        if candidate == list(head):
            return len(head)
    return 0


# "Hack4Gov", "Hack2Tech": the word "hack" carrying a digit. A digit is what makes it a
# coined name — without it, "hackathons" reads as one, and a lead for the plural of the
# head term is the exact thing this must not produce.
COINED_HACK_RE = re.compile(r"^hack[a-z]*\d[a-z0-9]*$", re.IGNORECASE)


def _looks_named(token: str) -> bool:
    """A token that carries an identity of its own: Hack4Gov, eGovPH, ImaGnation."""
    if COINED_HACK_RE.match(token):
        return True
    # Internal capitals or an embedded digit mark a coined name rather than a word.
    return bool(re.search(r"[A-Za-z][A-Z]", token) or re.search(r"[A-Za-z]\d|\d[A-Za-z]", token))


def _edition_hint(text: str, start: int, end: int) -> str | None:
    """Whatever near the name says which run of it this is.

    This is what stops the cooldown swallowing a new edition: "the eGov hackathon" and
    "eGov hackathon September" produce different lead keys, so the September mention is a
    new lead searched at once while the March one is still cooling.
    """
    window = text[max(0, start - EDITION_HINT_WINDOW) : end + EDITION_HINT_WINDOW].casefold()
    year = re.search(r"\b(20\d{2})\b", window)
    month = None
    for name in MONTHS:
        match = re.search(rf"\b{name}\b", window)
        if not match:
            continue
        if name in AMBIGUOUS_MONTHS and not re.search(rf"\b{name}\b\s*\d|\d\s*\b{name}\b", window):
            continue
        month = name
        break
    parts = [part for part in (year.group(1) if year else None, month) if part]
    return " ".join(parts) or None


def extract_competition_name(
    text: str, vocabulary: frozenset[str] = frozenset()
) -> NameSpan | None:
    """Name the competition a mention is about, or return None if it names none.

    Anchors on a head term and walks left over qualifying tokens, stopping at a stopword
    or punctuation. No model is involved; see this module's docstring for why.

        "pwede po ba manuod if hindi naka register sa egov hackaton?" -> "egov hackaton"
        "anyone joining the eGov hackathon?"                          -> "eGov hackathon"
        "any upcoming hackathon events near manila"                   -> None

    The third is the one that matters. "upcoming" is a stopword, so nothing but the bare
    head survives, and a lead there would spend a search request on the word "hackathon".
    """
    if not text:
        return None
    matches = list(TOKEN_RE.finditer(text))
    tokens = [match.group(0) for match in matches]
    # A head term is tried everywhere before a coined token is considered, so
    # "Questions about Hack4gov competition" yields the pair rather than "Hack4gov"
    # alone. Only a mention with no head term at all falls through to the second pass.
    for named_pass in (False, True):
        for index, token in enumerate(tokens):
            if named_pass:
                if not COINED_HACK_RE.match(token):
                    continue
                length = 1
            else:
                length = _is_head(tokens, index)
                if not length:
                    continue
            span = _span_from(text, matches, tokens, index, length, vocabulary, named_pass)
            if span:
                return span
    return None


def _span_from(
    text: str,
    matches: list[re.Match[str]],
    tokens: list[str],
    index: int,
    length: int,
    vocabulary: frozenset[str],
    head_is_named: bool,
) -> NameSpan | None:
    start = index
    for step in range(1, MAX_QUALIFIER_TOKENS + 1):
        position = index - step
        if position < 0:
            break
        folded = tokens[position].casefold()
        # An alias is checked before the stopword list, because "UP" is both.
        if folded not in vocabulary and folded in STOPWORDS:
            break
        # Punctuation between the tokens ends the name: "...register. Hackathon..."
        gap = text[matches[position].end() : matches[position + 1].start()]
        if any(char in gap for char in '.,;:!?()[]{}"') or "\n" in gap:
            break
        start = position
    span_tokens = tokens[start : index + length]
    identifying = [
        token
        for token in span_tokens[: index - start]
        if token.casefold() not in WEAK_TOKENS and token.casefold() not in HEAD_WORDS
    ]
    if head_is_named:
        identifying.append(tokens[index])
    if not identifying:
        return None
    # A year is not an identity. "hackathon 2026" returns the whole internet.
    if all(token.strip(".").isdigit() for token in identifying):
        return None
    begin, finish = matches[start].start(), matches[index + length - 1].end()
    return NameSpan(
        text=text[begin:finish],
        normalized=" ".join(canonical_token(token) for token in span_tokens),
        edition_hint=_edition_hint(text, begin, finish),
    )


def build_mention(
    body: str,
    *,
    kind: str,
    platform: str,
    source_url: str,
    source_key: str | None = None,
    vocabulary: frozenset[str] = frozenset(),
) -> MentionLead | None:
    """A lead from one classified post, or None if it names nothing searchable.

    Both collectors go through here so a Facebook question and a Reddit question produce
    the same lead key for the same competition, and the two platforms cannot drift.
    """
    if kind not in LEAD_KINDS:
        return None
    span = extract_competition_name(body, vocabulary)
    if not span:
        return None
    return MentionLead(
        name=span.text,
        normalized_name=span.normalized,
        edition_hint=span.edition_hint,
        platform=platform,
        mention_kind=kind,
        source_url=source_url,
        source_key=source_key,
        excerpt=" ".join(body.split())[:300] or None,
        query=span.query,
    )


def canonical_token(token: str) -> str:
    """Fold a token to the spelling the lead key is built from."""
    folded = token.casefold()
    if folded.endswith("s") and folded[:-1] in HEAD_WORDS:
        folded = folded[:-1]
    return HEAD_ALIASES.get(folded, folded)


def has_any(haystack: str, terms: tuple[str, ...]) -> bool:
    return any(term in haystack for term in terms)


def has_competition_term(text: str) -> bool:
    lowered = text.casefold()
    if has_any(lowered, COMPETITION_TERMS) or NAMED_HACK_RE.search(lowered):
        return True
    return classify_category(text).value != "UNKNOWN"


def is_question(body: str, lowered: str, *, category: bool) -> bool:
    """True when the thread is asking about a competition rather than announcing one.

    Checking only the last character missed the real case, whose question is on the
    first line and whose last line is a follow-up remark:

        pwede po ba manuod if hindi naka register sa egov hackaton?
        - pwede pasabit kung may available teams pa ( first timer)
    """
    if has_any(lowered, QUESTION_HINTS):
        return True
    if len(body) > 400:
        return False
    if "?" not in body:
        return False
    # An announcement can open with a rhetorical question — "Have you got a bold idea?" —
    # but it goes on to say what to do about it. The absence of a call to action is what
    # separates someone asking from someone announcing. A link does not settle it: a
    # question can carry the very listing it is asking about.
    return category and not has_any(lowered, ACTION_TERMS)


def is_talk_not_competition(lowered: str) -> bool:
    event_words = (
        "hackathon",
        "hackaton",
        "case competition",
        "ideathon",
        "datathon",
        "codefest",
    )
    talk_words = (
        "conference",
        "webinar",
        "summit",
        "symposium",
        "convention",
        "congress",
        "seminar",
        "masterclass",
        "bootcamp",
        "call for papers",
        "call for abstracts",
    )
    # A talk that names an actual competition format is a competition with a talk
    # attached; a talk that merely says "conference" is not.
    if any(term in lowered for term in event_words):
        return False
    return any(term in lowered for term in talk_words)


def classify_mention(body: str, urls: Iterable[str] | None = None) -> str:
    """Classify one cleaned post or reply.

    `body` must already have platform chrome stripped and styled Unicode folded; `urls`
    must already have the platform's own domain filtered out. Returns one of: event,
    question, question_with_link, teammate, recap, job, foreign, unrelated.
    """
    if not body:
        return "unrelated"
    lowered = body.casefold()
    event_urls = list(urls or ())
    # Host allowlist only. `is_registration_url` matches a `/register` path on any host,
    # so including it here let a spam reply's link make a thread look like an event.
    followable = [url for url in event_urls if should_follow_url(url)]
    # A form link does not decide where the candidate points — the form is not the event
    # page — but it is strong evidence the thread is a real call for entries.
    has_form_link = any(is_form_url(url) for url in event_urls)
    if has_any(lowered, RESULT_TERMS + RECAP_TERMS):
        return "recap"
    if has_any(lowered, JOB_HINTS):
        return "job"
    # A community carries reposts from the wider region. A Malaysian call for entries is
    # not something a Philippine participant can enter, and screening it here saves a
    # pipeline run and an extraction.
    country = detect_country(body)
    if country and country != "PH":
        return "foreign"

    category = has_competition_term(body)
    action = has_any(lowered, ACTION_HINTS)
    dated = bool(YEAR_RE.search(body))
    teammate = has_any(lowered, TEAMMATE_HINTS)
    question = is_question(body, lowered, category=category)

    if teammate:
        return "teammate"
    # A question that happens to contain "register" is still a question:
    # "pwede po ba manuod if hindi naka register sa egov hackaton?"
    if question:
        # A question carrying a real listing is still worth the listing. The thread
        # itself never becomes the candidate; only the page it points at does.
        return "question_with_link" if followable else "question"
    if is_talk_not_competition(lowered):
        return "unrelated"
    if followable or (has_form_link and category):
        return "event"
    if category and action:
        return "event"
    # In a competition community, "X 2026 registration is now open" is the event even
    # when the name does not contain the word "hackathon" (Hack4Gov, Code League).
    if action and dated:
        return "event"
    if category and dated:
        return "event"
    return "unrelated"
