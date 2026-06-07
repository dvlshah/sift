"""Agent-in-the-loop eval — measure whether grep over a sift index lets
an agent answer questions more correctly than parametric knowledge alone
or live web-fetch.

This eval is the validation step for sift's headline claim ("a verified,
always-fresh standing index for agents"). Without it, all the per-stage
metrics are component-level — they say the pipeline is deterministic and
the markdown is structurally faithful, but they don't say whether the
final product helps an agent get more answers right.

Three conditions are compared:

  * ``closed-book``  — Claude with no tools (parametric knowledge only)
  * ``sift-grep``    — Claude with grep + read tools over a sift index
  * ``web-fetch``    — Claude with a polite HTTP fetcher over the live web

A small (~20) hand-curated question set, each tied to URLs known to exist
in the v1.0 corpus, drives the comparison. An LLM judge scores correctness
+ citation faithfulness; the aggregate report shows lift per use case
and per question type.
"""

AGENT_LOOP_VERSION = "v1"
