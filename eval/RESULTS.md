# Evaluation results

Run: 2026-10-20 12:49 PKT · live Qloo hackathon API · 10 synthetic personas (see `personas.json`) · 10 completed · 0 with partial upstream data.
Only aggregate numbers are recorded; per Qloo's terms no response data is stored in the repo.

## Coverage
- Tastes resolved by `/search`: **38/38 (100%)**
  - music: 17/17
  - films: 8/8
  - tv: 3/3
  - books: 3/3
  - food: 4/4
  - brands: 3/3
- Lifestyle words resolved by `/v2/tags`: **20/20 (100%)**

## Relevance proxies
- Mean Qloo affinity of the top pick: **0.84**; of all shown cities: 0.83
- Share of resolved tastes with a non-zero affinity at the top pick: **89%**
- Requested destination tags present in a shown city's Qloo tags: **2/3**
- Cities with local evidence (neighborhoods or spots): **100%**; cities that needed the 60 km widening: 0

## Explanation grounding (automated)
- Claims in explanations backed by Qloo-derived fields: **436/436 (100.0%)**
  (scores, per-taste affinities, city tags, neighborhoods, spots and vibe are each checked against the structured result)

## Sensitivity
- Mean Jaccard overlap of top-4 cities, persona vs perturbed persona: **0.11** (lower = recommendations respond to taste changes; 1.0 would mean taste is ignored)
  - indie_creative: 0.00
  - foodie_nightlife: 0.00
  - outdoorsy_family_us: 0.14
  - jazz_bookworm: 0.00
  - kpop_anime: 0.00
  - country_ranch: 0.00
  - latin_beach: 0.00
  - classical_history_eu: 0.00
  - tech_minimalist: 0.00
  - single_taste_edge: 1.00

## Latency (cold cache, first run of each persona)
- p50 9.6 s · max 15.6 s

## Baseline: the same LLM without Qloo
Model: `@cf/mistralai/mistral-small-3.1-24b-instruct`, temperature 0, same personas, no Qloo access.

| Measure | Relocation Scout (Qloo) | LLM only |
|---|---|---|
| Distinct cities across 10 personas (higher = more personal) | 32/38 | 27/40 |
| Share of all picks taken by the 5 most common cities (lower = less generic) | 26% | 35% |
| Top-4 overlap, persona vs perturbed (lower = responds to taste) | 0.11 | 0.23 |
| Mean Qloo affinity of the picks (Qloo's own yardstick) | 0.83 | 0.78 |
| Per-taste attribution, neighborhoods, venues | ✔ from Qloo | ✘ not available |

- Mean overlap between the two systems' top-4: 0.05
