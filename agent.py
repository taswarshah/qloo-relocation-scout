"""Relocation Scout agent: turns a taste profile into city + neighborhood picks using Qloo's Insights API."""
import asyncio, os, re
from collections import Counter, defaultdict
import httpx
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
KEY = os.getenv("QLOO_API_KEY", "")
SEM = asyncio.Semaphore(int(os.getenv("QLOO_CONCURRENCY", "2")))

CATEGORY_TYPES = {
    "music": "urn:entity:artist",
    "films": "urn:entity:movie",
    "tv": "urn:entity:tv_show",
    "books": "urn:entity:book",
    "food": "urn:entity:place",     # restaurants / cafes by name
    "brands": "urn:entity:brand",
}

class Qloo:
    def __init__(self):
        self.c = httpx.AsyncClient(base_url=BASE, headers={"X-Api-Key": KEY}, timeout=20)
        self.cache = {}
    async def get(self, path, params):
        k = (path, tuple(sorted(params.items())))
        if k not in self.cache:
            async with SEM:
                for attempt in range(5):
                    r = await self.c.get(path, params=params)
                    if r.status_code != 429: break
                    await asyncio.sleep(float(r.headers.get("retry-after") or 0) or 1.5 * (attempt + 1))
            if r.status_code >= 400:
                return {}
            self.cache[k] = r.json()
        return self.cache[k]
    async def search(self, q, typ):
        d = await self.get("/search", {"query": q, "types": typ, "take": 1})
        res = d.get("results") or []
        return res[0] if res else None
    async def tags(self, q):
        d = await self.get("/v2/tags", {"filter.query": q, "take": 5})
        return (d.get("results") or {}).get("tags", [])
    async def insights(self, **p):
        p = {k.replace("__", "."): v for k, v in p.items()}
        d = await self.get("/v2/insights", p)
        return (d.get("results") or {}).get("entities", [])

VIBE_STOP = {"place","morning","afternoon","evening","night","routine visit","commercial","destination","locals"}

NOISE = re.compile(r"^(place|morning|afternoon|evening|night|routine visit|commercial|destination|locals|takeout|delivery|dine in|curbside.*|.*accessible.*|.*card.*|.*parking.*|cash.*|\d.*|restroom.*|wi-?fi.*|nfc.*)$")

def split(s): return [x.strip() for x in re.split(r"[,;\n]", s or "") if x.strip()]

async def resolve_profile(q, profile, trace):
    """Step 1: resolve free-text likes to Qloo entity IDs and lifestyle words to tag IDs."""
    entities, tags = [], []
    jobs = [(cat, name) for cat, typ in CATEGORY_TYPES.items() for name in split(profile.get(cat, ""))]
    found = await asyncio.gather(*[q.search(n, CATEGORY_TYPES[c]) for c, n in jobs])
    for (cat, name), e in zip(jobs, found):
        if e:
            entities.append({"id": e["entity_id"], "name": e["name"], "category": cat})
            trace.append(f"search: '{name}' → {e['name']} ({cat})")
        else:
            trace.append(f"search: '{name}' → no match")
    for word in split(profile.get("lifestyle", "")):
        ts = await q.tags(word)
        pick = next((t for t in ts if "destination" in t["id"]), None) or (ts[0] if ts else None)
        if pick:
            tags.append({"id": pick["id"], "name": pick["name"], "word": word})
            trace.append(f"tags: '{word}' → {pick['id']}")
    return entities, tags

async def scout(profile: dict, n_cities: int = 4) -> dict:
    q, trace = Qloo(), []
    entities, tags = await resolve_profile(q, profile, trace)
    if not entities and not tags:
        return {"error": "Couldn't match any of your tastes in Qloo. Try specific artists, films or restaurants.", "trace": trace}
    sig = {}
    if entities: sig["signal__interests__entities"] = ",".join(e["id"] for e in entities)
    if tags: sig["signal__interests__tags"] = ",".join(t["id"] for t in tags)
    if profile.get("country"): sig["filter__geocode__country_code"] = profile["country"].upper()

    # Step 2: overall destination affinity for the whole taste profile.
    dests = await q.insights(filter__type="urn:entity:destination", take=n_cities * 3, **sig)
    trace.append(f"insights(destination, all signals) → {len(dests)} candidates")
    # Step 3: per-domain affinity, so we can explain *which* tastes drive each city.
    per = {}
    for e in entities:
        r = await q.insights(filter__type="urn:entity:destination", signal__interests__entities=e["id"], take=50,
                             **({"filter__geocode__country_code": sig["filter__geocode__country_code"]} if "filter__geocode__country_code" in sig else {}))
        per[e["name"]] = {x["entity_id"]: x["query"].get("affinity", 0) for x in r}
    trace.append(f"insights(destination) per-taste x{len(entities)} for explanations")

    # De-duplicate by city name and keep the top n.
    seen, cities = set(), []
    for d in dests:
        g = d["properties"].get("geocode", {})
        key = (d["name"], g.get("country_code"))
        if key in seen: continue
        seen.add(key); cities.append(d)
        if len(cities) >= n_cities: break

    async def detail(d):
        g = d["properties"].get("geocode", {})
        loc = f"{d['name']}, {g.get('admin1_region') or ''} {g.get('country_code') or ''}".strip()
        places = await q.insights(filter__type="urn:entity:place", filter__location__query=d["name"],
                                  take=25, **{k: v for k, v in sig.items() if k.startswith("signal")})
        hoods = defaultdict(list); vibe = Counter()
        for p in places:
            pr = p.get("properties", {})
            nb = pr.get("neighborhood")
            if nb: hoods[nb].append(p["name"])
            for t in (p.get("tags") or [])[:8]:
                if not NOISE.search(t.get("name", "").lower()) and "category" not in (t.get("type") or ""):
                    vibe[t["name"]] += 1
        top_hoods = sorted(hoods.items(), key=lambda kv: -len(kv[1]))[:3]
        drivers = sorted(((nm, m.get(d["entity_id"], 0)) for nm, m in per.items()), key=lambda x: -x[1])
        drivers = [x for x in drivers if x[1] > 0][:3]
        return {
            "city": d["name"], "location": loc, "country": g.get("country_code"),
            "affinity": round(d["query"].get("affinity", 0), 3),
            "city_tags": [t["name"] for t in d.get("tags") or []][:5],
            "neighborhoods": [{"name": n, "spots": s[:4]} for n, s in top_hoods],
            "vibe": [v for v, _ in vibe.most_common(6)],
            "drivers": [{"taste": nm, "affinity": round(a, 3)} for nm, a in drivers],
        }
    results = await asyncio.gather(*[detail(d) for d in cities])
    trace.append(f"insights(place, filter.location.query=city) x{len(results)} for neighborhoods")
    for r in results: r["why"] = explain(r, tags)
    narrative = await llm_summary(profile, results)
    await q.c.aclose()
    return {"matched": entities, "lifestyle_tags": tags, "cities": results, "narrative": narrative, "trace": trace}

def explain(r, tags):
    """Rule-based explanation grounded only in Qloo data (works with no LLM)."""
    parts = [f"{r['city']} scores {int(r['affinity']*100)}/100 taste affinity with your profile."]
    if r["drivers"]:
        parts.append("Strongest pull: " + ", ".join(f"{d['taste']} ({int(d['affinity']*100)})" for d in r["drivers"]) + ".")
    if r["city_tags"]: parts.append("Known for: " + ", ".join(r["city_tags"][:3]).lower() + ".")
    if r["neighborhoods"]:
        h = r["neighborhoods"][0]
        parts.append(f"Start in {h['name']}: spots like {', '.join(h['spots'][:2])} match your taste.")
    if r["vibe"]: parts.append("Local vibe: " + ", ".join(r["vibe"][:4]).lower() + ".")
    return " ".join(parts)

async def llm_summary(profile, results):
    """Optional: any OpenAI-compatible endpoint (Groq, Gemini, Cloudflare Workers AI...). Skipped if not configured."""
    key, base, model = os.getenv("LLM_API_KEY"), os.getenv("LLM_BASE_URL"), os.getenv("LLM_MODEL")
    if not (key and base and model): return None
    facts = "\n".join(f"- {r['why']}" for r in results)
    msg = ("You are a relocation advisor. Using ONLY these Qloo-derived facts, write a warm 120-word "
           f"recommendation for someone whose tastes are {profile}. Facts:\n{facts}")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "messages": [{"role": "user", "content": msg}]})
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return None
