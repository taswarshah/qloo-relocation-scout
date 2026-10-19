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

Baseline hook: `baseline(profile)` is a placeholder for a future non-Qloo baseline (e.g. an LLM-only
recommender) to compare against. Intentionally not implemented: no LLM is called from the app or eval.
"""
import asyncio, json, os, re, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TASTE_FIELDS = ["music", "films", "tv", "books", "food", "brands"]


def baseline(profile: dict) -> list[str] | None:
    """Hook for a future baseline recommender. Return a list of city names, or None to skip."""
    return None


def claims_backed(city: dict) -> tuple[int, int, list[str]]:
    """Check each factual claim in city['why'] against the structured, Qloo-derived fields."""
    why, ok, total, bad = city["why"], 0, 0, []
    def check(cond, label):
        nonlocal ok, total
        total += 1
        if cond: ok += 1
        else: bad.append(label)
    m = re.match(r"(.+?) scores (\d+)/100", why)
    check(bool(m) and int(m.group(2)) == int(city["affinity"] * 100), "overall score")
    drivers = {d["taste"]: int(d["affinity"] * 100) for d in city["drivers"]}
    pull = re.search(r"Strongest pull: (.+?)\.(?: [A-Z]|$)", why)
    if pull:
        for name, score in re.findall(r"([^,]+?) \((\d+)\)", pull.group(1)):
            check(drivers.get(name.strip()) == int(score), f"driver {name.strip()}")
    known = re.search(r"Known for: (.+?)\.", why)
    if known:
        tags = {t.lower() for t in city["city_tags"]}
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
    dest_tags = [t["name"].lower() for t in r["lifestyle_tags"] if "destination" in t["id"]]
    tag_hits = sum(1 for t in dest_tags if any(t in (x.lower() for x in c["city_tags"]) for c in cities))
    return {
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
        "baseline": "not run" if baseline(p["profile"]) is None else "run",
    }


async def main():
    personas = json.load(open(os.path.join(HERE, "personas.json")))
    rows = []
    for p in personas:
        row = await run_persona(p); rows.append(row)
        print(json.dumps(row), flush=True)
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
        f"Run: {time.strftime('%Y-%m-%d %H:%M %Z')} · live Qloo hackathon API · {len(personas)} synthetic personas "
        f"(see `personas.json`) · {len(good)} completed · {sum(1 for r in good if r['partial'])} with partial upstream data.",
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
        f"- Mean Jaccard overlap of top-4 cities, persona vs perturbed persona: **{statistics.mean(r['jaccard_top4_vs_perturbed'] for r in good):.2f}** "
        "(lower = recommendations respond to taste changes; 1.0 would mean taste is ignored)",
        *[f"  - {r['id']}: {r['jaccard_top4_vs_perturbed']:.2f}" for r in good], "",
        "## Latency (cold cache, first run of each persona)",
        f"- p50 {statistics.median(secs):.1f} s · max {max(secs):.1f} s", "",
        "## Baseline",
        "- Not run. `baseline()` in `run_eval.py` is a hook for a future non-Qloo baseline; no LLM is called by the app or this eval.",
    ]
    if len(good) < len(rows):
        lines += ["", "## Errors", *[f"- {r['id']}: {r['error']}" for r in rows if "error" in r]]
    open(os.path.join(HERE, "RESULTS.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))

if __name__ == "__main__":
    asyncio.run(main())
