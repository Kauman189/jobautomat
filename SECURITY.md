# Security Policy

## Supported versions

jobautomat is distributed as a container image and as source. Security fixes are
applied to the latest published release and to `main`.

| Version | Supported          |
| ------- | ------------------ |
| `latest` / `main` | :white_check_mark: |
| latest minor tag (`v1.x`) | :white_check_mark: |
| older tags (`v1.0`, …) | :x: — upgrade to the latest tag |

Because the image runs a browser automation stack, always prefer the most recent
tag: Chromium, `patchright` and base-image updates are only shipped there.

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a security problem.**

Use GitHub's private reporting channel:
<https://github.com/Kauman189/jobautomat/security/advisories/new>
(GitHub *Private vulnerability reporting*). If that is unavailable, email
**camiloarias.frg@gmail.com** with the subject `[jobautomat] security`.

Please include:

- affected version / image tag or commit;
- a description of the issue and its impact;
- reproduction steps or a proof of concept;
- any suggested fix or mitigation.

What to expect:

- acknowledgement within **72 hours**;
- an assessment and, if confirmed, a fix in a private branch and a patched tag;
- credit in the advisory once the fix is published, unless you prefer to stay anonymous.

Please give us a reasonable window to release a fix before any public disclosure.

## Scope

In scope: the code in this repository, the published image, the web panel
(`service/web.py`), the pipeline that handles offers and generated documents,
and anything that could leak stored data or credentials.

Out of scope: vulnerabilities in third-party services (LinkedIn, DeepSeek,
Telegram), and issues that require an already-compromised host.

## Secrets and personal data

This project handles real personal data and API keys. Never commit them:

- **Never commit `.env`** or any `*.env` file. `.env` is listed in `.gitignore`;
  keep it that way and copy `.env.example` when you need a starting point.
- Never commit `data/perfil.json`, `data/linkedin_session.json`,
  `data/candidaturas/`, `state.db` or the browser profile directories — they
  contain your CV, session cookies and application history.
- Rotate the `DEEPSEEK_API_KEY` and `TELEGRAM_BOT_TOKEN` immediately if one is
  ever pushed, even to a private branch: rewriting history is not enough, the
  value must be considered leaked.
- When reporting a bug, redact tokens, cookies and personal data from logs and
  screenshots.

If you find a secret committed to this repository, report it privately using the
channel above instead of opening an issue.
