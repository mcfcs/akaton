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


def authority_for_url(url: str, sources: dict, *, discovery_channel: str | None = None) -> int:
    host = (urlsplit(url).hostname or "").casefold()
    for organizer in sources.get("organizers", []):
        if not organizer.get("enabled", True):
            continue
        for domain in organizer.get("domains", []):
            domain = domain.casefold()
            if host == domain or host.endswith(f".{domain}"):
                return int(organizer.get("authority", DEFAULT_AUTHORITY["official_organizer"]))
    if host in STRUCTURED:
        return DEFAULT_AUTHORITY["structured_platform"]
    if host in SOCIAL:
        return DEFAULT_AUTHORITY["official_social"]
    if host in {"forms.gle", "docs.google.com"}:
        return DEFAULT_AUTHORITY["repost"]
    if discovery_channel == "search_snippet":
        return DEFAULT_AUTHORITY["search_snippet"]
    return DEFAULT_AUTHORITY["third_party"]
