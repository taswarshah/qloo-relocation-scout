"""Evaluation harness for Relocation Scout (runs against the LIVE Qloo API; needs .env).

Measures, per synthetic persona:
  * coverage      - share of input tastes resolved by /search and /v2/tags, per category
  * relevance     - proxies: top/mean Qloo affinity, lifestyle-tag hits in city tags,
                    share of resolved tastes with non-zero affinity at the top pick, local evidence rate
  * evidence      - automated check that every claim in each explanation ("why") is backed by a
                    field populated from a Qloo response (scores, tastes, neighborhoods, spots, tags, vibe)
  * sensitivity   - Jaccard overlap of top-k cities between the persona and a perturbed version
Writes aggregate metrics to eval/RESULTS.md. Per Qloo's terms, no Qloo response data (entity names,
cities, venues) is written to the repo - only numbers.

LLM-only baseline (runs when LLM_API_KEY / LLM_BASE_URL / LLM_MODEL are set): the same model, same personas,
no Qloo. Compared on non-circular measures (genericness across personas, sensitivity to taste changes) plus
how the baseline's picks score on Qloo's taste graph (labelled as Qloo's own yardstick).
"""
import asyncio
import json
import os
import pathlib
import re
import statistics
import sys
import time
from collections import Counter

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import agent

HERE = os.path.dirname(os.path.abspath(__file__))
TASTE_FIELDS = ["music", "films", "tv", "books", "food", "brands"]


BASELINE_PROMPT = ("Recommend the 4 best cities in the world for this person to live in, based only on their taste. "
                   "Answer with a JSON array of 4 city names and nothing else.\nTaste profile: {profile}")


def baseline_enabled() -> bool:
    return all(os.getenv(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"))


async def baseline(profile: dict) -> list[str] | None:
    """LLM-only recommender: the same model the app's agent uses, with no Qloo access."""
    if not baseline_enabled():
        return None
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{os.environ['LLM_BASE_URL'].rstrip('/')}/chat/completions",
                              headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
                              json={"model": os.environ["LLM_MODEL"], "temperature": 0, "max_tokens": 200,
                                    "messages": [{"role": "user", "content": BASELINE_PROMPT.format(profile=json.dumps(profile))}]})
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    match = re.search(r"\[.*\]", text, re.DOTALL)
    try:
        cities = json.loads(match.group(0)) if match else []
    except ValueError:
        cities = []
    return [str(c).split(",")[0].strip() for c in cities if str(c).strip()][:4]


async def qloo_score_of(profile: dict, cities: list[str]) -> list[float]:
    """Score arbitrary city names on Qloo for a profile (used to put the baseline on Qloo's yardstick)."""
    q = agent.Qloo()
    entities, tags = await agent.resolve_profile(q, profile, [])
    hits = await asyncio.gather(*[q.search(c, "urn:entity:destination") for c in cities])
    ids = [h["entity_id"] for h in hits if h]
    if not ids or not (entities or tags):
        return []
    scored = await q.insights(filter__type="urn:entity:destination", filter__results__entities=",".join(ids),
                              take=len(ids), **agent._signals(entities))
    return [agent.affinity(m) for m in scored]


def claims_backed(city: dict) -> tuple[int, int, list[str]]:
    """Check each factual claim in city['why'] against the structured, Qloo-derived fields."""
    why, ok, total, bad = city["why"], 0, 0, []
    def check(cond, label):
        nonlocal ok, total
        total += 1
        if cond: ok += 1
        else: bad.append(label)
    m = re.match(r"(.+?) scores (\d+)/100", why)
    check(bool(m) and int(m.group(2)) == agent.pct(city["affinity"]), "overall score")
    drivers = {d["taste"]: agent.pct(d["affinity"]) for d in city["drivers"]}
    pull = re.search(r"Strongest pull: (.+?)\.(?: [A-Z]|$)", why)
    if pull:
        for name, score in re.findall(r"([^,]+?) \((\d+)\)", pull.group(1)):
            check(drivers.get(name.strip()) == int(score), f"driver {name.strip()}")
    known = re.search(r"Known for: (.+?)\.", why)
    if known:
        tags = set(agent.known_for(city["city_tags"]))
        for t in known.group(1).split(", "):
            check(t.strip() in tags, "city tag")
    start = re.search(r"Start in (.+?): spots like (.+?) match your taste\.", why)
    if start:
        hood = next((h for h in city["neighborhoods"] if h["name"] == start.group(1)), None)
        check(hood is not None, "neighborhood")
        # compare the whole list (venue names may contain commas)
        check(bool(hood) and any(start.group(2) == ", ".join(hood["spots"][:k]) for k in (1, 2)), "neighborhood spots")
    top = re.search(r"Top taste-matched spots(?: within \d+ km)?: (.+?)\.(?: [A-Z]|$)", why)
    if top:
        check(any(top.group(1) == ", ".join(city["top_spots"][:k]) for k in (1, 2)), "top spots")
    vibe = re.search(r"Local vibe: (.+?)\.$", why)
    if vibe:
        v = {x.lower() for x in city["vibe"]}
        for t in vibe.group(1).split(", "):
            check(t.strip() in v, "vibe")
    return ok, total, bad


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 1.0


async def run_persona(p: dict) -> dict:
    agent._CACHE.clear()
    t = time.perf_counter()
    r = await agent.scout(p["profile"], 4)
    secs = time.perf_counter() - t
    rp = await agent.scout(p["perturbed"], 4)
    if "error" in r:
        return {"id": p["id"], "error": r["error"]}
    cities = r["cities"]
    asked = {f: agent.split(p["profile"].get(f, "")) for f in TASTE_FIELDS}
    resolved = {f: sum(1 for m in r["matched"] if m["category"] == f) for f in TASTE_FIELDS}
    words = agent.split(p["profile"].get("lifestyle", ""))
    top = cities[0] if cities else None
    ok = tot = 0
    for c in cities:
        o, n, _ = claims_backed(c); ok += o; tot += n
    dest_tags = [t["destination_tag"].rsplit(":", 1)[-1] for t in r["lifestyle_tags"] if t.get("destination_tag")]
    tag_hits = sum(1 for t in dest_tags if any(t == re.sub(r"[^a-z]+", "_", x.lower()).strip("_") for c in cities for x in c["city_tags"]))
    row = {
        "id": p["id"], "seconds": round(secs, 2), "partial": r["health"]["partial"],
        "asked": {f: len(v) for f, v in asked.items() if v}, "resolved": {f: resolved[f] for f, v in asked.items() if v},
        "lifestyle_asked": len(words), "lifestyle_resolved": len(r["lifestyle_tags"]),
        "top_affinity": top["affinity"] if top else 0, "mean_affinity": statistics.mean(c["affinity"] for c in cities) if cities else 0,
        "tastes_scored_at_top": (top["coverage"]["tastes_scored"] / max(1, top["coverage"]["tastes_total"])) if top else 0,
        "dest_tags_requested": len(dest_tags), "dest_tag_hits": tag_hits,
        "local_evidence_rate": sum(1 for c in cities if c["neighborhoods"] or c["top_spots"]) / max(1, len(cities)),
        "widened": sum(1 for c in cities if (c.get("radius_km") or 0) > agent.URBAN_RADIUS_KM),
        "claims_ok": ok, "claims_total": tot,
        "jaccard_top4_vs_perturbed": jaccard([c["city"] for c in cities], [c["city"] for c in rp.get("cities", [])]),
        "cities": [c["city"] for c in cities],  # kept in memory for genericness stats; never written to the repo
    }
    base = await baseline(p["profile"])
    if base is not None:
        base_perturbed = await baseline(p["perturbed"]) or []
        scores = await qloo_score_of(p["profile"], base)
        row.update({"baseline_cities": base,
                    "baseline_jaccard": jaccard(base, base_perturbed),
                    "baseline_qloo_affinity": statistics.mean(scores) if scores else None,
                    "baseline_overlap_with_scout": jaccard(base, row["cities"])})
    return row


def genericness(lists: list[list[str]]) -> tuple[int, int, float]:
    """Distinct cities across personas, total slots, and the share of slots taken by the 5 most common cities."""
    slots = [c.lower() for cities in lists for c in cities]
    top5 = sum(n for _, n in Counter(slots).most_common(5))
    return len(set(slots)), len(slots), top5 / max(1, len(slots))


def baseline_lines(good: list[dict]) -> list[str]:
    if not good or "baseline_cities" not in good[0]:
        return ["- Not run: set `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL` to compare against an LLM-only recommender."]
    rows = [r for r in good if "baseline_cities" in r]
    sd, sn, st = genericness([r["cities"] for r in rows])
    bd, bn, bt = genericness([r["baseline_cities"] for r in rows])
    scored = [r["baseline_qloo_affinity"] for r in rows if r["baseline_qloo_affinity"] is not None]
    return [
        f"Model: `{os.getenv('LLM_MODEL')}`, temperature 0, same personas, no Qloo access.", "",
        "| Measure | Relocation Scout (Qloo) | LLM only |", "|---|---|---|",
        f"| Distinct cities across {len(rows)} personas (higher = more personal) | {sd}/{sn} | {bd}/{bn} |",
        f"| Share of all picks taken by the 5 most common cities (lower = less generic) | {st:.0%} | {bt:.0%} |",
        f"| Top-4 overlap, persona vs perturbed (lower = responds to taste) | {statistics.mean(r['jaccard_top4_vs_perturbed'] for r in rows):.2f} | {statistics.mean(r['baseline_jaccard'] for r in rows):.2f} |",
        f"| Mean Qloo affinity of the picks (Qloo's own yardstick) | {statistics.mean(r['mean_affinity'] for r in rows):.2f} | {statistics.mean(scored):.2f} |" if scored else "| Mean Qloo affinity | n/a | n/a |",
        "| Per-taste attribution, neighborhoods, venues | ✔ from Qloo | ✘ not available |", "",
        f"- Mean overlap between the two systems' top-4: {statistics.mean(r['baseline_overlap_with_scout'] for r in rows):.2f}",
    ]


async def _no_summary(*_args):
    return None


async def main():
    agent.llm_summary = _no_summary  # the deployed app writes no LLM summary; measure that same pipeline
    personas = json.loads(pathlib.Path(HERE, "personas.json").read_text())
    rows = []
    for p in personas:
        row = await run_persona(p); rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k not in ("cities", "baseline_cities")}), flush=True)
    good = [r for r in rows if "error" not in r]
    asked = sum(sum(r["asked"].values()) for r in good); res = sum(sum(r["resolved"].values()) for r in good)
    per_cat = {}
    for f in TASTE_FIELDS:
        a = sum(r["asked"].get(f, 0) for r in good); b = sum(r["resolved"].get(f, 0) for r in good)
        if a: per_cat[f] = (b, a)
    la = sum(r["lifestyle_asked"] for r in good); lr = sum(r["lifestyle_resolved"] for r in good)
    claims_ok = sum(r["claims_ok"] for r in good); claims = sum(r["claims_total"] for r in good)
    dt = sum(r["dest_tags_requested"] for r in good); dh = sum(r["dest_tag_hits"] for r in good)
    secs = [r["seconds"] for r in good]
    lines = [
        "# Evaluation results", "",
        (f"Run: {time.strftime('%Y-%m-%d %H:%M %Z')} · live Qloo hackathon API · {len(personas)} synthetic personas "
         f"(see `personas.json`) · {len(good)} completed · {sum(1 for r in good if r['partial'])} with partial upstream data."),
        "Only aggregate numbers are recorded; per Qloo's terms no response data is stored in the repo.", "",
        "## Coverage",
        f"- Tastes resolved by `/search`: **{res}/{asked} ({res/asked:.0%})**",
        *[f"  - {f}: {b}/{a}" for f, (b, a) in per_cat.items()],
        f"- Lifestyle words resolved by `/v2/tags`: **{lr}/{la} ({lr/max(1,la):.0%})**", "",
        "## Relevance proxies",
        f"- Mean Qloo affinity of the top pick: **{statistics.mean(r['top_affinity'] for r in good):.2f}**; of all shown cities: {statistics.mean(r['mean_affinity'] for r in good):.2f}",
        f"- Share of resolved tastes with a non-zero affinity at the top pick: **{statistics.mean(r['tastes_scored_at_top'] for r in good):.0%}**",
        f"- Requested destination tags present in a shown city's Qloo tags: **{dh}/{dt}**",
        f"- Cities with local evidence (neighborhoods or spots): **{statistics.mean(r['local_evidence_rate'] for r in good):.0%}**; cities that needed the 60 km widening: {sum(r['widened'] for r in good)}", "",
        "## Explanation grounding (automated)",
        f"- Claims in explanations backed by Qloo-derived fields: **{claims_ok}/{claims} ({claims_ok/max(1,claims):.1%})**",
        "  (scores, per-taste affinities, city tags, neighborhoods, spots and vibe are each checked against the structured result)", "",
        "## Sensitivity",
        (f"- Mean Jaccard overlap of top-4 cities, persona vs perturbed persona: **{statistics.mean(r['jaccard_top4_vs_perturbed'] for r in good):.2f}** "
         "(lower = recommendations respond to taste changes; 1.0 would mean taste is ignored)"),
        *[f"  - {r['id']}: {r['jaccard_top4_vs_perturbed']:.2f}" for r in good], "",
        "## Latency (cold cache, first run of each persona)",
        f"- p50 {statistics.median(secs):.1f} s · max {max(secs):.1f} s", "",
        "## Baseline: the same LLM without Qloo",
        *baseline_lines(good),
    ]
    if len(good) < len(rows):
        lines += ["", "## Errors", *[f"- {r['id']}: {r['error']}" for r in rows if "error" in r]]
    pathlib.Path(HERE, "RESULTS.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

if __name__ == "__main__":
    asyncio.run(main())
