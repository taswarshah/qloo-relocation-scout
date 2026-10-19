# Devpost submission draft: Relocation Scout (DRAFT, not submitted)

**Tagline:** Find the city and the neighborhood that already shares your taste.

## Inspiration
"Best places to live" rankings score cost, jobs and weather, but not whether you'll find *your people*. Remote workers, students and expats choose cities blind to culture. Qloo's cross-domain taste graph can say that someone who loves Bon Iver and *Lost in Translation* will feel at home in Reykjavik's Austurbær or Pittsburgh's Bloomfield.

## What it does
You enter the artists, films, shows, books, restaurants, brands and lifestyle keywords you love. The agent:
1. resolves each like to a Qloo entity or tag,
2. ranks destinations by combined taste affinity,
3. attributes each city to the specific tastes driving it ("strongest pull"),
4. zooms into neighborhoods by finding taste-matched venues in each city and grouping them,
5. explains every pick using only Qloo data, with a transparent trace of each API call.

## How we built it
Python FastAPI backend with an async agent loop over Qloo's `/search`, `/v2/tags` and `/v2/insights` (destination and place types, `signal.interests.entities`, `signal.interests.tags`, `filter.location.query`, `filter.geocode.country_code`). Concurrency limiting, 429 backoff and in-memory caching. Vanilla HTML/JS frontend. An optional LLM narrative layer works with any OpenAI-compatible endpoint and is grounded strictly in Qloo facts.

## Challenges
Rate limits (solved with a semaphore and backoff), filtering amenity tags out of "vibe" signals, and making explanations trustworthy without hallucination.

## What's next
Cost-of-living and visa overlays, side-by-side comparison of two neighborhoods, and "taste match" for a couple or family by merging profiles.

## Built with
python, fastapi, httpx, qloo-insights-api, html, javascript

**Links:** Demo: <HOSTED_URL> · Repo: <PUBLIC_REPO_URL>
