"""Relocation Scout agent: turns a taste profile into city + neighborhood picks using Qloo's Insights API."""
import asyncio
import math
import os
import re
import time
from collections import OrderedDict
import unicodedata
from collections import Counter, defaultdict
from typing import Any, TypedDict
import httpx
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
KEY = os.getenv("QLOO_API_KEY", "")
SEM = asyncio.Semaphore(int(os.getenv("QLOO_CONCURRENCY", "4")))
CACHE_TTL_S = float(os.getenv("QLOO_CACHE_TTL_S", "3600"))
CACHE_MAX = int(os.getenv("QLOO_CACHE_MAX", "2000"))
# Private, in-process (memory-only) cache shared across requests. Never written to disk or the repo,
# which Qloo's hackathon guide explicitly allows ("private server-side caching is fine").
_CACHE: "OrderedDict[tuple, tuple[float, dict[str, Any]]]" = OrderedDict()

CATEGORY_TYPES = {
    "music": "urn:entity:artist",
    "films": "urn:entity:movie",
    "tv": "urn:entity:tv_show",
    "books": "urn:entity:book",
    "food": "urn:entity:place",
    "brands": "urn:entity:brand",
}

QlooEntity = dict[str, Any]
Parameters = dict[str, str | int]


class MatchedTaste(TypedDict):
    """Resolved profile interest used as an entity signal."""

    id: str
    name: str
    category: str


class LifestyleTag(TypedDict):
    """Resolved lifestyle signal and its original keyword."""

    id: str
    name: str
    word: str


class Neighborhood(TypedDict):
    """Clean neighborhood label with distinct taste-matched venues."""

    name: str
    spots: list[str]


class TasteDriver(TypedDict):
    """A profile taste's individual destination affinity."""

    taste: str
    affinity: float


class CityResult(TypedDict):
    """Backward-compatible city response, with an additive venue fallback."""

    city: str
    location: str
    country: str | None
    affinity: float
    city_tags: list[str]
    neighborhoods: list[Neighborhood]
    top_spots: list[str]
    vibe: list[str]
    drivers: list[TasteDriver]
    why: str


class ScoutResult(TypedDict, total=False):
    """Successful scout payload or the existing unmatched-profile error."""

    matched: list[MatchedTaste]
    lifestyle_tags: list[LifestyleTag]
    cities: list[CityResult]
    narrative: str | None
    trace: list[str]
    timings: dict[str, int]
    error: str


_CLIENT: httpx.AsyncClient | None = None


def _shared_client() -> httpx.AsyncClient:
    """One pooled client per event loop: keep-alive avoids a TLS handshake per request."""
    global _CLIENT
    loop = asyncio.get_running_loop()
    if _CLIENT is None or _CLIENT.is_closed or getattr(_CLIENT, "_loop", None) is not loop:
        _CLIENT = httpx.AsyncClient(base_url=BASE, headers={"X-Api-Key": KEY},
                                    timeout=httpx.Timeout(10.0, connect=5.0),
                                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        _CLIENT._loop = loop  # type: ignore[attr-defined]
    return _CLIENT


class Qloo:
    """Request-scoped Qloo client with memory-only caching and rate backoff."""

    def __init__(self) -> None:
        self.c = _shared_client()
        self.calls = 0
        self.cache_hits = 0

    async def get(self, path: str, params: Parameters) -> dict[str, Any]:
        """Fetch a permitted endpoint, retrying only rate-limited requests."""
        cache_key = (path, tuple(sorted(params.items())))
        hit = _CACHE.get(cache_key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            _CACHE.move_to_end(cache_key)
            self.cache_hits += 1
            return hit[1]
        self.calls += 1
        async with SEM:
            for attempt in range(4):
                response = await self.c.get(path, params=params)
                if response.status_code != 429:
                    break
                # Short, capped backoff keeps p95 under the 8 s budget.
                await asyncio.sleep(min(float(response.headers.get("retry-after") or 0) or 0.4 * (attempt + 1), 2.0))
        if response.status_code >= 400:
            return {}
        data = response.json()
        _CACHE[cache_key] = (time.monotonic(), data)
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)
        return data

    async def search(self, query: str, entity_type: str) -> QlooEntity | None:
        """Resolve one free-text taste to an entity."""
        data = await self.get("/search", {"query": query, "types": entity_type, "take": 1})
        results = data.get("results") or []
        return results[0] if results else None

    async def tags(self, query: str) -> list[QlooEntity]:
        """Resolve a lifestyle word to a small set of candidate tags."""
        data = await self.get("/v2/tags", {"filter.query": query, "take": 5})
        return (data.get("results") or {}).get("tags", [])

    async def insights(self, **params: str | int) -> list[QlooEntity]:
        """Query destination or place insights with dotted Qloo parameters."""
        query = {key.replace("__", "."): value for key, value in params.items()}
        data = await self.get("/v2/insights", query)
        return (data.get("results") or {}).get("entities", [])

NOISE = re.compile(r"^(place|morning|afternoon|evening|night|routine visit|commercial|destination|locals|takeout|delivery|dine in|curbside.*|.*accessible.*|.*card.*|.*parking.*|cash.*|\d.*|restroom.*|wi-?fi.*|nfc.*)$")

VIBE_JUNK = re.compile(r"delivery|disinfect|contact|mask|staff|sanitiz|temperature|alcohol|reservation|payment|credit|debit|wheelchair|restroom|parking|checks?$|dine-?in")

MAX_ITEMS_PER_FIELD = 5
MAX_ENTITIES = 12
MAX_TAGS = 5
ADMIN_PREFIX = re.compile(r"^(?:upzs?|localidad)(?:\s+|$)", re.IGNORECASE)
COUNTRY_ALIASES = {
    "US": ("united states", "united states of america", "usa", "u.s.a.", "estados unidos"),
    "GT": ("guatemala",),
    "CO": ("colombia",),
    "CA": ("canada",),
    "MX": ("mexico",),
    "GB": ("united kingdom", "uk", "great britain", "england", "scotland", "wales"),
    "PT": ("portugal",),
    "DE": ("germany", "deutschland", "alemania"),
    "FR": ("france", "francia"),
    "ES": ("spain", "espana"),
    "IT": ("italy", "italia"),
    "BR": ("brazil", "brasil"),
    "AR": ("argentina",),
    "PE": ("peru",),
    "CL": ("chile",),
    "JP": ("japan", "japon"),
    "AU": ("australia",),
}


def split(value: str | None) -> list[str]:
    """Split a comma list, drop control chars, cap item count and length."""
    items = [re.sub(r"[\x00-\x1f\x7f]", "", item).strip()[:80] for item in re.split(r"[,;\n]", value or "")]
    return [item for item in items if item][:MAX_ITEMS_PER_FIELD]


def normalize(value: str) -> str:
    """Fold accents, case and whitespace for deterministic label comparisons."""
    plain = "".join(char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char))
    return " ".join(plain.casefold().split())


def clean_neighborhood_name(name: str | None) -> str:
    """Strip repeated administrative prefixes, preserving Zona/Zone labels."""
    cleaned = " ".join((name or "").split())
    while ADMIN_PREFIX.match(cleaned):
        cleaned = ADMIN_PREFIX.sub("", cleaned, count=1).strip()
    return cleaned


def mentions(text: str, label: str) -> bool:
    """Match a complete accent-insensitive label, not a city-name substring."""
    needle = normalize(label)
    return bool(needle and re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", normalize(text)))


# Qloo geocodes US territories as country "US" while destinations use the territory code.
COUNTRY_EQUIVALENTS = {"PR": "US", "GU": "US", "VI": "US", "AS": "US", "MP": "US"}


def same_country(a: str | None, b: str | None) -> bool:
    """Compare country codes, treating US territories as the US."""
    return bool(a and b and COUNTRY_EQUIVALENTS.get(a, a) == COUNTRY_EQUIVALENTS.get(b, b))


def country_code(value: str | None) -> str | None:
    """Recognize structured codes and common country names; unknowns stay unknown."""
    if not value:
        return None
    if re.fullmatch(r"[a-zA-Z]{2}", value.strip()):
        return value.strip().upper()
    folded = normalize(value)
    return next((code for code, aliases in COUNTRY_ALIASES.items() if folded in aliases), None)


def place_matches_city(place: QlooEntity, city: QlooEntity) -> bool:
    """Reject contradictory country/locality evidence, retaining uncertain places.

    Address and geocode city fields use whole-label, accent-insensitive matches.
    Explicit country conflicts veto namesakes. Free-text country names are checked
    only at the end of an address; bare two-letter address tokens may be states.
    Missing or unrecognized evidence cannot establish that a venue is foreign.
    """
    properties = place.get("properties") or {}
    geocode = properties.get("geocode") or {}
    city_geocode = (city.get("properties") or {}).get("geocode") or {}
    expected_country = country_code(city_geocode.get("country_code") or city_geocode.get("country"))
    address = properties.get("address") or ""
    address_fields = address if isinstance(address, dict) else {}
    address_text = address if isinstance(address, str) else ", ".join(
        value for value in address_fields.values() if isinstance(value, str)
    )
    countries = [country_code(fields.get("country_code") or fields.get("country"))
                 for fields in (geocode, address_fields)]
    terminal = normalize(address_text).rstrip(" .")
    for code, aliases in COUNTRY_ALIASES.items():
        if any(re.search(r"(?<!\w)" + re.escape(alias.rstrip(" .")) + r"$", terminal) for alias in aliases):
            countries.append(code)
    if expected_country and any(code and not same_country(code, expected_country) for code in countries):
        return False
    if mentions(address_text, city["name"]):
        return True
    localities = [fields.get(key) for fields in (geocode, address_fields)
                  for key in ("city", "locality") if fields.get(key)]
    if localities:
        return any(mentions(locality, city["name"]) for locality in localities)
    return True


def vibe_tag(tag: QlooEntity) -> str | None:
    """Keep descriptive tags, excluding category, service, numeric and short junk."""
    name = (tag.get("name") or "").strip()
    if (len(name) < 3 or len(name) > 28 or any(char.isdigit() for char in name) or VIBE_JUNK.search(name.casefold())
            or NOISE.search(name.casefold()) or "category" in (tag.get("type") or "").casefold()):
        return None
    return name


def affinity(entity: QlooEntity) -> float:
    """Read the optional taste-affinity score, defaulting to zero."""
    return float((entity.get("query") or {}).get("affinity") or 0)


def summarize_places(places: list[QlooEntity], city: QlooEntity) -> tuple[list[Neighborhood], list[str], list[str]]:
    """Group cleaned neighborhoods and rank distinct local spots and vibe tags."""
    hoods: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}
    vibe: Counter[str] = Counter()
    local_places = [place for place in places if place_matches_city(place, city)]
    for place in local_places:
        name = (place.get("name") or "").strip()
        neighborhood = clean_neighborhood_name((place.get("properties") or {}).get("neighborhood"))
        if name and neighborhood:
            key = normalize(neighborhood)
            labels.setdefault(key, neighborhood)
            if name not in hoods[key]:
                hoods[key].append(name)
        for tag in (place.get("tags") or [])[:8]:
            descriptive = vibe_tag(tag)
            if descriptive:
                vibe[descriptive] += 1
    ranked_hoods = sorted(hoods, key=lambda key: -len(hoods[key]))[:3]
    neighborhoods: list[Neighborhood] = [{"name": labels[key], "spots": hoods[key][:4]} for key in ranked_hoods]
    top_spots: list[str] = []
    seen_spots: set[str] = set()
    for place in sorted(local_places, key=affinity, reverse=True):
        name = (place.get("name") or "").strip()
        key = normalize(name)
        if key and key not in seen_spots:
            seen_spots.add(key)
            top_spots.append(name)
        if len(top_spots) == 4:
            break
    return neighborhoods, top_spots, [name for name, _ in vibe.most_common(6)]


async def resolve_profile(q: Qloo, profile: dict[str, str], trace: list[str]) -> tuple[list[MatchedTaste], list[LifestyleTag]]:
    """Step 1: resolve free-text likes to Qloo entity IDs and lifestyle words to tag IDs."""
    entities: list[MatchedTaste] = []
    tags: list[LifestyleTag] = []
    jobs = [(category, name) for category in CATEGORY_TYPES for name in split(profile.get(category, ""))][:MAX_ENTITIES]
    words = split(profile.get("lifestyle", ""))[:MAX_TAGS]
    # Entity search and tag lookup are independent: one parallel round instead of two.
    results = await asyncio.gather(*[q.search(name, CATEGORY_TYPES[category]) for category, name in jobs],
                                   *[q.tags(word) for word in words])
    found, tag_results = results[:len(jobs)], results[len(jobs):]
    for (category, name), entity in zip(jobs, found):
        if entity:
            entities.append({"id": entity["entity_id"], "name": entity["name"], "category": category})
            trace.append(f"search: '{name}' → {entity['name']} ({category})")
        else:
            trace.append(f"search: '{name}' → no match")
    for word, candidates in zip(words, tag_results):
        pick = next((tag for tag in candidates if "destination" in tag["id"]), None) or (candidates[0] if candidates else None)
        if pick:
            tags.append({"id": pick["id"], "name": pick["name"], "word": word})
            trace.append(f"tags: '{word}' → {pick['id']}")
    return entities, tags

async def scout(profile: dict[str, str], n_cities: int = 4) -> ScoutResult:
    """Scout taste-matched cities (pooled HTTP client is shared and kept alive)."""
    q = Qloo()
    return await _scout(q, profile, n_cities)

async def _scout(q: Qloo, profile: dict[str, str], n_cities: int) -> ScoutResult:
    """Resolve signals, rank cities, attribute tastes, then gather local detail."""
    trace: list[str] = []
    t0 = time.perf_counter()
    entities, tags = await resolve_profile(q, profile, trace)
    if not entities and not tags:
        return {"error": "Couldn't match any of your tastes in Qloo. Try specific artists, films or restaurants.", "trace": trace}
    signals: Parameters = {}
    if entities:
        signals["signal__interests__entities"] = ",".join(entity["id"] for entity in entities)
    if tags:
        signals["signal__interests__tags"] = ",".join(tag["id"] for tag in tags)
    country_filter: Parameters = {}
    if profile.get("country"):
        country_filter["filter__geocode__country_code"] = profile["country"].upper()

    t1 = time.perf_counter()
    # Combined ranking and per-taste attribution are independent: run them in parallel.
    destinations, *per_taste_lists = await asyncio.gather(
        q.insights(filter__type="urn:entity:destination", take=n_cities * 3, **signals, **country_filter),
        *[q.insights(filter__type="urn:entity:destination", signal__interests__entities=entity["id"], take=50,
                     **country_filter) for entity in entities])
    per_taste: dict[str, dict[str, float]] = {
        entity["name"]: {match["entity_id"]: affinity(match) for match in matches}
        for entity, matches in zip(entities, per_taste_lists)}
    trace.append(f"insights(destination, all signals) → {len(destinations)} candidates")
    trace.append(f"insights(destination) per-taste x{len(entities)} in parallel for attribution")
    t2 = time.perf_counter()

    cities = unique_cities(destinations, n_cities)
    results = await asyncio.gather(*[city_detail(q, city, signals, per_taste) for city in cities])
    trace.append(f"insights(place, filter.location.query=city) x{len(results)} in parallel for neighborhoods")
    t3 = time.perf_counter()
    narrative = await llm_summary(profile, results)
    timings = {"resolve_ms": round((t1 - t0) * 1000), "rank_ms": round((t2 - t1) * 1000),
               "neighborhoods_ms": round((t3 - t2) * 1000), "total_ms": round((time.perf_counter() - t0) * 1000),
               "qloo_calls": q.calls, "cache_hits": q.cache_hits}
    trace.append(f"done in {timings['total_ms']} ms · {q.calls} Qloo calls · {q.cache_hits} cache hits")
    return {"matched": entities, "lifestyle_tags": tags, "cities": results, "narrative": narrative,
            "trace": trace, "timings": timings}


def near(a: QlooEntity, b: QlooEntity, km: float = 30.0) -> bool:
    """True when two destinations are within `km` (e.g. "Cartagena" vs "Cartagena De Indias")."""

    la, lb = a.get("location") or {}, b.get("location") or {}
    try:
        lat1, lon1, lat2, lon2 = map(math.radians, (la["lat"], la["lon"], lb["lat"], lb["lon"]))
    except (KeyError, TypeError):
        return False
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h)) < km


def unique_cities(destinations: list[QlooEntity], limit: int) -> list[QlooEntity]:
    """Keep the first destination per city/country pair in ranked input order."""
    seen: set[tuple[str, str | None]] = set()
    cities: list[QlooEntity] = []
    for destination in destinations:
        geocode = (destination.get("properties") or {}).get("geocode") or {}
        key = (normalize(destination["name"]), geocode.get("country_code"))
        if key in seen or any(near(destination, kept) for kept in cities):
            continue
        seen.add(key)
        cities.append(destination)
        if len(cities) >= limit:
            break
    return cities


async def city_detail(q: Qloo, city: QlooEntity, signals: Parameters,
                      per_taste: dict[str, dict[str, float]]) -> CityResult:
    """Build one city card from destination affinity and filtered local places."""
    geocode = (city.get("properties") or {}).get("geocode") or {}
    location = f"{city['name']}, {geocode.get('admin1_region') or ''} {geocode.get('country_code') or ''}".strip()
    places = await q.insights(filter__type="urn:entity:place", filter__location__query=city["name"], take=25, **signals)
    neighborhoods, top_spots, vibe = summarize_places(places, city)
    drivers = sorted(((name, matches.get(city["entity_id"], 0)) for name, matches in per_taste.items()),
                     key=lambda item: -item[1])
    result: CityResult = {
        "city": city["name"], "location": location, "country": geocode.get("country_code"),
        "affinity": round(affinity(city), 3),
        "city_tags": [tag["name"] for tag in city.get("tags") or []][:5],
        "neighborhoods": neighborhoods, "top_spots": top_spots, "vibe": vibe,
        "drivers": [{"taste": name, "affinity": round(score, 3)} for name, score in drivers if score > 0][:3],
        "why": "",
    }
    result["why"] = explain(result, [])
    return result


def explain(result: CityResult, tags: list[LifestyleTag]) -> str:
    """Rule-based explanation grounded only in Qloo data (works with no LLM)."""
    parts = [f"{result['city']} scores {int(result['affinity']*100)}/100 taste affinity with your profile."]
    if result["drivers"]:
        parts.append("Strongest pull: " + ", ".join(f"{driver['taste']} ({int(driver['affinity']*100)})" for driver in result["drivers"]) + ".")
    if result["city_tags"]:
        parts.append("Known for: " + ", ".join(result["city_tags"][:3]).lower() + ".")
    if result["neighborhoods"]:
        neighborhood = result["neighborhoods"][0]
        parts.append(f"Start in {neighborhood['name']}: spots like {', '.join(neighborhood['spots'][:2])} match your taste.")
    elif result.get("top_spots"):
        parts.append("Neighborhood detail is unavailable. Top spots to explore: " + ", ".join(result["top_spots"][:2]) + "; these match your taste.")
    if result["vibe"]:
        parts.append("Local vibe: " + ", ".join(result["vibe"][:4]).lower() + ".")
    return " ".join(parts)

async def llm_summary(profile: dict[str, str], results: list[CityResult]) -> str | None:
    """Optional: any OpenAI-compatible endpoint (Groq, Gemini, Cloudflare Workers AI...). Skipped if not configured."""
    key, base, model = os.getenv("LLM_API_KEY"), os.getenv("LLM_BASE_URL"), os.getenv("LLM_MODEL")
    if not (key and base and model):
        return None
    facts = "\n".join(f"- {result['why']}" for result in results)
    msg = ("You are a relocation advisor. Using ONLY these Qloo-derived facts, write a warm 120-word "
           f"recommendation for someone whose tastes are {profile}. Facts:\n{facts}")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            response = await client.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "max_tokens": 300, "messages": [{"role": "user", "content": msg}]})
            return response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
