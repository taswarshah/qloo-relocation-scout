# 🧭 Relocation Scout: an agent with taste

Moving cities is one of the biggest lifestyle decisions people make, and most "best places to live" lists ignore what makes a place feel like *yours*.
Relocation Scout takes what you love (artists, films, shows, books, restaurants, brands, lifestyle keywords) and uses **Qloo's taste graph** to find the cities **and neighborhoods** whose cultural fabric matches you. It also explains *which* of your tastes pull you there.

Built for the [Qloo Agentic Hackathon](https://qloo.devpost.com/).

**Live demo: https://scout.taswar.dev** (Cloudflare Python Worker). Try a shared profile: <https://scout.taswar.dev/?music=Radiohead,Bon%20Iver&films=Lost%20in%20Translation&lifestyle=walkable&compare=Lisbon>

## Two ways to use it
- **Ask the scout (conversational agent).** An LLM plans and calls Qloo-backed tools itself: `scout_cities`, `check_city` ("why not Lisbon?"), `explore_city` ("where in Reykjavik for great coffee?") and `blend_profiles` ("my partner loves Bad Bunny and beaches"). The LLM only plans and talks. Every city, score, neighborhood and venue it mentions comes from a tool result, and the UI shows each Qloo step it took. Works with any OpenAI-compatible endpoint that supports tool calling (`LLM_*` env vars); the panel is hidden when none is configured.
- **The form.** The same Qloo pipeline as a deterministic one-shot flow, with no LLM needed.

## Couple and family mode
`blend_profiles` resolves each person's tastes separately, ranks a candidate pool on the combined signals, then scores every candidate for each person alone (`filter.results.entities`). Cities are ordered by **the lower of the two scores**, so the result is a genuine compromise, not one person's list. Each card shows both people's affinity bars.

## How the agent works
1. **Resolve tastes:** `/search` maps each free-text like to a Qloo entity (artist, movie, tv_show, book, place, brand). `/v2/tags` maps lifestyle words ("walkable", "nightlife", "hiking") to Qloo tag IDs.
2. **Find cities:** `/v2/insights?filter.type=urn:entity:destination` with all entity and tag signals combined (optionally `filter.geocode.country_code`).
3. **Attribute:** one destination-insights call per taste, so each city gets a "strongest pull" breakdown (for example, Reykjavik is driven by Bon Iver 90 and Lost in Translation 87).
4. **Zoom into neighborhoods:** `/v2/insights?filter.type=urn:entity:place&filter.location.query=<city>` with the same signals. Administrative neighborhood prefixes are cleaned before grouping. Accent-insensitive address/geocode checks reject contradictory locality or country evidence; missing evidence is retained, not treated as proof of location. Common country names in addresses are recognized conservatively. Descriptive venue tags become a "local vibe" summary, excluding service noise, short tags and tags containing digits. When neighborhood detail is missing, the additive `top_spots` list provides up to four distinct taste-matched venues, ranked by affinity (response order breaks ties), and the explanation mentions them.
5. **Explain:** grounded explanations are built only from Qloo data. The conversational agent (`src/chat_agent.py`) wraps these steps as tools; its system prompt forbids inventing places or scores, and tool results are its only facts.

Every response includes an **agent trace** of the Qloo calls it made.

## Run locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-server.txt
cp .env.example .env   # add your Qloo hackathon key (and optionally LLM_* for the chat agent)
uvicorn --app-dir src app:app --port 8077   # open http://localhost:8077
ENV=development uvicorn --app-dir src app:app --port 8077   # enables /docs locally
python tests/live_personas.py # 3 sample personas end-to-end (live Qloo)
pytest -q tests/test_app.py   # offline tests, Qloo mocked
```
Or with Docker: `docker build -t scout . && docker run -p 8080:8080 --env-file .env scout`

## Deploy to Cloudflare Workers (how the live demo runs)
The same FastAPI app runs as a [Python Worker](https://developers.cloudflare.com/workers/languages/python/) (`src/entry.py`); `scripts/build_site.py` turns `static/` into Workers static assets with the app's security headers.
```bash
uv run pywrangler deploy                       # builds .site/, bundles src/ + Pyodide deps
npx wrangler secret put QLOO_API_KEY           # encrypted secret, never in wrangler.toml
npx wrangler secret put LLM_API_KEY            # optional, enables the chat agent (+ set LLM_BASE_URL, LLM_MODEL as vars)
```
Edit the `routes` entry in `wrangler.toml` for your own domain. Note: Pyodide has no threads, so every endpoint is `async def` (a test enforces this).

## Project layout
`src/agent.py` (Qloo pipeline: scout, blend, spots) · `src/chat_agent.py` (LLM tool-use loop) · `src/app.py` (FastAPI, security) · `src/entry.py` (Worker entrypoint) · `static/` (frontend) · `eval/` (live evaluation and LLM-only baseline) · `tests/` (offline tests).

## Cost
Hosting runs on Cloudflare Workers; the chat agent uses Workers AI (`@cf/mistralai/mistral-small-3.1-24b-instruct`) through a binding, so there is no external LLM key. A chat turn measures about **105 neurons** (2 LLM calls, ~3k prompt tokens), so Workers AI's daily free allocation covers roughly 95 turns. Edge rate limits (6 chat turns/min per IP, 12/min globally) bound the worst case. The form flow uses no LLM at all, and Qloo responses are cached in memory, so repeat scouts make zero Qloo calls.

**Why this model:** a live 5-turn test checked every number in each reply against the tool outputs. Mistral Small 3.1 had 0 ungrounded numbers and correct tool use; Llama 4 Scout wrote tool calls as text, and gpt-oss-120b invented 10 of 19 numbers. As a guard, any reply that writes a tool call as text is retried once and then refused.

## Performance
Three parallel rounds of Qloo calls (resolve, then rank and attribute, then neighborhoods). Bounded concurrency (`QLOO_CONCURRENCY`, default 6), a pooled keep-alive HTTP client, a short capped 429 backoff, and a private in-memory TTL cache (`QLOO_CACHE_TTL_S`, `QLOO_CACHE_MAX`). Measured with a cold cache across the 10 eval personas: p50 4.1 s to 9.6 s depending on Qloo API load (see [eval/RESULTS.md](eval/RESULTS.md) for the latest run). Cached repeats take under 50 ms. Every response includes a `timings` object.

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
`python eval/run_eval.py` runs 10 synthetic personas (plus a perturbed version of each) against live Qloo and writes aggregate metrics to [eval/RESULTS.md](eval/RESULTS.md): coverage, relevance proxies, automated explanation grounding, sensitivity and latency. With `LLM_*` set it also runs an **LLM-only baseline** (same model, same personas, no Qloo) and compares how generic the picks are across personas, how much they react to taste changes, and how they score on Qloo's graph.

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
