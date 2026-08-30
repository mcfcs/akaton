from __future__ import annotations

from urllib.parse import urlsplit

from akaton.processing.authority import authority_for_url
from akaton.processing.normalize import is_registration_url, normalize_url


def choose_urls(
    requested_url: str,
    final_url: str | None,
    links: list[str],
    metadata: dict,
    sources: dict,
) -> tuple[str, str | None]:
    candidates = [requested_url]
    if final_url:
        candidates.append(final_url)
    for key in ("canonical", "og:url", "url"):
        if metadata.get(key):
            candidates.append(str(metadata[key]))
    candidates.extend(links)
    normalized = list(
        dict.fromkeys(
            normalize_url(url) for url in candidates if url.startswith(("http://", "https://"))
        )
    )
    registration = next((url for url in normalized if is_registration_url(url)), None)

    def score(url: str) -> tuple[int, int, int]:
        authority = authority_for_url(url, sources)
        path = urlsplit(url).path.strip("/")
        event_specific = 1 if path else 0
        registration_penalty = -1 if is_registration_url(url) else 0
        return authority, event_specific, registration_penalty

    non_registration = [url for url in normalized if not is_registration_url(url)] or normalized
    canonical = (
        max(non_registration, key=score)
        if non_registration
        else normalize_url(final_url or requested_url)
    )
    return canonical, registration
