from __future__ import annotations

from urllib.parse import urlsplit

DEFAULT_AUTHORITY = {
    "official_event": 100,
    "official_organizer": 90,
    "official_registration": 85,
    "structured_platform": 80,
    "official_social": 75,
    "community": 60,
    "third_party": 50,
    "repost": 30,
    "search_snippet": 20,
}

STRUCTURED = {
    "devpost.com",
    "www.devpost.com",
    "kaggle.com",
    "www.kaggle.com",
    "eventbrite.com",
    "www.eventbrite.com",
}
SOCIAL = {
    "facebook.com",
    "www.facebook.com",
    "linkedin.com",
    "www.linkedin.com",
    "instagram.com",
    "www.instagram.com",
}


# Words that appear inside a long official name and carry no identity of their own.
# Without this, "University of the Philippines" would put "of" and "the" into the
# vocabulary, and a name walk that consults the vocabulary before its own stopword list
# would then happily swallow "of the" into a competition's name.
_CONNECTIVES = frozenset(
    {"of", "the", "and", "for", "de", "la", "del", "los", "las", "ng", "sa", "at"}
)


def organizer_vocabulary(sources: dict) -> frozenset[str]:
    """Every way a configured organizer is written, casefolded.

    The `aliases:` lists in config/sources.yaml were dead configuration until name
    extraction needed them: nothing read them, and `authority_for_url` matches on domains
    alone. They are what lets "DICT", "ADMU" and "GCash" be recognised as the identifying
    part of a name written by a person rather than by a press office.

    Aliases are kept whole *and* split, because the walk that uses this reads one token
    at a time and has to know that "Diliman" and "Salle" are not stray words. Full names
    are kept whole only: splitting a department's title yields connectives, not identity.
    """
    vocabulary: set[str] = set()
    for organizer in sources.get("organizers", []):
        if not organizer.get("enabled", True):
            continue
        aliases = [str(alias or "").casefold().strip() for alias in organizer.get("aliases") or []]
        name = str(organizer.get("name") or "").casefold().strip()
        if name:
            vocabulary.add(name)
        for alias in aliases:
            if not alias:
                continue
            vocabulary.add(alias)
            vocabulary.update(
                part for part in alias.split() if len(part) > 1 and part not in _CONNECTIVES
            )
    return frozenset(vocabulary)


def authority_for_url(url: str, sources: dict, *, discovery_channel: str | None = None) -> int:
    host = (urlsplit(url).hostname or "").casefold()
    for organizer in sources.get("organizers", []):
        if not organizer.get("enabled", True):
            continue
        for domain in organizer.get("domains", []):
            domain = domain.casefold()
            if host == domain or host.endswith(f".{domain}"):
                return int(organizer.get("authority", DEFAULT_AUTHORITY["official_organizer"]))
    # `platforms` in config/sources.yaml lets a listed event platform or aggregator clear
    # the verifier's authority gate without being modelled as an organizer.
    for domain, authority in (sources.get("platforms") or {}).items():
        domain = str(domain).casefold()
        if host == domain or host.endswith(f".{domain}"):
            return int(authority)
    if host in STRUCTURED:
        return DEFAULT_AUTHORITY["structured_platform"]
    if host in SOCIAL:
        return DEFAULT_AUTHORITY["official_social"]
    if host in {"forms.gle", "docs.google.com"}:
        return DEFAULT_AUTHORITY["repost"]
    if discovery_channel == "search_snippet":
        return DEFAULT_AUTHORITY["search_snippet"]
    return DEFAULT_AUTHORITY["third_party"]
