"""How much a scraped link may be trusted in an alert.

Links harvested from a social thread are attacker-influenced: anyone can reply to a
public group with a shortener. A real philhacks run collected `bit.ly`, `sk-qr.com` and
`jollibee.onelink.me` from spam replies alongside genuine `forms.office.com` and
`luma.com` registration links, and `is_registration_url` matches `/register` on *any*
host, so a hostile URL could have become the alert's clickable "Register" button.

Three outcomes:

- `CLICKABLE` — rendered as a markdown link. Only hosts we already trust elsewhere.
- `PLAIN`     — shown, but never linkified. Unknown hosts and shorteners land here, so
                nothing is hidden and nothing is endorsed.
- `DROP`      — never shown. Platform chrome and dead ends.

Nothing here resolves a URL. Following a shortener to decide how to render it would be a
network request to an attacker-chosen host from the alert path, and the SSRF guard in
`fetch.safety` only runs inside `FetchManager`. A shortener that arrives as a *seed* URL
is fetched normally, guard and domain policy included, and judged on its final URL.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from akaton.domain.enums import LinkTrust
from akaton.processing.authority import authority_for_url
from akaton.processing.normalize import is_registration_url

# Link shorteners and QR redirectors. The destination is unknown until it is visited, so
# these are never clickable and never eligible to be the registration URL.
SHORTENER_HOSTS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "is.gd",
    "ow.ly",
    "cutt.ly",
    "rb.gy",
    "rebrand.ly",
    "shorturl.at",
    "s.id",
    "lnkd.in",
    "linktr.ee",
    "lnk.bio",
    "onelink.me",
    "sk-qr.com",
    "qr.link",
}

# Form and ticketing hosts. Real registration lives here, and the host itself is
# reputable even though the individual form is user-created.
FORM_HOSTS = {
    "forms.gle",
    "docs.google.com",
    "forms.office.com",
    "forms.microsoft.com",
    "typeform.com",
    "tally.so",
    "jotform.com",
    "airtable.com",
    "eventbrite.com",
    "lu.ma",
    "luma.com",
}

# Facebook's own furniture: account centre, auth, settings, commerce. Never an event.
CHROME_HOSTS = {"accountscenter.facebook.com", "messenger.com", "www.messenger.com"}
CHROME_PATH_PREFIXES = (
    "/login",
    "/checkpoint",
    "/recover",
    "/security",
    "/settings",
    "/privacy",
    "/policies",
    "/help",
    "/notifications",
    "/friends",
    "/marketplace",
    "/watch",
    "/gaming",
)

# Platforms whose pages cannot be read anonymously. A Facebook /events/ page is the
# exception: that is the event itself, not chrome.
OPAQUE_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "www.tiktok.com",
}

TRUSTED_AUTHORITY = 60


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold()


def _matches(host: str, hosts: set[str]) -> bool:
    return any(host == entry or host.endswith(f".{entry}") for entry in hosts)


def is_shortener(url: str) -> bool:
    return _matches(host_of(url), SHORTENER_HOSTS)


def _is_facebook(host: str) -> bool:
    return host == "facebook.com" or host.endswith(".facebook.com") or host.endswith(".fbcdn.net")


def _is_chrome(url: str) -> bool:
    host = host_of(url)
    if _matches(host, CHROME_HOSTS) or host.endswith(".fbcdn.net"):
        return True
    if not _is_facebook(host):
        return False
    path = urlsplit(url).path.casefold()
    return any(path.startswith(prefix) for prefix in CHROME_PATH_PREFIXES)


def link_trust(url: str, sources: dict | None = None) -> LinkTrust:
    """Decide how a scraped URL may be presented."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return LinkTrust.DROP
    host = host_of(url)
    if not host:
        return LinkTrust.DROP
    if _is_chrome(url):
        return LinkTrust.DROP
    if _matches(host, OPAQUE_HOSTS):
        return LinkTrust.DROP
    if _is_facebook(host):
        # A group or post permalink is the source we are citing; an event page is the
        # event. Everything else on the domain is furniture.
        path = urlsplit(url).path.casefold()
        if "/events/" in path or "/groups/" in path or "/posts/" in path or "/permalink/" in path:
            return LinkTrust.CLICKABLE
        return LinkTrust.DROP
    if is_shortener(url):
        return LinkTrust.PLAIN
    # authority_for_url already knows every configured organizer, every `platforms:`
    # entry, and the restricted gov.ph/edu.ph suffixes, so trusting a new host stays a
    # config/sources.yaml edit rather than a code change.
    if authority_for_url(url, sources or {}) >= TRUSTED_AUTHORITY:
        return LinkTrust.CLICKABLE
    if _matches(host, FORM_HOSTS):
        return LinkTrust.CLICKABLE
    return LinkTrust.PLAIN


def is_trusted_registration_url(url: str, sources: dict | None = None) -> bool:
    """A registration link we are willing to put behind a "Register" button.

    `is_registration_url` is a path-shape test — it matches `/register` on any host,
    including `https://evil.example/register` — so the host has to be checked too.
    """
    return is_registration_url(url) and link_trust(url, sources) is LinkTrust.CLICKABLE
