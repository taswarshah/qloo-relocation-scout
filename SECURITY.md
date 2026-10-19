# Security

## Reporting
Please report vulnerabilities privately to taswarshah0344@gmail.com. Don't open a public issue.

## Secrets
- `QLOO_API_KEY` (and the optional `LLM_API_KEY`) are read only from environment variables or a local `.env` file, which is git-ignored and docker-ignored. The repo ships only `.env.example`.
- The key is sent only in the `X-Api-Key` header, never in URLs, responses or logs.
- CI runs gitleaks over the full git history on every push.

## Qloo data
No Qloo API responses, fixtures or caches are stored in this repo, as Qloo's hackathon terms require. Responses are cached only in process memory (a bounded LRU with a TTL, shared across requests), which Qloo's guide permits. Nothing is written to disk. Tests use synthetic mock data.

## Hardening in place
| Area | Control |
|---|---|
| Input | Strict schema (unknown fields rejected); each field ≤400 chars; ≤5 items per field (each ≤80 chars, control chars stripped); ≤12 entities and ≤5 lifestyle tags in total; country must match `^[A-Z]{2}$`; request body ≤4 KB |
| Abuse / quota | Per-IP limit (`RATE_LIMIT_PER_MIN`, default 12/min); concurrent agent runs capped (`MAX_CONCURRENT_SCOUTS`, default 3; extra requests get a 503); Qloo calls throttled (`QLOO_CONCURRENCY`=6) with a short capped 429 backoff; Qloo timeout 6s per call (a failed call degrades only its part of the result and is reported in `health`); 60s overall timeout per run |
| Upstream failures | Counted per endpoint; the response gets `health.partial` plus a fixed, safe warning. Upstream bodies, URLs, params and keys are never returned or logged (log lines hold only the path and status) |
| Compare field | ≤60 chars, no control chars or `<>{}` |
| Sharing | Share links contain only the user's own form inputs (no results or Qloo data); they're re-validated server-side like any request |
| Output | The frontend builds the DOM with `textContent` only (no HTML injection); no inline scripts, styles or event handlers |
| Headers | CSP (`default-src 'self'`, `frame-ancestors 'none'`, no `unsafe-inline`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, COOP, and HSTS in production |
| Server | No CORS middleware (same-origin only); generic error messages, with stack traces kept in server logs only; `/docs` and `/openapi.json` disabled unless `ENV=development`; uvicorn server header off |
| Container | `python:3.12-slim`, non-root user (uid 10001), no secrets in the image, `.dockerignore` excludes `.env` and `.git` |
| Supply chain | Runtime dependencies pinned; pip-audit and bandit run in CI |

## Deployment notes
- Behind Cloudflare or another proxy, set `TRUST_PROXY_HEADERS=1` so rate limiting uses `CF-Connecting-IP`. Leave it off otherwise, because the header can be spoofed.
- Rate limits are in-memory and per instance. For multiple instances, use a shared store or Cloudflare rate-limiting rules.
- Rotate the Qloo key if it is ever exposed.
