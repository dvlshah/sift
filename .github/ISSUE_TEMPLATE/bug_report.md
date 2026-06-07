---
name: Bug report
about: Something in sift isn't working as expected
title: "[BUG] "
labels: bug
assignees: ''
---

## Describe the bug

A clear, concise description of what's going wrong.

## To reproduce

Steps to reproduce the behavior:

1. `sift init --root ./index`
2. `sift seed --root ./index --from-sitemap ...`
3. `sift run --root ./index ...`
4. See error

Include exact CLI commands and the relevant excerpts from `sift status`,
`sift verify`, or the run log (`./index/_logs/*.log`).

## Expected behavior

What you expected to happen.

## Actual behavior

What actually happened. Paste error messages, stack traces, and the JSON
output of any affected gate or `sift verify` block.

## Environment

- **sift version**: (output of `pip show sift | grep Version`)
- **Python version**: (output of `python --version`)
- **OS**: (macOS 14.5 / Ubuntu 22.04 / etc.)
- **Active site profile**: (from `sift status` → `versions.classifier` and the config's `[site] profile`)
- **Index size** (if relevant): (manifest rows, FRESH count, raw blob count)

## Additional context

- Was this a fresh install or an upgrade?
- Did integrity verification pass before / after the issue? (`sift verify --root R --skip-signature`)
- Any non-default `sift.toml` settings worth flagging?
