"""Re-classify a saved philhacks scrape without touching Facebook.

`data/facebook-backfill.json` is a real run: 27 posts, 139 comments, and the kind each
one was given at the time. Replaying it is how filtering changes get measured against
real noise instead of against invented fixtures.

    $env:PYTHONPATH='src'
    python tools/facebook_replay.py            # kind histogram, and the leads it produces
    python tools/facebook_replay.py --verbose  # per-post detail, and each lead's excerpt
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from akaton.config import load_config  # noqa: E402
from akaton.discovery.facebook_parse import clean_facebook_text, mention_kind  # noqa: E402
from akaton.processing.authority import organizer_vocabulary  # noqa: E402
from akaton.processing.leads import lead_key  # noqa: E402
from akaton.processing.mentions import LEAD_KINDS, build_mention  # noqa: E402

DUMP = ROOT / "data" / "facebook-backfill.json"


def _classify(text: str, urls: list[str]) -> str:
    # The dump stores raw scraped text, so run it through the same cleaner the live
    # adapter uses. That is what applies the Unicode folding.
    return mention_kind(clean_facebook_text(text), urls)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dump", type=Path, default=DUMP)
    args = parser.parse_args()

    if not args.dump.exists():
        print(f"No scrape at {args.dump}. Run tools/facebook_backfill.py first.")
        return 2

    data = json.loads(args.dump.read_text(encoding="utf-8"))
    posts = data.get("posts", [])
    before = collections.Counter()
    after = collections.Counter()
    changed: list[tuple[str, str, str]] = []

    for post in posts:
        was = post.get("kind", "?")
        now = _classify(post.get("text") or "", post.get("urls") or [])
        before[was] += 1
        after[now] += 1
        if was != now:
            changed.append((was, now, (post.get("text") or "").replace("\n", " ")[:88]))

    comment_before = collections.Counter()
    comment_after = collections.Counter()
    for post in posts:
        for comment in post.get("comments", []):
            comment_before[comment.get("kind", "?")] += 1
            comment_after[_classify(comment.get("text") or "", comment.get("urls") or [])] += 1

    print(f"scrape: {args.dump.name}  posts={len(posts)}  comments={sum(comment_before.values())}")
    truncated = sum(1 for post in posts if len(post.get("text") or "") == 500)
    if truncated:
        print(
            f"WARNING: {truncated} posts are stored at exactly 500 characters, so this dump "
            "predates the full-text change and replayed counts understate 'event'.\n"
            "         Re-run tools/facebook_backfill.py for numbers that match a live run."
        )
    print(f"\n{'kind':22} {'before':>7} {'now':>7}")
    for kind in sorted(set(before) | set(after)):
        print(f"  posts {kind:15} {before.get(kind, 0):>7} {after.get(kind, 0):>7}")
    for kind in sorted(set(comment_before) | set(comment_after)):
        print(f"  reply {kind:15} {comment_before.get(kind, 0):>7} {comment_after.get(kind, 0):>7}")

    print(f"\nposts that would alert: {before.get('event', 0)} -> {after.get('event', 0)}")
    _report_leads(posts, verbose=args.verbose)
    if changed:
        print(f"\nreclassified ({len(changed)}):")
        for was, now, text in changed:
            print(f"  {was:10} -> {now:10}  {text}")
            if args.verbose:
                print()
    return 0


def _report_leads(posts: list[dict], *, verbose: bool) -> None:
    """What this scrape would put in the leads table, and what it would cost.

    A lead is one search, however many people asked. The distinct count is the number of
    requests the run would spend; the sighting counts show what the deduplication saved.
    """
    vocabulary = organizer_vocabulary(load_config(ROOT).sources)
    leads: dict[str, dict] = {}
    unnamed = 0
    for post in posts:
        parts = [(post.get("text") or "", post.get("urls") or [], "post")]
        parts += [
            (comment.get("text") or "", comment.get("urls") or [], "reply")
            for comment in post.get("comments", [])
        ]
        for text, urls, where in parts:
            body = clean_facebook_text(text)
            kind = mention_kind(body, urls)
            if kind not in LEAD_KINDS:
                continue
            mention = build_mention(
                body,
                kind=kind,
                platform="facebook",
                source_url=post.get("permalink") or "",
                vocabulary=vocabulary,
            )
            if not mention:
                unnamed += 1
                continue
            key = lead_key(mention.normalized_name, mention.edition_hint)
            entry = leads.setdefault(
                key, {"mention": mention, "sightings": 0, "where": collections.Counter()}
            )
            entry["sightings"] += 1
            entry["where"][where] += 1

    sightings = sum(entry["sightings"] for entry in leads.values())
    print(f"\nleads: {len(leads)} distinct, {sightings} sightings")
    print(f"       {unnamed} mentions named nothing searchable and cost nothing")
    for entry in sorted(leads.values(), key=lambda e: -e["sightings"]):
        mention = entry["mention"]
        where = ", ".join(f"{count} {name}" for name, count in entry["where"].most_common())
        print(f"  {entry['sightings']}x  {mention.query!r:<40} ({where})")
        if verbose:
            print(f"       {mention.excerpt[:96]}")


if __name__ == "__main__":
    raise SystemExit(main())
