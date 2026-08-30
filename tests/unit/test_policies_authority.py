from __future__ import annotations

from akaton.fetch.policies import DomainPolicyResolver
from akaton.processing.authority import authority_for_url

BLOCKED = {
    "default": {"requests_per_minute": 6},
    "domains": {
        "facebook.com": {"fetch": "disabled", "browser": "disabled", "proxy": "never"},
        "eventbrite.com": {"requests_per_minute": 4},
        "gov.ph": {"match": "suffix", "requests_per_minute": 3},
        "strict.example": {"match": "exact", "requests_per_minute": 1},
    },
}


def test_blocked_domain_also_covers_www_subdomain():
    """Search results are almost always www.*, so an exact-only rule blocks nothing."""
    resolver = DomainPolicyResolver(BLOCKED)
    for url in (
        "https://facebook.com/events/1",
        "https://www.facebook.com/events/1",
        "https://web.facebook.com/events/1",
    ):
        assert resolver.for_url(url).fetch == "disabled", url


def test_subdomain_inherits_rate_limit():
    resolver = DomainPolicyResolver(BLOCKED)
    assert resolver.for_url("https://www.eventbrite.com/e/1").requests_per_minute == 4
    assert resolver.for_url("https://dro10.depdev.gov.ph/x").requests_per_minute == 3


def test_exact_match_still_excludes_subdomains():
    resolver = DomainPolicyResolver(BLOCKED)
    assert resolver.for_url("https://strict.example/x").requests_per_minute == 1
    assert resolver.for_url("https://sub.strict.example/x").requests_per_minute == 6


def test_unlisted_domain_uses_default_policy():
    resolver = DomainPolicyResolver(BLOCKED)
    policy = resolver.for_url("https://hackathons.example/x")
    assert policy.fetch == "enabled"
    assert policy.requests_per_minute == 6


def test_configured_platform_clears_the_authority_gate():
    sources = {"organizers": [], "platforms": {"hackathons.ph": 70}}
    assert authority_for_url("https://hackathons.ph/list", sources) == 70
    assert authority_for_url("https://www.hackathons.ph/list", sources) == 70
    # An unlisted site stays third_party, which the verifier rejects on its own.
    assert authority_for_url("https://unknown.example/x", sources) == 50


def test_organizer_domain_outranks_platform_list():
    sources = {
        "organizers": [{"name": "DICT", "domains": ["dict.gov.ph"], "authority": 90}],
        "platforms": {"gov.ph": 60},
    }
    assert authority_for_url("https://dict.gov.ph/news", sources) == 90
