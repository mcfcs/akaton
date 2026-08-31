"""Telling one run of a recurring competition from the next.

A hackathon that ran in March and runs again in September is two events, two alerts and
two deadlines. Getting this wrong is silent in both directions: merged, the September
edition never alerts because the March row already did; split, the same event is
announced twice. Both failures are invisible without going and looking.

Three shapes of the problem were verified against the real matcher before this existed:

    same landing page, March vs September  -> MERGE 100   ('same canonical_url')
    92-day gap, same organiser, new URL    -> POSSIBLE_DUPLICATE, terminal and silent
    September page with no parsed date     -> POSSIBLE_DUPLICATE, same silent death
    184-day gap, different URL             -> SEPARATE, correct

The first is the common case, because government and university sites reuse a landing
page as a matter of course.

Everything here answers "do these two positively disagree?", never "do they differ?".
Absence of evidence is not disagreement: most pages have no trustworthy date at all, and
a rule phrased as "differ" would split every one of those from its own next update.
"""

from __future__ import annotations

from datetime import timedelta

from akaton.domain.models import DateFact

# Two starts further apart than this are different runs. A competition does not move its
# own start date by six weeks; a genuine postponement arrives as EventPhase.POSTPONED on
# the same snapshot lineage, not as a fresh page. If this misfires the cost is a
# duplicate event rather than a missed one, which is the direction to err in.
EDITION_GAP_DAYS = 45

# The same test verifier.deadline_past already applies before it will call a deadline
# expired: a low-confidence date, or one whose year was inferred from context rather than
# read off the page, is not evidence of anything.
TRUSTWORTHY_CONFIDENCE = 0.8


def is_trustworthy(fact: DateFact | None) -> bool:
    """Whether a parsed date may be used to rule two events apart."""
    return bool(
        fact and fact.value and fact.confidence >= TRUSTWORTHY_CONFIDENCE and not fact.year_inferred
    )


def editions_conflict(left: str | None, right: str | None) -> bool:
    """True only when two edition keys positively disagree.

    Keys are hierarchical: "2026" is the same edition as "2026-03" seen more precisely,
    so a coarse key is treated as a prefix of a finer one. This is not a nicety. Stored
    rows carry year-granularity keys, and a raw `!=` would declare every one of them a
    different edition from its own next update the moment that update parsed a month.

        ("2026",    "2026-09")  -> False   the same edition, known better
        ("2026-03", "2026-09")  -> True    March and September
        ("2026",    "2027")     -> True
        (None,      "2026-09")  -> False   nothing to disagree with
    """
    if not left or not right or left == right:
        return False
    parts_left, parts_right = left.split("-"), right.split("-")
    shared = min(len(parts_left), len(parts_right))
    return parts_left[:shared] != parts_right[:shared]


def dates_contradict(left: DateFact | None, right: DateFact | None) -> bool:
    """True when both sides have a trustworthy start and they are runs apart.

    Requiring *both* sides is what keeps this safe. A Facebook announcement often carries
    a deadline and no start date at all; reading that absence as disagreement would break
    the collapse of three reposts into one alert.
    """
    if not (is_trustworthy(left) and is_trustworthy(right)):
        return False
    return abs(left.value - right.value) > timedelta(days=EDITION_GAP_DAYS)
