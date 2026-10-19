# 🧭 Relocation Scout: an agent with taste

Moving cities is one of the biggest lifestyle decisions people make, and most "best places to live" lists ignore what makes a place feel like *yours*.
Relocation Scout takes what you love (artists, films, shows, books, restaurants, brands, lifestyle keywords) and uses **Qloo's taste graph** to find the cities **and neighborhoods** whose cultural fabric matches you. It also explains *which* of your tastes pull you there.

Built for the [Qloo Agentic Hackathon](https://qloo.devpost.com/).

## How the agent works
1. **Resolve tastes:** `/search` maps each free-text like to a Qloo entity (artist, movie, tv_show, book, place, brand). `/v2/tags` maps lifestyle words ("walkable", "nightlife", "hiking") to Qloo tag IDs.
2. **Find cities:** `/v2/insights?filter.type=urn:entity:destination` with all entity and tag signals combined (optionally `filter.geocode.country_code`).
3. **Attribute:** one destination-insights call per taste, so each city gets a "strongest pull" breakdown (for example, Reykjavik is driven by Bon Iver 90 and Lost in Translation 87).
4. **Zoom into neighborhoods:** `/v2/insights?filter.type=urn:entity:place&filter.location.query=<city>` with the same signals. Administrative neighborhood prefixes are cleaned before grouping. Accent-insensitive address/geocode checks reject contradictory locality or country evidence; missing evidence is retained, not treated as proof of location. Common country names in addresses are recognized conservatively. Descriptive venue tags become a "local vibe" summary, excluding service noise, short tags and tags containing digits. When neighborhood detail is missing, the additive `top_spots` list provides up to four distinct taste-matched venues, ranked by affinity (response order breaks ties), and the explanation mentions them.
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

## Performance
Three parallel rounds of Qloo calls (resolve, then rank and attribute, then neighborhoods). Bounded concurrency (`QLOO_CONCURRENCY`, default 4), a pooled keep-alive HTTP client, a short capped 429 backoff, and a private in-memory TTL cache (`QLOO_CACHE_TTL_S`, `QLOO_CACHE_MAX`). Measured on 3 personas × 2 runs with a cold cache: p50 4.3 s, max 5.7 s (before: p50 6.5 s, max 7.9 s). Cached repeats take under 50 ms. Every response includes a `timings` object.

## Data quality
Neighborhood names are cleaned of administrative prefixes. Venues whose address or geocode contradicts the city or country are dropped (US territories count as US). Destinations within 30 km of each other are merged. Amenity, numeric and COVID-era tags are filtered out of the vibe. Cities without neighborhood labels fall back to top taste-matched spots.

## Notes
- Qloo responses are cached in memory only. No Qloo data is stored in this repo, as the hackathon terms require.
- Requests are rate-limited (semaphore plus 429 backoff).
- Third-party credits: Qloo Insights API, FastAPI, httpx.

## Security
See [SECURITY.md](SECURITY.md): input limits, rate limiting, CSP, no secrets or Qloo data in the repo, and CI running pytest, pip-audit, bandit and gitleaks.

## License
MIT
