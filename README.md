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
Three parallel rounds of Qloo calls (resolve, then rank and attribute, then neighborhoods). Bounded concurrency (`QLOO_CONCURRENCY`, default 6), a pooled keep-alive HTTP client, a short capped 429 backoff, and a private in-memory TTL cache (`QLOO_CACHE_TTL_S`, `QLOO_CACHE_MAX`). Measured with a cold cache across the 10 eval personas: p50 4.1 s, max 5.7 s (original version: p50 6.5 s, max 7.9 s on 3 personas). Cached repeats take under 50 ms. Every response includes a `timings` object.

## Data quality
Neighborhood names are cleaned of administrative prefixes. Venues whose address or geocode contradicts the city or country are dropped (US territories count as US). Destinations within 30 km of each other are merged. Amenity, numeric and COVID-era tags are filtered out of the vibe. Cities without neighborhood labels fall back to top taste-matched spots.


## Screenshots
| Desktop | Mobile |
|---|---|
| ![Home](docs/screenshots/desktop-home.png) | ![Mobile home](docs/screenshots/mobile-home.png) |

Screenshots of results and the comparison view contain Qloo response data, so they're kept out of the repo (Qloo's hackathon terms) and attached to the Devpost submission instead. Regenerate them locally with `tests/ui/browser_check.py`.

## Features added in round 3
- **Compare two cities side by side:** tick any two result cards, including "your pick", to see real Qloo numbers only (no blended score): overall affinity, per-taste affinities, shared and unique cultural signals, neighborhood evidence and data coverage.
- **"Why not my city?":** enter a city you're already considering. It's scored on the same signals via `filter.results.entities`, and the agent explains the gap taste by taste.
- **Exact attribution:** per-taste affinities are fetched for exactly the shown cities (`filter.results.entities`), keyed by Qloo entity ID.
- **Coordinates instead of names for local venues:** `filter.location=POINT(lon lat)` + `filter.location.radius` (15 km, widening to 60 km for thin data). A name like "Flores" otherwise resolves to a Buenos Aires barrio.
- **Relevance filter:** hotels, schools, offices, gas stations, transit and address-like names are dropped using Qloo category tags. Spots are ranked by taste affinity.
- **Failure transparency:** per-endpoint counters (ok, retries, timeouts, HTTP errors, rate-limited). Responses carry `health.partial` and a safe warning shown in the UI, and an upstream outage returns 503 rather than "no matches".

## Evaluation
`python eval/run_eval.py` runs 10 synthetic personas (plus a perturbed version of each) against live Qloo and writes aggregate metrics to [eval/RESULTS.md](eval/RESULTS.md): coverage, relevance proxies, automated explanation grounding, sensitivity and latency. A `baseline()` hook is left for a future non-Qloo baseline.

## UI check
`python tests/ui/browser_check.py` (Playwright and Chrome, localhost only) loads the page, runs a persona with a comparison, fails on any console, CSP or HTTP error, and regenerates the screenshots.

## Notes
- Qloo responses are cached in memory only. No Qloo data is stored in this repo, as the hackathon terms require.
- Requests are rate-limited (semaphore plus 429 backoff).
- Third-party credits: Qloo Insights API, FastAPI, httpx.

## Security
See [SECURITY.md](SECURITY.md): input limits, rate limiting, CSP, no secrets or Qloo data in the repo, and CI running pytest, pip-audit, bandit and gitleaks.

## License
MIT
