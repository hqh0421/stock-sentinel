# Contributing to Stock Sentinel

Thanks for your interest in this project. It is a self-hosted A-share monitoring and
decision-support tool. Please read this before opening an issue or pull request.

> This project is for learning and research only. It is not investment advice.

## Scope: what contributions fit

The product's differentiator is **discipline enforcement** — rules that block an
out-of-discipline trade before it happens and let you audit adherence afterward.
Contributions that strengthen this core are most welcome:

- Correctness of the trading gate, four-pool allocation, or adherence ledger
- Backtest honesty (no look-ahead, honest limit-up/down handling)
- Data-source resilience and observable degradation
- Tests, CI, and documentation that make the above verifiable

Out of scope: stock-picking signals, "guaranteed return" claims, or anything that
positions the tool as investment advice. See `SECURITY.md` and the positioning
principles in the project docs for the full boundary.

## Development self-check

The project venv is Python 3.11 and intentionally has no test framework installed —
tests use the standard library `unittest`. From the repo root:

```bash
# 1. Compile check
python -m compileall -q app tests scripts

# 2. Frontend syntax (Node 18+)
for f in frontend/*.js; do node --check "$f"; done

# 3. Secret-leak self-check (read-only, never prints secret values)
python scripts/harden_secrets.py

# 4. Unit tests — run per module (a full single discover run has a known
#    cross-module state leak in some environments)
for f in tests/test_*.py; do python -m unittest "tests.$(basename "$f" .py)"; done
```

The same four checks run automatically in `.github/workflows/ci.yml` on push and PR.

**Never point `APP_DB_PATH` at a real database while testing.** `tests/__init__.py`
redirects it to a temp dir at import time and asserts isolation. Any manual
`import app.main` for smoke tests must set `APP_DB_PATH` to a temp path first —
`ensure_initialized()` runs idempotent setup (including session cleanup) even on
the default DB.

## Commit conventions

Use a scoped, imperative subject line:

```
feat(pools): add drift alert threshold config
fix(backtest): count limit-up days as unfilled
docs(prd): record G-series closure evidence
test(discipline): cover fee split by share ratio
```

Scopes follow the module area: `pools`, `backtest`, `discipline`, `agent`, `alert`,
`rules`, `auth`, `ci`, `docs`, `test`, etc. Keep one logical change per commit and
include the test that proves it.

## Pull request process

1. Fork and create a topic branch off `master`.
2. Make the change with a test; run all four self-checks above.
3. Open a PR describing **what changed, why, and how you verified it**. Link any
   related issue.
4. CI must be green. Maintainers review for correctness against the discipline
   invariants (single source of truth, gate wiring, no look-ahead) before merge.

## External PR flow (how we merge)

- This public repository holds a **sanitized single-commit snapshot**. The private
  development history lives in a separate repository. Accepted PRs are merged here
  first, then ported into the internal history by a maintainer.
- Because the snapshot force-pushes on each release, please keep PRs small and
  based on the latest `master` to avoid repeated conflict resolution.
- Do not include real API keys, cookies, webhook URLs, or personal holdings in any
  commit, issue, or PR. `scripts/harden_secrets.py` and the pre-commit hook will
  reject obvious cases; the rest is on you.

## Reporting bugs

Open an issue with: reproduction steps, expected vs actual behavior, Python/Node
version, OS, and whether you ran from source or the portable package. For
security issues, follow `SECURITY.md` instead of a public issue.
