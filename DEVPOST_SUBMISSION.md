# Devpost submission draft: Relocation Scout (DRAFT, not submitted)

**Tagline:** An agent with taste that finds the city, and the neighborhood, where your kind of life already exists.

**Demo:** https://scout.taswar.dev · **Repo:** https://github.com/taswarshah/qloo-relocation-scout (MIT) · Try a preset or a shared link such as `https://scout.taswar.dev/?music=Radiohead,Bon%20Iver&lifestyle=walkable&compare=Lisbon`

## The problem (Potential Impact)
**Who it's for:** people who can choose where to live but don't know where they'd belong: remote workers, graduate students picking between programs, expats with a job offer in a city they've never seen, and couples whose tastes don't match.

**One of them:** a designer who just went fully remote. They can live anywhere, and they know their rent budget. What they don't know is where they'd find the film nights, record shops and quiet cafés that made their current city feel like home. Today that answer only comes after the move. Relocation Scout gives it to them before they book a viewing trip, with the neighborhood to stay in when they do.

**Why taste is the missing piece:** rent, salaries and weather are easy to look up before a move. Whether a place will feel like yours is the part people only find out once they arrive, and it's exactly what Qloo's taste graph measures.

**What they use today:** "best places to live" rankings and cost-of-living calculators. They score rent, salaries and weather, so everyone gets the same shortlist. None of them can tell you whether a city has your kind of record shops, cinemas and cafés, or which neighborhood to start in. Asking a chatbot gives you the same famous cities with confident, unverifiable reasons (we measured this: see the baseline below).

**What Relocation Scout does about it**, shown in the live demo with the "Indie & creative" preset (Radiohead, Bon Iver, Amélie, Lost in Translation, Blue Bottle Coffee; art, cafés, walkable):
1. **A shortlist that's actually yours.** Pittsburgh (84/100), Portland (83), San Francisco and Kyoto. Not the usual Lisbon and Berlin.
2. **The reason, in your own tastes.** Pittsburgh's score is driven by Lost in Translation (86), Bon Iver (85) and Amélie (80). That's something you can check against your own gut.
3. **Where to start.** North Oakland (Carnegie Library, Schenley Plaza) and Downtown (Wood Street Galleries, SPACE), with spots picked for art and cafés.
4. **A check on the city you were already considering.** "Why not Lisbon?" gives: Lisbon scores 75/100, 9 points behind Pittsburgh, and the biggest gaps are Lost in Translation (74 vs 86) and Amélie (68 vs 80).
5. **A compromise for two.** Add a partner who loves Bad Bunny, Daft Punk and beaches, and the agent ranks cities by the *lower* of the two scores, landing on places like Placencia and Huatulco where neither person is settling.

It narrows a huge decision to a shortlist you can visit, with reasons you can verify. It doesn't pretend to cover what Qloo doesn't: cost, visas, jobs and safety are left to the tools built for them, and the agent says so when asked.

## What it does
Enter the artists, films, shows, books or authors, restaurants, brands and lifestyle words you love (or pick a preset). In about 5–7 seconds the agent returns a ranked shortlist of cities. Each one comes with:
- a **0–100 taste-affinity score** from Qloo,
- **"Your strongest connections"**: per-taste affinity bars showing *which* of your tastes pull you there (e.g. Pittsburgh: Lost in Translation 86, Bon Iver 85, Amélie 80),
- **neighborhoods to start in**, each with taste-matched venues (Pittsburgh → North Oakland: Carnegie Library, Schenley Plaza; Portland → Buckman: Mother Foucault's Bookshop, Roseline Coffee), narrowed to your lifestyle words (cafés, art, hiking areas, coworking spaces…). When Qloo has no neighborhood labels, it falls back to the top taste-matched spots,
- a **local vibe** distilled from venue tags (historic, walkable, lively…),
- a plain-English **why**, built only from Qloo data, so it never makes up facts,
- a **shareable link** that reproduces your profile,
- a visible **agent trace** of every Qloo call, with timings,
- **"Why not my city?"**: type a city you're already considering and get its real Qloo score next to your top pick, with the taste-by-taste gap (e.g. "Lisbon scores 75/100, 9 points behind Pittsburgh (84). The biggest gaps: Lost in Translation (74 vs 86), Amélie (68 vs 80)"),
- **side-by-side comparison** of any two cities: overall and per-taste Qloo affinity, shared and unique cultural signals, neighborhood evidence and data coverage. Only real Qloo numbers, no invented composite score,
- **honest failure handling**: favorites Qloo doesn't recognize are shown as "not found" instead of being silently swapped for a lookalike, and if Qloo is slow or failing, the page says the results are partial instead of pretending there were no matches.

## The agent
**Ask the scout** is an LLM tool-use agent whose only source of facts is Qloo. It decides which Qloo-backed tools to call and in what order:
- `scout_cities`: set or refine the taste profile ("add The Bear", "only Europe") and rank cities,
- `check_city`: "why not Lisbon?", scored taste by taste against the top pick,
- `explore_city`: "where in Reykjavik for great coffee?", taste-matched neighborhoods and venues nudged by a Qloo tag,
- `blend_profiles`: **couple and family mode**. Each person is scored separately on Qloo and cities are ranked by the *lower* of the two scores, so the agent finds a real compromise.

The LLM plans and talks. Every city, number, neighborhood and venue in its answer comes from a tool result, and the UI shows each Qloo step it took. This is enforced in code, not just asked for in the prompt: every score-like number in a reply must appear in that turn's Qloo tool output (or earlier in the conversation). If it doesn't, the model is sent back to call the right tool, and if it still can't ground the answer, the user gets a refusal instead of an invented score. Live testing caught exactly this case: asked to blend with a partner, the model once skipped the tool and made up "Barcelona 86/82"; that reply can no longer reach a user. Without Qloo it has nothing to say, which is the point.

## How it uses Qloo (Technological Implementation)
Under the tools sits a multi-step pipeline that orchestrates about 15–25 Qloo calls per scout, in three parallel rounds, paced to the key's 5 requests/second:
1. **Perceive:** `/search` resolves each free-text taste to a Qloo entity (artist, movie, tv_show, place, brand, person), and the agent checks the match: Qloo search always returns *something*, so "zzqxv qwerty" (→ the band Qwerty) is reported as not found instead of quietly steering results. Books resolve to their author as a person, because Qloo rejects books as destination signals. Lifestyle words become Qloo tag **filters**: a destination genre plus venue tags from `/v2/tags?filter.parents.types=urn:entity:place`.
2. **Rank:** `/v2/insights` with `filter.type=urn:entity:destination` and the taste signals, narrowed by `filter.tags` to the lifestyle's destination genres (optionally `filter.geocode.country_code`). We tested passing tags as `signal.interests.tags`: Qloo kept the same order and only lowered the scores, so filters are the way lifestyle actually shapes results. The user's own candidate city is scored on the same signals via `filter.results.entities`.
3. **Attribute (exact):** one insights call per taste, restricted with `filter.results.entities` to exactly the shown cities, builds an attribution matrix keyed by entity ID. Every city explains which tastes drive its score: Qloo's cross-domain affinity made visible.
4. **Zoom in:** for each city, `/v2/insights` with `filter.type=urn:entity:place`, `filter.location=POINT(lon lat)` and `filter.location.radius` (15 km, widening to 60 km when data is thin) finds venues that share your taste, narrowed to your lifestyle's venue tags (cafés, hiking areas, coworking spaces) when enough match nearby. Coordinates avoid name collisions: "Flores" by name resolves to a Buenos Aires barrio instead of Flores, Guatemala. Venues are ranked by affinity, and hotels, wedding venues, supermarkets, schools, offices and transit are filtered out using Qloo category tags.
5. **Reason:** the agent cleans and validates the venue data. It strips administrative prefixes ("UPZs Localidad Chapinero" → "Chapinero"), drops venues whose address or geocode contradicts the city or country (handling US territories such as Puerto Rico), merges near-duplicate destinations within 30 km, spreads the shortlist across regions, keeps only mood and character tags for the vibe, and groups the remainder into neighborhoods. It also catches Qloo pinning a destination to the wrong town ("Cambridge" at London coordinates, "Doolin" in Pennsylvania): when no venue there has the town in its address, the city is swapped for a spare and the trace says why.

**Engineering:** async Python (FastAPI and httpx), deployed as a Cloudflare Python Worker on the edge, with bounded concurrency, a pooled keep-alive client, a lock-free pacer matched to Qloo's 5 req/s limit (with a wait-until-reset retry on 429), and a private in-memory TTL cache that Qloo's guide permits (no Qloo data is ever written to disk or the repo). Cold p50 is 6.2 s (max 7.6 s) across 10 personas, and cached repeats take under 50 ms. The agent runs on Workers AI (Mistral Small 3.1, about 105 neurons per chat turn) behind edge rate limits, including a global ceiling that caps worst-case spend. There are 185 offline tests using synthetic data (including a scripted-LLM harness for the agent loop and its grounding and scope checks), and CI runs pytest, pip-audit, bandit and gitleaks.

**Qloo vs the same LLM without Qloo** (same model, same 10 personas, temperature 0):

| Measure | Relocation Scout (Qloo) | LLM only |
|---|---|---|
| Distinct cities across 10 personas (higher = more personal) | 33/38 | 27/40 |
| Share of picks taken by the 5 most common cities (lower = less generic) | 26% | 35% |
| Top-4 overlap when tastes change (lower = responds to taste) | 0.17 | 0.23 |
| Per-taste attribution, neighborhoods, venues | from Qloo | not available |

The LLM alone recycles the same "creative city" shortlist; with Qloo, recommendations are more personal and react to what you actually love.

**Evaluation** (`eval/`, 10 synthetic personas, live Qloo): 100% of tastes and lifestyle words resolved; **100% of explanation claims automatically verified against Qloo-derived data (444/444)**; requested lifestyle genres appear in the shown cities' Qloo tags 14/16 times (2/3 before lifestyle words became filters); changing a persona's tastes changes most of the top-4 (mean Jaccard overlap 0.17); every shown city has local evidence.

## Design
It's a complete product, not a notebook: a hero section and a 3-step explainer, a grouped form with presets, input hints and validation, a loading state with live status, empty and error states, "not found" chips for unrecognized favorites, score badges, affinity meters, neighborhood and fallback spot lists, vibe chips, shareable links and a footer. It's responsive down to mobile and accessible (labels, skip link, `aria-live` results, meters with ARIA labels, keyboard focus styles). A strict Content-Security-Policy is in force, with no inline code and no third-party scripts.

## Quality of the idea
Most taste apps recommend *more content*. Relocation Scout points taste at a life decision, using Qloo's cross-domain graph to connect what's on your playlist to where you'd live. The attribution matrix answers "why?", which is what makes a recommendation like this trustworthy enough to act on.

## Security and responsible use
Strict input limits, per-IP rate limiting and a concurrency cap protect the API quota. Security headers (CSP, nosniff, frame-ancestors none, no-referrer), generic errors, no secrets in the repo or image, and a non-root container. Results are framed as a starting point for exploration, not relocation advice.

## Challenges
Qloo place data is global and noisy: namesake neighborhoods, territory geocodes, destinations pinned to the wrong town and amenity tags. Probing the API taught us things the docs don't say: books are rejected as destination signals, and tag signals don't move the ranking, so lifestyle words had to become filters. Solving all of that deterministically, with tests, made the output trustworthy. Latency was the other challenge: we restructured the agent into three parallel rounds and added pooled connections.

## What's next
Cost-of-living and visa overlays, side-by-side neighborhood comparison, saved shortlists, and group mode for more than two people.

## Built with
python, fastapi, httpx, asyncio, qloo-insights-api, llm-tool-calling, cloudflare-workers, html5, css3, javascript, github-actions
