# Contributing to sift

Thanks for your interest. sift is the open-source engine behind a deterministic, grep-first documentation indexer. Contributions that keep it **deterministic, well-tested, and site-agnostic** are very welcome.

## Development setup

Requires Python **3.11+** (CI runs 3.11 / 3.12 / 3.13).

```bash
git clone https://github.com/dvlshah/sift.git
cd sift
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,evals]"        # runtime + tests + eval suite

# Optional: browser stack for JS-rendered SPAs
pip install -e ".[browser]" && python -m playwright install chromium
```

Entry points after install: `sift` (pipeline CLI), `sift-mcp` (MCP server), `sift-evals` (eval harness).

## Running tests

```bash
pytest -q                            # full suite (asyncio_mode=auto)
pytest tests/test_integrity.py -q    # a single module
```

The suite is hermetic — HTTP is mocked with `respx`, so no network is required. A few real-browser tests are skipped unless chromium and the `[browser]` extra are installed.

## Code style

- Match the surrounding code; keep functions small and the core pipeline **site-agnostic** — no per-site logic outside `sift/sites/`.
- Lint and format with [ruff](https://docs.astral.sh/ruff/): `ruff check .` and `ruff format .`.
- **Determinism is the headline property:** same input → same `content_hash`. Anything touching extract / normalize / hash must preserve it and bump the relevant `*_VERSION` when behavior changes (see `sift/__init__.py`). Re-derivable output (`sift re-extract`) depends on this.

## Commits & PRs

- **Conventional Commits**: `feat(scope): …`, `fix(scope): …`, `docs:`, `refactor:`, `chore:`. Imperative subject, ≤ ~72 chars.
- One logical change per PR; add tests for new behavior and explain *why*, not just *what*.
- CI (GitHub Actions) must be green across 3.11–3.13 before review.

## Adding a site

Most "support site X" work is a `SiteProfile` subclass under `sift/sites/` — URL classification, excludes, facts schemas, browser routing — with **no core changes**. Start from `GenericProfile`, override only what you need, and point `[site].profile` at it in `sift.toml`. See the "Site profiles" section of the README.

## Project layout

| Path | What |
|---|---|
| `sift/` | the engine: `cli.py`, `mcp_server.py`, and the `seed → plan → fetch → extract → commit → publish` phases |
| `sift/sites/` | per-site `SiteProfile`s (the only site-specific code) |
| `evals/` | the `sift-evals` harness (performance, determinism, fidelity) |
| `tests/` | hermetic pytest suite |

## Security

Don't file security problems as public issues — see [SECURITY.md](./SECURITY.md).

## License

By contributing, you agree your contributions are licensed under the repository's [LICENSE](./LICENSE).
