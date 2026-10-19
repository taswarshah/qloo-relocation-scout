# 🧭 Relocation Scout: an agent with taste

Moving cities is one of the biggest lifestyle decisions people make, and most "best places to live" lists ignore what makes a place feel like *yours*.
Relocation Scout takes what you love (artists, films, shows, books, restaurants, brands, lifestyle keywords) and uses **Qloo's taste graph** to find the cities **and neighborhoods** whose cultural fabric matches you. It also explains *which* of your tastes pull you there.

Built for the [Qloo Agentic Hackathon](https://qloo.devpost.com/).

## How the agent works
1. **Resolve tastes:** `/search` maps each free-text like to a Qloo entity (artist, movie, tv_show, book, place, brand). `/v2/tags` maps lifestyle words ("walkable", "nightlife", "hiking") to Qloo tag IDs.
2. **Find cities:** `/v2/insights?filter.type=urn:entity:destination` with all entity and tag signals combined (optionally `filter.geocode.country_code`).
3. **Attribute:** one destination-insights call per taste, so each city gets a "strongest pull" breakdown (for example, Reykjavik is driven by Bon Iver 90 and Lost in Translation 87).
4. **Zoom into neighborhoods:** `/v2/insights?filter.type=urn:entity:place&filter.location.query=<city>` with the same signals. Taste-matched venues are grouped by `neighborhood`, and their tags become a "local vibe" summary.
5. **Explain:** grounded explanations are built only from Qloo data. If an OpenAI-compatible LLM is configured (`LLM_*` env vars), it writes an extra narrative from those facts only. No LLM is required.

Every response includes an **agent trace** of the Qloo calls it made.

## Run locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add your Qloo hackathon key
uvicorn app:app --port 8077   # open http://localhost:8077
ENV=development uvicorn app:app --port 8077   # enables /docs locally
python tests/live_personas.py # 3 sample personas end-to-end (live Qloo)
pytest -q tests/test_app.py   # offline tests, Qloo mocked
```
Or with Docker: `docker build -t scout . && docker run -p 8080:8080 --env-file .env scout`

## Notes
- Qloo responses are cached in memory only. No Qloo data is stored in this repo, as the hackathon terms require.
- Requests are rate-limited (semaphore plus 429 backoff).
- Third-party credits: Qloo Insights API, FastAPI, httpx.

## Security
See [SECURITY.md](SECURITY.md): input limits, rate limiting, CSP, no secrets or Qloo data in the repo, and CI running pytest, pip-audit, bandit and gitleaks.

## License
MIT
