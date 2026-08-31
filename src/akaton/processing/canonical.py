from __future__ import annotations

from urllib.parse import urlsplit

from akaton.processing.links import is_trusted_registration_url
from akaton.processing.normalize import is_registration_url, normalize_url


def choose_urls(
    requested_url: str,
    final_url: str | None,
    links: list[str],
    metadata: dict,
    *,
    sources: dict | None = None,
) -> tuple[str, str | None]:
    """Pick the event's canonical URL and, if present, a registration link.

    The canonical URL always identifies the fetched document: the URL we landed on, or a
    same-site `canonical`/`og:url` it declares for itself. Outbound links are only ever
    considered for the registration URL. Scoring arbitrary page links by authority would
    let the Facebook or Instagram link in a site's footer outrank the page itself, because
    social domains carry higher default authority than an unknown third-party site.
    """
    self_url = normalize_url(final_url or requested_url)
    declared = [
        normalize_url(str(metadata[key]))
        for key in ("canonical", "og:url", "url")
        if metadata.get(key) and str(metadata[key]).startswith(("http://", "https://"))
    ]
    canonical = next((url for url in declared if _is_usable_canonical(url, self_url)), self_url)

    link_candidates = list(
        dict.fromkeys(
            normalize_url(url)
            for url in (requested_url, final_url, *declared, *links)
            if url and str(url).startswith(("http://", "https://"))
        )
    )
    # With a sources config the host is checked too, so a scraped `/register` path on an
    # unknown host cannot become the alert's clickable Register button. Without one this
    # stays a pure path-shape test, which is all the URL-only callers need.
    accept = (
        (lambda url: is_trusted_registration_url(url, sources))
        if sources is not None
        else is_registration_url
    )
    registration = next((url for url in link_candidates if accept(url)), None)
    return canonical, registration


def _is_usable_canonical(declared: str, self_url: str) -> bool:
    """Accept a page's declared canonical only when it still identifies this page.

    A site-wide root canonical on a deep page is a CMS default rather than a real
    declaration, and following it collapses every event on the site onto the homepage.
    """
    if not _same_site(declared, self_url):
        return False
    declared_path = urlsplit(declared).path.strip("/")
    self_path = urlsplit(self_url).path.strip("/")
    return bool(declared_path) or not self_path


def _same_site(url: str, other: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold()
    other_host = (urlsplit(other).hostname or "").casefold()
    if not host or not other_host:
        return False
    return host == other_host or host.endswith(f".{other_host}") or other_host.endswith(f".{host}")
