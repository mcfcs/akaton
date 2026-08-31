"""Tell a Philippine competition from a neighbouring country's.

The philhacks group carries reposts from the wider region. A real run pulled in two
PLANMalaysia announcements written in Malay ("JOM SERTAI URBANMIND AI CHALLENGE 2026:
KATEGORI ORANG AWAM"), which a Philippine participant cannot enter.

Language alone is not enough: Malay and Tagalog share a great deal of Austronesian
vocabulary, and Taglish posts borrow English freely. So a single explicit foreign place
name is decisive, while language markers need corroboration before they are believed.
"""

from __future__ import annotations

from akaton.processing.normalize import fold_text

PH_TERMS = (
    "philippines",
    "philippine",
    "filipino",
    "filipinas",
    "pilipinas",
    "pilipino",
    "metro manila",
)

# Place names that put an event outside the Philippines. One is enough.
FOREIGN_PLACE_TERMS = {
    "MY": (
        "malaysia",
        "kuala lumpur",
        "putrajaya",
        "selangor",
        "johor",
        "penang",
        "sabah",
        "sarawak",
        "planmalaysia",
    ),
    "SG": ("singapore",),
    "ID": ("indonesia", "jakarta", "bandung", "surabaya"),
    "VN": ("vietnam", "hanoi", "ho chi minh"),
    "TH": ("thailand", "bangkok", "chiang mai"),
    "IN": ("bengaluru", "bangalore", "mumbai", "new delhi", "chennai", "hyderabad"),
    "PK": ("pakistan", "karachi", "lahore", "islamabad"),
}

# Malay words that are not also everyday Tagalog. "anda", "boleh" and "kami" are
# deliberately excluded because they overlap with Filipino usage.
MALAY_MARKERS = (
    "jom",
    "sertai",
    "kategori",
    "orang awam",
    "bersediakah",
    "kecerdasan buatan",
    "pertandingan",
    "peserta",
    "percuma",
    "negeri",
    "sila",
    "hadiah",
    "penyertaan",
    "anjuran",
    "masa depan",
)
MALAY_MARKER_THRESHOLD = 2


def mentions_philippines(text: str) -> bool:
    lowered = fold_text(text).casefold()
    return any(term in lowered for term in PH_TERMS)


def detect_country(text: str) -> str | None:
    """Best-effort country for a social post, or None when nothing indicates one.

    A Philippine signal always wins: a post naming both Manila and Singapore is most
    likely a Philippine event mentioning a regional partner.
    """
    lowered = fold_text(text).casefold()
    if any(term in lowered for term in PH_TERMS):
        return "PH"
    for country, terms in FOREIGN_PLACE_TERMS.items():
        if any(term in lowered for term in terms):
            return country
    hits = {marker for marker in MALAY_MARKERS if marker in lowered}
    if len(hits) >= MALAY_MARKER_THRESHOLD:
        return "MY"
    return None
