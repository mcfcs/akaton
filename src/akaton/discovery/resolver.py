"""Find the page a mention was talking about.

Given a name lifted out of a social post, search for it and pick the result most likely
to be the competition's own page. What the ranking has to be was decided by running the
real queries against the live SearXNG instance, not by reasoning about them:

    "ImaGnation GCash"          -> gcash.com at #1, authority 85
    "Hack4Gov Philippines"      -> pia.gov.ph present at authority 85, but *below*
                                   several news articles
    "eGov hackathon Philippines"-> 87 results led by yugatech, mb.com.ph and inquirer,
                                   every one of them authority 50 and rejected by the
                                   verifier's gate

So taking the first result would have resolved two of those three to a news article that
the pipeline then throws away, having spent the fetch and possibly a model call. The
resolver ranks by `authority_for_url` and takes the best.

It also drops results on the social platforms themselves. One live hit for the Hack4Gov
query was literally "Questions about Hack4gov competition : r/PinoyProgrammer" — that is
resolving a mention to another mention, and it would put the pipeline in a loop between
two threads neither of which announces anything.
"""

from __future__ import annotations

import logging
import re

from akaton.discovery.base import SearchProvider, SearchRequest
from akaton.domain.models import CandidateSeed, LeadRef, MentionLead
from akaton.processing.authority import authority_for_url
from akaton.processing.links import host_of
from akaton.processing.mentions import HEAD_WORDS
from akaton.processing.normalize import is_listing_url, is_news_url, is_registration_url
from akaton.processing.relevance import looks_like_old_news

logger = logging.getLogger(__name__)

# A result has to be at least this authoritative to be worth fetching. It is the
# verifier's own single-source gate: below it the page is rejected as LOW_AUTHORITY, so
# resolving to one spends a fetch to arrive at a rejection.
MIN_RESOLVE_AUTHORITY = 60

# Resolving a mention to another mention is not progress.
MENTION_HOSTS = {
    "facebook.com",
    "fb.com",
    "fb.me",
    "reddit.com",
    "redd.it",
    "twitter.com",
    "x.com",
    "instagram.com",
    "threads.net",
    "linkedin.com",
    "quora.com",
    "medium.com",
}


def _is_mention_host(url: str) -> bool:
    host = host_of(url)
    return any(host == entry or host.endswith(f".{entry}") for entry in MENTION_HOSTS)


def name_tokens(name: str) -> set[str]:
    """The parts of a name that a matching page should actually contain."""
    return {
        token
        for token in re.split(r"[^A-Za-z0-9]+", name.casefold())
        if len(token) > 2 and token not in HEAD_WORDS
    }


def _name_overlap(seed: CandidateSeed, tokens: set[str]) -> int:
    if not tokens:
        return 1
    haystack = " ".join(
        part for part in (str(seed.url), seed.title or "", seed.snippet or "") if part
    ).casefold()
    return sum(1 for token in tokens if token in haystack)


def rank_results(
    seeds: list[CandidateSeed], sources: dict, name: str = ""
) -> list[tuple[int, CandidateSeed]]:
    """Score and order candidate pages, best first, dropping the unusable ones."""
    tokens = name_tokens(name)
    ranked: list[tuple[tuple[int, int], int, CandidateSeed]] = []
    for seed in seeds:
        url = str(seed.url)
        if _is_mention_host(url):
            continue
        # A listing page is a directory of competitions, not one competition, and
        # extracting a single event from it invents one out of unrelated fragments.
        if is_listing_url(url):
            continue
        # A newsroom post about a competition is not the competition. Ranking on
        # authority alone resolved "Hack4Gov Philippines" to
        # pia.gov.ph/news/dict-launches-hack4gov-… — a government news agency, so
        # authority 85, and an article rather than a page anyone can register on. The
        # classifier would reject it after a fetch; dropping it here costs nothing.
        if is_news_url(url) or looks_like_old_news(seed.title, seed.snippet):
            continue
        authority = authority_for_url(url, sources)
        if authority < MIN_RESOLVE_AUTHORITY:
            continue
        # Authority says a host is credible, not that this page is the right one. A live
        # search for "Hack4Gov Philippines" returned elibrary.judiciary.gov.ph at the
        # same authority 85 as the actual announcement, purely for being under gov.ph.
        overlap = _name_overlap(seed, tokens)
        if tokens and not overlap:
            continue
        # A page you can register on beats an equally authoritative page you cannot.
        registrable = 1 if is_registration_url(url) else 0
        ranked.append(((overlap, registrable, authority), len(ranked), seed))
    # Name overlap first, then whether it is a registration page, then authority, then the
    # order the engines returned them — the only other signal available, and a reasonable
    # tiebreak among equally good hosts.
    ranked.sort(key=lambda item: (*item[0], -item[1]), reverse=True)
    return [(score[2], seed) for score, _, seed in ranked]


class LeadResolver:
    """One search per lead, and at most one page out of it."""

    def __init__(
        self, provider: SearchProvider, sources: dict, *, country_term: str = "Philippines"
    ):
        self.provider = provider
        self.sources = sources
        self.country_term = country_term

    def query_for(self, mention: MentionLead) -> str:
        query = mention.query
        # Every organiser worth finding is Philippine, and without this "eGov hackathon"
        # returns Indian and Nigerian e-government events ahead of the local one.
        if self.country_term.casefold() not in query.casefold():
            query = f"{query} {self.country_term}"
        return query

    async def resolve(self, mention: MentionLead, lead_id: int) -> tuple[CandidateSeed | None, str]:
        """Search for the mention's name and return the best page, plus what happened."""
        query = self.query_for(mention)
        page = await self.provider.search(SearchRequest(query=query))
        if page.degraded:
            return None, "search backend unavailable"
        ranked = rank_results(list(page.results), self.sources, mention.name)
        if not ranked:
            return None, f"no authoritative page among {len(page.results)} results"
        authority, best = ranked[0]
        logger.info(
            "lead_resolved",
            extra={"query": query, "url": str(best.url), "authority": authority},
        )
        return (
            best.model_copy(
                update={
                    # The document is what it is — an official page stays an official
                    # page. The lead records why we went looking for it.
                    "discovery_channel": "search",
                    "query": query,
                    "lead": LeadRef(
                        lead_id=lead_id,
                        platform=mention.platform,
                        source_url=mention.source_url,
                        name=mention.name,
                    ),
                }
            ),
            "",
        )
