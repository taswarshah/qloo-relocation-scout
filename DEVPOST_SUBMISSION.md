# Devpost submission draft: Relocation Scout (DRAFT, not submitted)

**Tagline:** An agent with taste that finds the city, and the neighborhood, where your kind of life already exists.

**Demo:** https://scout.taswar.dev · **Repo:** https://github.com/taswarshah/qloo-relocation-scout (MIT) · Try a preset or a shared link such as `https://scout.taswar.dev/?music=Radiohead,Bon%20Iver&lifestyle=walkable&compare=Lisbon`

## The problem (Potential Impact)
Remote workers, students, expats and digital nomads choose cities with "best places to live" lists that score rent, jobs and weather but say nothing about whether they'll find *their people*. A bad cultural fit is the quiet reason many moves don't stick. Relocation Scout makes the shortlisting step about identity: it answers "where will I feel at home?" and then "which part of town do I start in?". Those are the two questions no cost-of-living calculator answers.

## What it does
Enter the artists, films, shows, books, restaurants, brands and lifestyle words you love (or pick a preset). In about 4–5 seconds the agent returns a ranked shortlist of cities. Each one comes with:
- a **0–100 taste-affinity score** from Qloo,
- **"Your strongest connections"**: per-taste affinity bars showing *which* of your tastes pull you there (e.g. Reykjavik: Bon Iver 90, Lost in Translation 87),
- **neighborhoods to start in**, each with taste-matched venues (Reykjavik → Austurbær: Kex Hostel, Sun Voyager; San Juan → Viejo San Juan, Santurce). When Qloo has no neighborhood labels, it falls back to the top taste-matched spots,
- a **local vibe** distilled from venue tags (historic, walkable, lively…),
- a plain-English **why**, built only from Qloo data, so it never makes up facts,
- a **shareable link** that reproduces your profile,
- a visible **agent trace** of every Qloo call, with timings,
- **"Why not my city?"**: type a city you're already considering and get its real Qloo score next to your top pick, with the taste-by-taste gap (e.g. "Dubai scores 69/100, 12 points behind Reykjavik (81). The biggest gaps: Bon Iver (52 vs 90), Lost in Translation (58 vs 87)"),
- **side-by-side comparison** of any two cities: overall and per-taste Qloo affinity, shared and unique cultural signals, neighborhood evidence and data coverage. Only real Qloo numbers, no invented composite score,
- **honest failure handling**: if Qloo is slow or failing, the page says the results are partial instead of pretending there were no matches.

## The agent
**Ask the scout** is an LLM tool-use agent whose only source of facts is Qloo. It decides which Qloo-backed tools to call and in what order:
- `scout_cities`: set or refine the taste profile ("add The Bear", "only Europe") and rank cities,
- `check_city`: "why not Lisbon?", scored taste by taste against the top pick,
- `explore_city`: "where in Reykjavik for great coffee?", taste-matched neighborhoods and venues nudged by a Qloo tag,
- `blend_profiles`: **couple and family mode**. Each person is scored separately on Qloo and cities are ranked by the *lower* of the two scores, so the agent finds a real compromise.

The LLM plans and talks. Every city, number, neighborhood and venue in its answer comes from a tool result, and the UI shows each Qloo step it took. Without Qloo it has nothing to say, which is the point.

## How it uses Qloo (Technological Implementation)
Under the tools sits a multi-step pipeline that orchestrates 15–18 Qloo calls per scout, in three parallel rounds:
1. **Perceive:** `/search` resolves each free-text taste to a Qloo entity across 6 entity types (artist, movie, tv_show, book, place, brand). At the same time, `/v2/tags` maps lifestyle words to Qloo tag URNs, preferring destination tags.
2. **Rank:** `/v2/insights` with `filter.type=urn:entity:destination` uses all entity and tag signals combined (optionally `filter.geocode.country_code`). The user's own candidate city is scored on the same signals via `filter.results.entities`.
3. **Attribute (exact):** one insights call per taste, restricted with `filter.results.entities` to exactly the shown cities, builds an attribution matrix keyed by entity ID. Every city explains which tastes drive its score: Qloo's cross-domain affinity made visible.
4. **Zoom in:** for each city, `/v2/insights` with `filter.type=urn:entity:place`, `filter.location=POINT(lon lat)` and `filter.location.radius` (15 km, widening to 60 km when data is thin) finds venues that share your taste. Coordinates avoid name collisions: "Flores" by name resolves to a Buenos Aires barrio instead of Flores, Guatemala. Venues are ranked by affinity, and hotels, schools, offices, gas stations and transit are filtered out using Qloo category tags.
5. **Reason:** the agent cleans and validates the venue data. It strips administrative prefixes ("UPZs Localidad Chapinero" → "Chapinero"), drops venues whose address or geocode contradicts the city or country (handling US territories such as Puerto Rico), merges near-duplicate destinations within 30 km, filters amenity and COVID-era tags out of the vibe, and groups the remainder into neighborhoods.

**Engineering:** async Python (FastAPI and httpx), deployed as a Cloudflare Python Worker on the edge, with bounded concurrency, a pooled keep-alive client, a short capped 429 backoff, and a private in-memory TTL cache that Qloo's guide permits (no Qloo data is ever written to disk or the repo). Cold p50 is 4–10 s across 10 personas depending on Qloo API load, and cached repeats take under 50 ms. The agent runs on Workers AI (Mistral Small 3.1, about 105 neurons per chat turn) behind edge rate limits, including a global ceiling that caps worst-case spend. There are 70 offline tests using synthetic data (including a scripted-LLM harness for the agent loop), and CI runs pytest, pip-audit, bandit and gitleaks.

**Qloo vs the same LLM without Qloo** (same model, same 10 personas, temperature 0):

| Measure | Relocation Scout (Qloo) | LLM only |
|---|---|---|
| Distinct cities across 10 personas (higher = more personal) | 32/38 | 27/40 |
| Share of picks taken by the 5 most common cities (lower = less generic) | 26% | 35% |
| Top-4 overlap when tastes change (lower = responds to taste) | 0.11 | 0.23 |
| Per-taste attribution, neighborhoods, venues | from Qloo | not available |

The LLM alone recycles the same "creative city" shortlist; with Qloo, recommendations are more personal and react to what you actually love.

**Evaluation** (`eval/`, 10 synthetic personas, live Qloo): 100% of tastes and lifestyle words resolved; **100% of explanation claims automatically verified against Qloo-derived data (436/436)**; changing a persona's tastes changes the top-4 cities almost entirely (mean Jaccard overlap 0.11); every shown city has local evidence.

## Design
It's a complete product, not a notebook: a hero section and a 3-step explainer, a grouped form with presets, input hints and validation, a loading state with live status, empty and error states, score badges, affinity meters, neighborhood and fallback spot lists, vibe chips, shareable links and a footer. It's responsive down to mobile and accessible (labels, skip link, `aria-live` results, meters with ARIA labels, keyboard focus styles). A strict Content-Security-Policy is in force, with no inline code and no third-party scripts.

## Quality of the idea
Most taste apps recommend *more content*. Relocation Scout points taste at a life decision, using Qloo's cross-domain graph to connect what's on your playlist to where you'd live. The attribution matrix answers "why?", which is what makes a recommendation like this trustworthy enough to act on.

## Security and responsible use
Strict input limits, per-IP rate limiting and a concurrency cap protect the API quota. Security headers (CSP, nosniff, frame-ancestors none, no-referrer), generic errors, no secrets in the repo or image, and a non-root container. Results are framed as a starting point for exploration, not relocation advice.

## Challenges
Qloo place data is global and noisy: namesake neighborhoods, territory geocodes and amenity tags. Solving that deterministically, with tests, made the output trustworthy. Latency was the other challenge: we restructured the agent into three parallel rounds and added pooled connections.

## What's next
Cost-of-living and visa overlays, side-by-side neighborhood comparison, saved shortlists, and group mode for more than two people.

## Built with
python, fastapi, httpx, asyncio, qloo-insights-api, llm-tool-calling, cloudflare-workers, html5, css3, javascript, github-actions
