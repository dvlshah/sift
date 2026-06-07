"""Locked site fixtures for the eval bench.

Each fixture pins:
  * a use case (one of six business categories)
  * a slug + host
  * the discovery method that worked in the v1.0.0 bench (sitemap vs firecrawl)
  * the source argument for that method
  * a curated `reference_urls` list — these are the pages the eval bench
    expects to be present in the published index, so it can grade extraction
    quality against known-good URLs rather than against whatever happened to
    land in a random --limit 25 sample.

The reference URLs were curated from the v1.0.0 bench run results so they
represent "what a typical sift user would care about for this use case."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


DiscoveryMethod = Literal["sitemap", "auto-sitemap", "firecrawl"]
# - "sitemap":      explicit sitemap URL via ``sift seed --from-sitemap``.
# - "auto-sitemap": let sift discover sitemaps from robots.txt + well-known
#                   paths via ``sift seed --from-domain``. Use when the site
#                   has a sitemap that isn't at the conventional location
#                   (e.g. canada.ca, where the per-section /sitemap.xml
#                   exists but the root one redirects).
# - "firecrawl":    fall back to Firecrawl /v2/map when no sitemap exists.


@dataclass(frozen=True)
class SiteFixture:
    use_case: str
    slug: str
    host: str
    discovery: DiscoveryMethod
    source: str
    reference_urls: tuple[str, ...] = field(default_factory=tuple)
    # Per-use-case quality patterns the extract eval should look for. Examples:
    # for tax docs, currency amounts; for legal, section refs. None means
    # "use generic structural metrics only."
    use_case_patterns: tuple[str, ...] = field(default_factory=tuple)


# 24 positive-case fixtures — 4 per use case, expanded from the v1.0.0
# 12-site bench. Selection driven by Google Trends and 2026 agent-traffic data:
# Stripe/OpenAI/Anthropic docs dominate "coding agent" reads; the J5 set
# (ATO, CRA, FIOD, HMRC, IRS) anchors tax; the Free Access to Law Movement
# (AustLII, Cornell LII, CanLII, BAILII via WorldLII) + EUR-Lex anchors
# legal; SaaS help centers per Mintlify's "State of Agent Traffic" report;
# changelog pages by 2026 release-velocity signal; handbooks led by the
# GitLab/Basecamp/PostHog public set. Discovery method per site was
# probed live — sitemap.xml where it 200s, Firecrawl /v2/map elsewhere.
POSITIVE_FIXTURES: tuple[SiteFixture, ...] = (
    # ========================================================================
    # 1. Coding agents — dev docs (40-45% of docs traffic now from AI agents)
    # ========================================================================
    SiteFixture(
        use_case="coding-agents",
        slug="python-docs",
        host="docs.python.org",
        discovery="firecrawl",
        source="https://docs.python.org",
        reference_urls=(
            "https://docs.python.org/3/library/python.html",
            "https://docs.python.org/3/library/csv.html",
            "https://docs.python.org/3/library/readline.html",
        ),
        use_case_patterns=(r"```", r"^def \w+\(", r"\bclass \w+\b"),
    ),
    SiteFixture(
        use_case="coding-agents",
        slug="mdn",
        host="developer.mozilla.org",
        discovery="sitemap",
        source="https://developer.mozilla.org/sitemap.xml",
        reference_urls=(
            "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
            "https://developer.mozilla.org/en-US/docs/Web/HTTP",
            "https://developer.mozilla.org/en-US/docs/Web/CSS",
        ),
        use_case_patterns=(r"```", r"<\w+>", r"@\w+"),
    ),
    # Stripe — the docs site most read by AI agents per Stripe's own
    # statement (agent reads ~10× human reads by EOY 2026).
    SiteFixture(
        use_case="coding-agents",
        slug="stripe-docs",
        host="docs.stripe.com",
        discovery="sitemap",
        source="https://docs.stripe.com/sitemap.xml",
        reference_urls=(
            "https://docs.stripe.com/api",
            "https://docs.stripe.com/payments",
            "https://docs.stripe.com/billing",
        ),
        use_case_patterns=(r"```\w*", r"\bPOST /v1/\w+", r"\bcurl\s+"),
    ),
    # Anthropic / Claude API — second-tier agent-traffic darling.
    # docs.anthropic.com migrated to platform.claude.com in 2026; the new
    # host serves the same content tree under the same /en/ prefix.
    SiteFixture(
        use_case="coding-agents",
        slug="anthropic-docs",
        host="platform.claude.com",
        discovery="auto-sitemap",
        source="https://platform.claude.com",
        reference_urls=(
            "https://platform.claude.com/en/api/overview",
            "https://platform.claude.com/en/docs/build-with-claude/prompt-caching",
            "https://platform.claude.com/en/docs/agents-and-tools/tool-use",
        ),
        use_case_patterns=(r"```\w*", r"claude-\w+(?:-\d+)?", r"\bx-api-key\b"),
    ),

    # ========================================================================
    # 2. Tax & compliance — J5 anchor + OECD international
    # ========================================================================
    SiteFixture(
        use_case="tax-compliance",
        slug="ato",
        host="www.ato.gov.au",
        discovery="sitemap",
        source="https://www.ato.gov.au/sitemap.xml",
        reference_urls=(
            "https://www.ato.gov.au/individuals-and-families",
            "https://www.ato.gov.au/businesses-and-organisations",
            "https://www.ato.gov.au/about-ato",
        ),
        use_case_patterns=(r"\$[\d,]+(?:\.\d{2})?", r"\b20\d{2}[-–]\d{2}\b", r"\bGST\b"),
    ),
    SiteFixture(
        use_case="tax-compliance",
        slug="irs",
        host="www.irs.gov",
        discovery="sitemap",
        source="https://www.irs.gov/sitemap.xml",
        reference_urls=(
            "https://www.irs.gov/individuals",
            "https://www.irs.gov/businesses",
            "https://www.irs.gov/forms-instructions",
        ),
        use_case_patterns=(r"\$[\d,]+", r"\bForm \w+\b", r"\bIRS\b"),
    ),
    # Canada — CRA via canada.ca. canada.ca publishes a sitemap index from
    # robots.txt that fans out across all GoC departments; the auto-sitemap
    # walker handles the index + per-language children correctly. (v1.0
    # used Firecrawl because the root /sitemap.xml is not the right entry
    # point — the actual index is announced in robots.txt.)
    SiteFixture(
        use_case="tax-compliance",
        slug="cra",
        host="www.canada.ca",
        discovery="auto-sitemap",
        source="https://www.canada.ca",
        reference_urls=(
            "https://www.canada.ca/en/revenue-agency/services/forms-publications.html",
            "https://www.canada.ca/en/revenue-agency/services/tax/individuals.html",
            "https://www.canada.ca/en/revenue-agency/services/tax/businesses.html",
        ),
        use_case_patterns=(r"\$[\d,]+(?:\.\d{2})?", r"\bCRA\b", r"\bT\d+\b"),
    ),
    # OECD — international tax framework (sitemap 403 → firecrawl)
    SiteFixture(
        use_case="tax-compliance",
        slug="oecd-tax",
        host="www.oecd.org",
        discovery="firecrawl",
        source="https://www.oecd.org/tax",
        reference_urls=(
            "https://www.oecd.org/tax/transfer-pricing/",
            "https://www.oecd.org/tax/beps/",
            "https://www.oecd.org/tax/tax-policy/",
        ),
        use_case_patterns=(r"\bOECD\b", r"\bBEPS\b", r"\bDAC\d+\b"),
    ),

    # ========================================================================
    # 3. Legal & standards — Free Access to Law Movement + EU + std bodies
    # ========================================================================
    SiteFixture(
        use_case="legal-standards",
        slug="rfc-editor",
        host="www.rfc-editor.org",
        discovery="sitemap",
        source="https://www.rfc-editor.org/sitemap.xml",
        reference_urls=(
            "https://www.rfc-editor.org/info/rfc7231",
            "https://www.rfc-editor.org/info/rfc8446",
            "https://www.rfc-editor.org/info/rfc9110",
        ),
        use_case_patterns=(r"\bRFC\s?\d+\b", r"\bSection \d+(?:\.\d+)*\b",
                           r"\b(?:MUST|SHOULD|MAY|SHALL)\b"),
    ),
    SiteFixture(
        use_case="legal-standards",
        slug="w3c",
        host="www.w3.org",
        discovery="firecrawl",
        source="https://www.w3.org",
        reference_urls=(
            "https://www.w3.org/TR/pointerevents/",
            "https://www.w3.org/TR/css-display-3/",
            "https://www.w3.org/WAI/standards-guidelines/wcag/",
        ),
        use_case_patterns=(r"\bW3C\b", r"\[\[[A-Z][A-Z0-9-]+\]\]",
                           r"\bnormative\b|\bnon-normative\b"),
    ),
    # Cornell LII — canonical free-access US legal source
    SiteFixture(
        use_case="legal-standards",
        slug="cornell-lii",
        host="www.law.cornell.edu",
        discovery="firecrawl",            # /sitemap.xml is 404
        source="https://www.law.cornell.edu",
        reference_urls=(
            "https://www.law.cornell.edu/uscode/text/26",
            "https://www.law.cornell.edu/cfr/text",
            "https://www.law.cornell.edu/wex",
        ),
        use_case_patterns=(r"§\s?\d+", r"\b\d+\s?U\.?S\.?C\.?\b",
                           r"\bsubsection\b|\bparagraph\b"),
    ),
    # EUR-Lex — EU legal portal
    SiteFixture(
        use_case="legal-standards",
        slug="eur-lex",
        host="eur-lex.europa.eu",
        discovery="sitemap",
        source="https://eur-lex.europa.eu/sitemap.xml",
        reference_urls=(
            "https://eur-lex.europa.eu/homepage.html",
            "https://eur-lex.europa.eu/collection/eu-law/treaties.html",
            "https://eur-lex.europa.eu/collection/eu-law/legislation.html",
        ),
        # CELEX IDs (e.g. 32016R0679 for GDPR), Directive/Regulation refs
        use_case_patterns=(r"\b3\d{4}[A-Z]\d{4}\b",                      # CELEX
                           r"\b(?:Directive|Regulation)\s\(?[A-Z]+\)?\s?20\d{2}/\d+\b",
                           r"\bArticle\s\d+(?:\(\d+\))?\b"),

    ),

    # ========================================================================
    # 4. Support & policy bots — SaaS help centers (45% agent traffic)
    # ========================================================================
    SiteFixture(
        use_case="support-policy",
        slug="github-docs",
        host="docs.github.com",
        discovery="firecrawl",
        source="https://docs.github.com",
        reference_urls=(
            "https://docs.github.com/en/actions",
            "https://docs.github.com/en/rest",
            "https://docs.github.com/en/copilot",
        ),
        use_case_patterns=(r"```\w*", r"^\$ ", r"\bgithub\.com\b"),
    ),
    SiteFixture(
        use_case="support-policy",
        slug="shopify-help",
        host="help.shopify.com",
        discovery="firecrawl",            # needs --firecrawl-fallback at fetch too
        source="https://help.shopify.com",
        reference_urls=(
            "https://help.shopify.com/en/manual/payments/shopify-payments",
            "https://help.shopify.com/en/manual/checkout-settings",
            "https://help.shopify.com/en/manual/markets",
        ),
        use_case_patterns=(r"\bShopify\b", r"\b(?:USD|EUR|CAD|GBP)\b"),
    ),
    # Atlassian (Jira/Confluence) — major SaaS help-center surface
    SiteFixture(
        use_case="support-policy",
        slug="atlassian-support",
        host="support.atlassian.com",
        discovery="sitemap",
        source="https://support.atlassian.com/sitemap.xml",
        reference_urls=(
            "https://support.atlassian.com/jira-cloud/",
            "https://support.atlassian.com/confluence-cloud/",
            "https://support.atlassian.com/bitbucket-cloud/",
        ),
        use_case_patterns=(r"\b(?:Jira|Confluence|Bitbucket)\b",
                           r"\b(?:admin|workspace|user)\b",
                           r"\bAtlassian\b"),
    ),
    # Notion help — the canonical product-led-growth help center
    SiteFixture(
        use_case="support-policy",
        slug="notion-help",
        host="www.notion.com",
        discovery="sitemap",
        source="https://www.notion.com/sitemap.xml",
        reference_urls=(
            "https://www.notion.com/help/category/account-and-settings",
            "https://www.notion.com/help/category/databases",
            "https://www.notion.com/help/category/sharing-and-collaboration",
        ),
        use_case_patterns=(r"\bNotion\b", r"\b(?:database|page|workspace|block)\b"),
    ),

    # ========================================================================
    # 5. Change monitoring — high-velocity release-notes / changelogs
    # ========================================================================
    SiteFixture(
        use_case="change-monitor",
        slug="kubernetes",
        host="kubernetes.io",
        discovery="sitemap",
        source="https://kubernetes.io/sitemap.xml",
        reference_urls=(
            "https://kubernetes.io/docs/concepts/",
            "https://kubernetes.io/docs/tutorials/",
            "https://kubernetes.io/docs/reference/",
        ),
        use_case_patterns=(r"```\w*", r"\bkubectl\b", r"\bv\d+\.\d+(?:\.\d+)?\b"),
    ),
    SiteFixture(
        use_case="change-monitor",
        slug="docker-docs",
        host="docs.docker.com",
        discovery="sitemap",
        source="https://docs.docker.com/sitemap.xml",
        reference_urls=(
            "https://docs.docker.com/get-started/",
            "https://docs.docker.com/engine/",
            "https://docs.docker.com/compose/",
        ),
        use_case_patterns=(r"```\w*", r"\bdocker\b", r"\bcompose\b"),
    ),
    # Vercel — canonical "compact changelog page" pattern
    SiteFixture(
        use_case="change-monitor",
        slug="vercel",
        host="vercel.com",
        discovery="sitemap",
        source="https://vercel.com/sitemap.xml",   # follows 301
        reference_urls=(
            "https://vercel.com/changelog",
            "https://vercel.com/docs",
            "https://vercel.com/docs/edge-network",
        ),
        # changelog dates, version tags
        use_case_patterns=(r"\b20\d{2}-\d{2}-\d{2}\b",
                           r"\bv?\d+\.\d+\.\d+\b",
                           r"\bdeploy(?:ment|s)?\b"),
    ),
    # OpenAI developers changelog — agent-relevant API churn
    SiteFixture(
        use_case="change-monitor",
        slug="openai-changelog",
        host="developers.openai.com",
        discovery="sitemap",
        source="https://developers.openai.com/sitemap-index.xml",   # from robots.txt
        reference_urls=(
            "https://developers.openai.com/changelog/",
            "https://developers.openai.com/api/docs/changelog",
            "https://developers.openai.com/api/docs",
        ),
        use_case_patterns=(r"\bgpt-[\w-]+\b", r"```\w*", r"\bv\d+(?:\.\d+)*\b"),
    ),

    # ========================================================================
    # 6. Internal knowledge — public handbooks (operator pattern)
    # ========================================================================
    SiteFixture(
        use_case="internal-kb",
        slug="gitlab-hb",
        host="handbook.gitlab.com",
        discovery="sitemap",
        source="https://handbook.gitlab.com/sitemap.xml",
        reference_urls=(
            "https://handbook.gitlab.com/handbook/engineering/",
            "https://handbook.gitlab.com/handbook/values/",
            "https://handbook.gitlab.com/handbook/people-group/",
        ),
        use_case_patterns=(r"\bGitLab\b", r"#\s\w+"),
    ),
    SiteFixture(
        use_case="internal-kb",
        slug="hashi-dev",
        host="developer.hashicorp.com",
        discovery="sitemap",
        source="https://developer.hashicorp.com/sitemap.xml",
        reference_urls=(
            "https://developer.hashicorp.com/terraform",
            "https://developer.hashicorp.com/vault",
            "https://developer.hashicorp.com/consul",
        ),
        use_case_patterns=(r"```\w*", r"\bHashiCorp\b",
                           r"\bv\d+\.\d+\.\d+\b"),
    ),
    # about.gitlab.com — the 3000-page main handbook (separate from
    # handbook.gitlab.com which is org-only content)
    SiteFixture(
        use_case="internal-kb",
        slug="gitlab-about",
        host="about.gitlab.com",
        discovery="sitemap",
        source="https://about.gitlab.com/sitemap.xml",
        reference_urls=(
            "https://about.gitlab.com/handbook/",
            "https://about.gitlab.com/handbook/leadership/",
            "https://about.gitlab.com/handbook/finance/",
        ),
        use_case_patterns=(r"\bGitLab\b", r"^#{1,3}\s", r"\bDRI\b"),
    ),
    # PostHog handbook — the canonical startup-handbook reference
    SiteFixture(
        use_case="internal-kb",
        slug="posthog-handbook",
        host="posthog.com",
        discovery="firecrawl",            # /sitemap.xml is 404
        source="https://posthog.com/handbook",
        reference_urls=(
            "https://posthog.com/handbook/company",
            "https://posthog.com/handbook/engineering",
            "https://posthog.com/handbook/product",
        ),
        use_case_patterns=(r"\bPostHog\b", r"```\w*", r"^#{1,3}\s"),
    ),
)


# Helpers --------------------------------------------------------------------

def by_use_case(use_case: str) -> tuple[SiteFixture, ...]:
    return tuple(f for f in POSITIVE_FIXTURES if f.use_case == use_case)


def by_slug(slug: str) -> SiteFixture | None:
    for f in POSITIVE_FIXTURES:
        if f.slug == slug:
            return f
    return None


USE_CASES: tuple[str, ...] = tuple({f.use_case for f in POSITIVE_FIXTURES})
