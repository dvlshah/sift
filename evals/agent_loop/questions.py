"""Hand-curated questions for the agent-loop eval.

Each question pins:
  * ``qid``         — short stable id used in result joins
  * ``use_case``    — one of the 6 bench use cases (so the report can split lift by domain)
  * ``text``        — what the agent is asked
  * ``gold_urls``   — pages that contain the answer; the judge uses these as
                      the citation-faithfulness ground truth + the harness
                      asserts the URLs exist in the sift index before running
  * ``gold_answer`` — a 1-3 sentence reference; the judge compares the agent's
                      answer against this, not against a free-form rubric, so
                      grading stays low-variance run to run
  * ``fresh_sensitive`` — True when the right answer changes year-over-year
                      (tax rates, model releases). These are the questions
                      where sift's freshness story should dominate parametric
                      knowledge most cleanly.

Curation strategy: questions tie to v1.0-baseline published fixtures
(see ``evals/bench/results/v1.0-baseline-2026-05-31.json``); ~20 total,
spread across the 6 use cases. Mix factual lookups, multi-page synthesis,
and freshness-sensitive items so the per-question report is interpretable.

Avoided on purpose:
  * questions with answers Claude very likely memorized verbatim (would mask
    the retrieval signal — e.g. "what is HTTP 200?")
  * questions about pages we know are not in the corpus (would unfairly
    penalize sift-grep)
  * ambiguous, opinion-based, or list-style questions where the judge has
    nothing crisp to grade against
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Question:
    qid: str
    use_case: str
    text: str
    gold_urls: tuple[str, ...]
    gold_answer: str
    fresh_sensitive: bool = False
    notes: str = ""

    @property
    def gold_hosts(self) -> set[str]:
        out: set[str] = set()
        for u in self.gold_urls:
            out.add(u.split("//", 1)[-1].split("/", 1)[0].lower())
        return out


# ---- The 20-question set --------------------------------------------------

QUESTIONS: tuple[Question, ...] = (
    # ============================================================
    # Coding agents (4) — docs.python.org, developer.mozilla.org, docs.stripe.com
    # ============================================================
    Question(
        qid="py-pathlib-suffix",
        use_case="coding-agents",
        text="In Python's pathlib, what attribute returns the file extension "
             "of a Path (including the dot), and what does it return for a "
             "path with no extension?",
        gold_urls=(
            "https://docs.python.org/3/library/pathlib.html",
        ),
        gold_answer=("PurePath.suffix returns the final component's extension "
                     "including the leading dot, e.g. '.txt'. Returns an empty "
                     "string '' for paths with no extension."),
        fresh_sensitive=False,
    ),
    Question(
        qid="py-asyncio-gather-vs-taskgroup",
        use_case="coding-agents",
        text="In modern Python (3.11+), what is the recommended replacement "
             "for asyncio.gather() when you need structured concurrency with "
             "exception handling, and why is it preferred?",
        gold_urls=(
            "https://docs.python.org/3/library/asyncio-task.html",
        ),
        gold_answer=("asyncio.TaskGroup (Python 3.11+) is preferred because "
                     "it provides structured concurrency: if any task fails, "
                     "all other tasks in the group are cancelled, and "
                     "exceptions are collected into an ExceptionGroup rather "
                     "than silently lost."),
        fresh_sensitive=True,
        notes="3.11+ feature; older Claude training data may default to gather().",
    ),
    Question(
        qid="mdn-css-cascade-layers",
        use_case="coding-agents",
        text="What CSS at-rule was added to control the cascade order of "
             "rule groups independently of specificity, and how do later "
             "layer declarations interact with earlier ones?",
        gold_urls=(
            "https://developer.mozilla.org/en-US/docs/Web/CSS/@layer",
        ),
        gold_answer=("@layer creates explicit cascade layers. Rules in later-"
                     "declared layers override rules in earlier ones "
                     "regardless of specificity. Un-layered styles take "
                     "precedence over all layered styles."),
        fresh_sensitive=False,
    ),
    Question(
        qid="stripe-idempotency-key-header",
        use_case="coding-agents",
        text="What HTTP header does Stripe's API use for idempotent requests, "
             "and what's the recommended way to generate the value?",
        gold_urls=(
            "https://docs.stripe.com/api/idempotent_requests",
        ),
        gold_answer=("The Idempotency-Key request header. Stripe recommends "
                     "using a fresh UUID (v4) per logical operation; the same "
                     "key replayed within 24 hours returns the original "
                     "result without re-executing the request."),
        fresh_sensitive=False,
    ),

    # ============================================================
    # Tax & compliance (4) — ato.gov.au, irs.gov
    # ============================================================
    Question(
        qid="ato-gst-rate",
        use_case="tax-compliance",
        text="What is the current GST rate in Australia, and which broad "
             "categories of supplies are GST-free?",
        gold_urls=(
            "https://www.ato.gov.au/businesses-and-organisations/gst-excise-and-indirect-taxes/gst",
        ),
        gold_answer=("GST in Australia is 10%. Major GST-free categories "
                     "include most basic food, certain medical and health "
                     "services, education courses, childcare, and exports."),
        fresh_sensitive=False,
    ),
    Question(
        qid="ato-individual-return-due-date",
        use_case="tax-compliance",
        text="When is the due date for an Australian individual to lodge "
             "their own tax return (without a registered tax agent), and "
             "what financial year does that cover?",
        gold_urls=(
            "https://www.ato.gov.au/individuals-and-families/your-tax-return/how-to-lodge-your-tax-return",
        ),
        gold_answer=("Individuals lodging their own return must lodge by "
                     "31 October for the previous Australian financial year "
                     "(1 July – 30 June). Lodging through a registered tax "
                     "agent typically has a later due date."),
        fresh_sensitive=False,
    ),
    Question(
        qid="irs-standard-mileage-2025",
        use_case="tax-compliance",
        text="What is the IRS standard mileage rate for business use of a "
             "personal vehicle in 2025?",
        gold_urls=(
            "https://www.irs.gov/tax-professionals/standard-mileage-rates",
        ),
        gold_answer=("70 cents per mile for business use in 2025."),
        fresh_sensitive=True,
        notes="Rate changes annually; tests freshness vs parametric memory.",
    ),
    Question(
        qid="irs-401k-contribution-limit",
        use_case="tax-compliance",
        text="What is the employee elective deferral contribution limit for "
             "a 401(k) plan in 2025, and what's the additional catch-up "
             "amount for people aged 50 and over?",
        gold_urls=(
            "https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-401k-and-profit-sharing-plan-contribution-limits",
        ),
        gold_answer=("The 2025 employee elective deferral limit is $23,500. "
                     "The standard catch-up for employees aged 50+ is an "
                     "additional $7,500."),
        fresh_sensitive=True,
    ),

    # ============================================================
    # Legal & standards (3) — rfc-editor.org, eur-lex.europa.eu
    # ============================================================
    Question(
        qid="rfc-9110-retry-after",
        use_case="legal-standards",
        text="According to RFC 9110, what two value formats are valid for "
             "the Retry-After HTTP response header, and which status codes "
             "is it typically used with?",
        gold_urls=(
            "https://www.rfc-editor.org/rfc/rfc9110.html",
        ),
        gold_answer=("Retry-After accepts either an HTTP-date or a non-"
                     "negative integer number of seconds. It is typically "
                     "sent with 503 (Service Unavailable), 429 (Too Many "
                     "Requests), or any 3xx redirect."),
        fresh_sensitive=False,
    ),
    Question(
        qid="rfc-8259-json-numbers",
        use_case="legal-standards",
        text="What does RFC 8259 say about the interoperable range and "
             "precision of JSON numbers, and what is the recommended "
             "interoperable range?",
        gold_urls=(
            "https://www.rfc-editor.org/rfc/rfc8259.html",
        ),
        gold_answer=("RFC 8259 notes that JSON does not set limits on number "
                     "magnitude or precision, but recommends an interoperable "
                     "range matching IEEE 754 double precision (roughly "
                     "-(2^53)+1 to (2^53)-1 for integers) since many parsers "
                     "use that representation."),
        fresh_sensitive=False,
    ),
    Question(
        qid="eur-lex-gdpr-erasure-when",
        use_case="legal-standards",
        text="Under GDPR Article 17 (right to erasure), name two specific "
             "grounds on which a data subject can require the controller to "
             "erase their personal data without undue delay.",
        gold_urls=(
            "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679",
        ),
        gold_answer=("Examples include: the data is no longer necessary for "
                     "the purposes it was collected; the data subject "
                     "withdraws consent and there is no other legal ground; "
                     "the data subject objects to processing and there is no "
                     "overriding legitimate ground; the data has been "
                     "unlawfully processed."),
        fresh_sensitive=False,
        notes="eur-lex only had 8 pages in v1.0 — may not be reachable in sift-grep.",
    ),

    # ============================================================
    # Support & policy (3) — help.shopify.com, support.atlassian.com, www.notion.com
    # ============================================================
    Question(
        qid="shopify-checkout-discount-percentage",
        use_case="support-policy",
        text="In Shopify, where in the admin do you create a percentage-"
             "based discount code, and what are the main minimum-requirement "
             "options you can attach?",
        gold_urls=(
            "https://help.shopify.com/en/manual/discounts/discount-types",
        ),
        gold_answer=("Discount codes are created under Discounts in the "
                     "Shopify admin. For a percentage discount, the main "
                     "minimum requirements are a minimum purchase amount or "
                     "a minimum quantity of items."),
        fresh_sensitive=False,
    ),
    Question(
        qid="notion-share-database-publicly",
        use_case="support-policy",
        text="In Notion, how do you make a database accessible to anyone on "
             "the web, and what permission level should you set for them?",
        gold_urls=(
            "https://www.notion.com/help/sharing-and-permissions",
        ),
        gold_answer=("Open the share menu and toggle 'Share to web' (or "
                     "'Publish'). The default web access is read-only / "
                     "'Can view'; editor or commenter access requires the "
                     "viewer to have a Notion account with explicit "
                     "workspace permissions."),
        fresh_sensitive=False,
    ),
    Question(
        qid="atlassian-jira-migrate-server-to-cloud",
        use_case="support-policy",
        text="When migrating from Jira Server (or Data Center) to Jira Cloud, "
             "what is the official Atlassian-recommended tool, and at a high "
             "level what does the migration plan include?",
        gold_urls=(
            "https://support.atlassian.com/migration/docs/migrate-from-jira-server-to-cloud",
        ),
        gold_answer=("Atlassian recommends the Jira Cloud Migration "
                     "Assistant, installed as an app on the Server/Data "
                     "Center instance. The migration plan covers selecting "
                     "projects, users, and customisations, running an "
                     "assessment, and choosing whether to merge into an "
                     "existing cloud site or migrate fresh."),
        fresh_sensitive=False,
    ),

    # ============================================================
    # Change monitoring (3) — developers.openai.com, vercel.com, docs.github.com
    # ============================================================
    Question(
        qid="openai-recent-model",
        use_case="change-monitoring",
        text="What is the most recently released OpenAI API model according "
             "to the official OpenAI changelog, and what model family does "
             "it belong to?",
        gold_urls=(
            "https://developers.openai.com/changelog",
        ),
        gold_answer=("Refer to the most recent dated entry in the OpenAI "
                     "developer changelog; the answer should name the "
                     "specific model and identify its family (e.g. GPT, o-"
                     "series, GPT-image)."),
        fresh_sensitive=True,
        notes="Pure freshness question — the answer is whatever the changelog "
              "lists, and changes month to month.",
    ),
    Question(
        qid="vercel-custom-domains-pricing",
        use_case="change-monitoring",
        text="On which Vercel pricing plans are custom domains supported, "
             "and is there a limit on the number per project?",
        gold_urls=(
            "https://vercel.com/docs/limits",
            "https://vercel.com/docs/pricing",
        ),
        gold_answer=("Custom domains are available on all Vercel plans, "
                     "including Hobby. The current per-project limit "
                     "differs by plan: Hobby is capped (e.g. 50), with "
                     "higher limits on Pro and Enterprise."),
        fresh_sensitive=True,
    ),
    Question(
        qid="github-actions-concurrency-group",
        use_case="change-monitoring",
        text="In GitHub Actions, what does the workflow-level `concurrency` "
             "key do, and what option causes an in-progress run to be "
             "cancelled when a new run starts?",
        gold_urls=(
            "https://docs.github.com/en/actions/using-jobs/using-concurrency",
        ),
        gold_answer=("`concurrency` groups runs so only one runs at a time "
                     "within the group. Setting `cancel-in-progress: true` "
                     "cancels any in-progress run in the group when a new "
                     "run starts."),
        fresh_sensitive=False,
    ),

    # ============================================================
    # Internal knowledge (3) — handbook.gitlab.com, posthog.com (handbook), about.gitlab.com
    # ============================================================
    Question(
        qid="gitlab-handbook-vacation",
        use_case="internal-knowledge",
        text="What is GitLab's official policy on paid time off / vacation "
             "for team members, as described in the GitLab handbook?",
        gold_urls=(
            "https://handbook.gitlab.com/handbook/people-group/paid-time-off/",
        ),
        gold_answer=("GitLab offers an uncapped 'flexible' paid time-off "
                     "policy: team members take time off as needed, with no "
                     "official upper limit, and managers encourage a minimum "
                     "amount of leave each year."),
        fresh_sensitive=False,
    ),
    Question(
        qid="posthog-handbook-interview-process",
        use_case="internal-knowledge",
        text="According to the PostHog handbook, what is the typical "
             "engineering interview process, and roughly how many stages "
             "does it include?",
        gold_urls=(
            "https://posthog.com/handbook/people/hiring-process",
        ),
        gold_answer=("PostHog's engineering interview process typically "
                     "includes an application review, a recruiter / hiring-"
                     "manager call, a technical interview, a small paid "
                     "project or 'small project interview', and a culture / "
                     "team-fit interview — roughly four to five stages."),
        fresh_sensitive=False,
    ),
    Question(
        qid="gitlab-about-values",
        use_case="internal-knowledge",
        text="What are GitLab's core company values (the CREDIT or similar "
             "acronym), and what does each letter stand for?",
        gold_urls=(
            "https://about.gitlab.com/handbook/values/",
        ),
        gold_answer=("GitLab's values are CREDIT: Collaboration, Results, "
                     "Efficiency, Diversity / Inclusion / Belonging, "
                     "Iteration, Transparency."),
        fresh_sensitive=False,
    ),
)


def by_qid(qid: str) -> Optional[Question]:
    for q in QUESTIONS:
        if q.qid == qid:
            return q
    return None


def by_use_case(uc: str) -> tuple[Question, ...]:
    return tuple(q for q in QUESTIONS if q.use_case == uc)


def fresh_only() -> tuple[Question, ...]:
    return tuple(q for q in QUESTIONS if q.fresh_sensitive)
