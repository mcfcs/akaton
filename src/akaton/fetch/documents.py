from __future__ import annotations

import hashlib
import io
import json
from urllib.parse import urljoin

import trafilatura
from bs4 import BeautifulSoup
from pypdf import PdfReader


def hash_content(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_html(content: bytes, base_url: str) -> tuple[str | None, str, list[str], dict]:
    html = content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    metadata: dict = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        value = tag.get("content")
        if key and value:
            metadata[str(key).casefold()] = str(value)
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        metadata["canonical"] = urljoin(base_url, canonical["href"])
    json_ld: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(script.get_text())
            json_ld.extend(value if isinstance(value, list) else [value])
        except (json.JSONDecodeError, TypeError):
            continue
    if json_ld:
        metadata["json_ld"] = json_ld[:20]
        for item in json_ld:
            if isinstance(item, dict) and str(item.get("@type", "")).casefold() == "event":
                metadata["event_json_ld"] = item
                title = title or item.get("name")
                break
    extracted = trafilatura.extract(
        html, include_links=False, include_tables=True, favor_recall=True
    )
    text = extracted or soup.get_text("\n", strip=True)
    links = []
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(base_url, anchor["href"])
        if absolute.startswith(("http://", "https://")):
            links.append(absolute)
    return title, text, list(dict.fromkeys(links))[:500], metadata


def extract_pdf(content: bytes) -> tuple[str | None, str, list[str], dict]:
    reader = PdfReader(io.BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    title = None
    metadata: dict = {"pages": len(reader.pages)}
    if reader.metadata:
        title = reader.metadata.title
        metadata["pdf_metadata"] = {
            str(key): str(value) for key, value in reader.metadata.items() if value
        }
    return title, text, [], metadata
