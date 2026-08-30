from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
}
TRACKING_PREFIXES = ("utm_",)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def normalize_title(value: str | None) -> str:
    text = normalize_text(value)
    noise = {"official", "registration", "register", "applications", "application"}
    return " ".join(token for token in text.split() if token not in noise)


def normalize_organizer(value: str | None) -> str:
    text = normalize_text(value)
    suffixes = {"inc", "corporation", "corp", "university", "philippines", "ph"}
    return " ".join(token for token in text.split() if token not in suffixes)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.casefold() or "https"
    hostname = (parts.hostname or "").casefold().encode("idna").decode("ascii")
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_KEYS and not key.casefold().startswith(TRACKING_PREFIXES)
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def is_registration_url(url: str) -> bool:
    lowered = url.casefold()
    return any(
        marker in lowered
        for marker in (
            "forms.gle/",
            "docs.google.com/forms",
            # Only a specific Eventbrite listing registers you. Matching the bare domain
            # also matched discovery pages such as /d/ca--sunnyvale/..., which made a
            # city directory look like a registrable event.
            "eventbrite.com/e/",
            "devpost.com/register",
            "/register",
            "/registration",
            "/apply",
            "lu.ma/",
        )
    )


LISTING_MARKERS = (
    "/d/",
    "/discover",
    "/search",
    "/sitemap",
    "/browse",
    "/tag/",
    "/category/",
    "/categories/",
    "/hackathons/",
    "/events/browse",
)


def is_listing_url(url: str) -> bool:
    """True for directory pages that enumerate many events rather than describing one.

    A city or tag listing mentions every category and location it contains, so treating
    it as a single event invents one out of unrelated fragments.
    """
    parts = urlsplit(url.casefold())
    path = parts.path if parts.path.endswith("/") else f"{parts.path}/"
    if any(marker in path for marker in LISTING_MARKERS):
        return True
    return any(key in ("q", "query", "search") for key, _ in parse_qsl(parts.query))


def extract_edition(title: str | None, *date_years: int | None) -> tuple[str | None, int | None]:
    normalized = normalize_title(title)
    year_match = re.search(r"\b(20\d{2})\b", normalized)
    year = int(year_match.group(1)) if year_match else next((y for y in date_years if y), None)
    edition_match = re.search(r"\b(?:edition|season)\s*(\d{1,2})\b", normalized)
    edition = edition_match.group(1) if edition_match else None
    if year and edition:
        return f"{year}:edition-{edition}", year
    if year:
        return str(year), year
    if edition:
        return f"edition-{edition}", None
    return None, None
