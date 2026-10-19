# Devpost submission draft: Relocation Scout (DRAFT, not submitted)

**Tagline:** An agent with taste that finds the city, and the neighborhood, where your kind of life already exists.

**Demo:** <HOSTED_URL> · **Repo:** <PUBLIC_REPO_URL> (MIT) · Try a preset or a shared link such as `<HOSTED_URL>/?music=Radiohead,Bon%20Iver&lifestyle=walkable`

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
- a visible **agent trace** of every Qloo call, with timings.

## How it uses Qloo (Technological Implementation)
A deterministic, multi-step agent that orchestrates 15–18 Qloo calls per scout, in three parallel rounds:
1. **Perceive:** `/search` resolves each free-text taste to a Qloo entity across 6 entity types (artist, movie, tv_show, book, place, brand). At the same time, `/v2/tags` maps lifestyle words to Qloo tag URNs, preferring destination tags.
2. **Rank and attribute:** `/v2/insights` with `filter.type=urn:entity:destination` uses all entity and tag signals combined (optionally `filter.geocode.country_code`) to rank cities. In parallel, one insights call per taste builds an attribution matrix, so every city explains which tastes drive its score. This is Qloo's cross-domain affinity made visible.
3. **Zoom in:** for each city, `/v2/insights` with `filter.type=urn:entity:place&filter.location.query=<city>` and the same signals finds venues that share your taste.
4. **Reason:** the agent cleans and validates the venue data. It strips administrative prefixes ("UPZs Localidad Chapinero" → "Chapinero"), drops venues whose address or geocode contradicts the city or country (handling US territories such as Puerto Rico), merges near-duplicate destinations within 30 km, filters amenity and COVID-era tags out of the vibe, and groups the remainder into neighborhoods.

**Engineering:** async Python (FastAPI and httpx) with bounded concurrency, a pooled keep-alive client, a short capped 429 backoff, and a private in-memory TTL cache that Qloo's guide permits (no Qloo data is ever written to disk or the repo). Measured cold p50 is **4.3 s** (down from 6.5 s), and cached repeats take under 50 ms. There are 41 offline tests using synthetic data, and CI runs pytest, pip-audit, bandit and gitleaks.

## Design
It's a complete product, not a notebook: a hero section and a 3-step explainer, a grouped form with presets, input hints and validation, a loading state with live status, empty and error states, score badges, affinity meters, neighborhood and fallback spot lists, vibe chips, shareable links and a footer. It's responsive down to mobile and accessible (labels, skip link, `aria-live` results, meters with ARIA labels, keyboard focus styles). A strict Content-Security-Policy is in force, with no inline code and no third-party scripts.

## Quality of the idea
Most taste apps recommend *more content*. Relocation Scout points taste at a life decision, using Qloo's cross-domain graph to connect what's on your playlist to where you'd live. The attribution matrix answers "why?", which is what makes a recommendation like this trustworthy enough to act on.

## Security and responsible use
Strict input limits, per-IP rate limiting and a concurrency cap protect the API quota. Security headers (CSP, nosniff, frame-ancestors none, no-referrer), generic errors, no secrets in the repo or image, and a non-root container. Results are framed as a starting point for exploration, not relocation advice.

## Challenges
Qloo place data is global and noisy: namesake neighborhoods, territory geocodes and amenity tags. Solving that deterministically, with tests, made the output trustworthy. Latency was the other challenge: we restructured the agent into three parallel rounds and added pooled connections.

## What's next
Couple and family mode (merging two taste profiles), cost-of-living and visa overlays, side-by-side neighborhood comparison, and an optional LLM narrator grounded only in the agent's Qloo facts (already pluggable).

## Built with
python, fastapi, httpx, asyncio, qloo-insights-api, html5, css3, javascript, github-actions
