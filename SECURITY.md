# Security Policy

## Supported versions

This is a self-hosted project distributed as source and a Windows portable package.
Security fixes land on `master` and in the next portable release. There is no LTS
branch — please update to the latest release.

## Reporting a vulnerability

**Do not open a public issue for security problems.** Please report privately via
GitHub's "Report a vulnerability" (Security tab) on this repository, or contact the
maintainer directly. Include:

- affected version / commit
- impact and an attacker model (who can exploit it, from where)
- reproduction steps
- a suggested fix if you have one

You should receive an acknowledgement within a few days. Please give a reasonable
window for a fix before public disclosure.

## What is in scope

- Authentication / session handling (PBKDF2 hashing, HMAC-signed tokens, login
  backoff, session revocation)
- Secret handling: API keys, LLM keys, DingTalk webhook/secret, Xueqiu cookie
- Input validation: expression sandbox (AST whitelist), path traversal, injection
- The trading gate and ledger — anything that lets a discipline violation be
  silently written or hidden

## Out of scope / accepted risks

- **Self-hosted trust model**: the software runs on your own machine with your own
  keys. Anyone with local admin access can modify code or the SQLite database;
  that is by design and cannot be "fixed" (see the project's commercialization
  notes on why local feature locks are ineffective).
- **No HTTPS by default**: deployments are expected on localhost or a trusted LAN.
  Put a reverse proxy in front if you expose it publicly.
- **External market data sources**: availability and rate limits of third-party
  quote providers are not a security guarantee of this project.

## Secret hygiene for operators

- Store secrets in environment variables (`MONITOR_STOCK_LLM_API_KEY`,
  `MONITOR_STOCK_DINGTALK_WEBHOOK_URL`, `MONITOR_STOCK_XUEQIU_COOKIE`, ...), not in
  tracked config files. `config/*.example.json` are empty templates.
- Run `python scripts/harden_secrets.py` (read-only) before committing or releasing.
- Any secret that has ever entered git history must be considered compromised and
  rotated at the provider — rewriting history does not un-leak it.
- Enable the pre-commit hook: `git config core.hooksPath .githooks`.
