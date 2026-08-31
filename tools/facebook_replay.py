"""Re-classify a saved philhacks scrape without touching Facebook.

`data/facebook-backfill.json` is a real run: 27 posts, 139 comments, and the kind each
one was given at the time. Replaying it is how filtering changes get measured against
real noise instead of against invented fixtures.

    $env:PYTHONPATH='src'
    python tools/facebook_replay.py            # kind histogram, before vs after
    python tools/facebook_replay.py --verbose  # per-post detail for anything reclassified
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from akaton.discovery.facebook_parse import clean_facebook_text, mention_kind  # noqa: E402

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
    if changed:
        print(f"\nreclassified ({len(changed)}):")
        for was, now, text in changed:
            print(f"  {was:10} -> {now:10}  {text}")
            if args.verbose:
                print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
