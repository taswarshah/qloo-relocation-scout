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
import logging

log = logging.getLogger("scout.qloo")

load_dotenv()
BASE = os.getenv("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
KEY = os.getenv("QLOO_API_KEY", "")
SEM = asyncio.Semaphore(int(os.getenv("QLOO_CONCURRENCY", "6")))
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
    id: str
    category: str


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
    radius_km: int | None
    coverage: dict[str, int]


class ComparisonRow(TypedDict):
    """Per-taste affinity for the user's candidate city vs the top pick."""

    taste: str
    candidate: float
    top_pick: float


class Comparison(TypedDict, total=False):
    """'Why not X?' — the user's own candidate city scored against the top pick."""

    city: CityResult
    top_pick: str
    gap: float
    rows: list[ComparisonRow]
    verdict: str
    error: str


class ScoutResult(TypedDict, total=False):
    """Successful scout payload or the existing unmatched-profile error."""

    matched: list[MatchedTaste]
    lifestyle_tags: list[LifestyleTag]
    cities: list[CityResult]
    narrative: str | None
    trace: list[str]
    timings: dict[str, int]
    compare: Comparison
    health: dict[str, Any]
    blend: dict[str, str]
    warning: str
    error: str


_CLIENT: httpx.AsyncClient | None = None


def _shared_client() -> httpx.AsyncClient:
    """One pooled client per event loop: keep-alive avoids a TLS handshake per request."""
    global _CLIENT
    loop = asyncio.get_running_loop()
    if _CLIENT is None or _CLIENT.is_closed or getattr(_CLIENT, "_loop", None) is not loop:
        _CLIENT = httpx.AsyncClient(base_url=BASE, headers={"X-Api-Key": KEY},
                                    timeout=httpx.Timeout(6.0, connect=3.0),
                                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        _CLIENT._loop = loop  # type: ignore[attr-defined]
    return _CLIENT


class Qloo:
    """Request-scoped Qloo client with memory-only caching and rate backoff."""

    def __init__(self) -> None:
        self.c = _shared_client()
        self.calls = 0
        self.errors = 0
        self.cache_hits = 0
        # Per-endpoint health, so upstream failures are never mistaken for "no matches".
        self.stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "ok": 0, "retries": 0, "timeouts": 0, "http_errors": 0, "rate_limited": 0, "network_errors": 0})

    @property
    def failed(self) -> int:
        """Calls that returned no usable data because of an upstream problem."""
        return sum(v["timeouts"] + v["http_errors"] + v["rate_limited"] + v["network_errors"] for v in self.stats.values())

    def health(self) -> dict[str, Any]:
        """Safe, aggregate health report (no URLs, params, keys or upstream bodies)."""
        return {"partial": self.failed > 0, "failed_calls": self.failed,
                "endpoints": {path: dict(v) for path, v in self.stats.items()}}

    async def get(self, path: str, params: Parameters) -> dict[str, Any]:
        """Fetch a permitted endpoint, retrying only rate-limited requests."""
        cache_key = (path, tuple(sorted(params.items())))
        hit = _CACHE.get(cache_key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            _CACHE.move_to_end(cache_key)
            self.cache_hits += 1
            return hit[1]
        self.calls += 1
        stat = self.stats[path]
        stat["calls"] += 1
        async with SEM:
            transient_retried = False
            attempt = 0
            while True:
                try:
                    response = await self.c.get(path, params=params)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    # GETs are idempotent: one quick retry rides out a cold-connection stall.
                    if not transient_retried:
                        transient_retried = True
                        stat["retries"] += 1
                        continue
                    if isinstance(exc, httpx.TimeoutException):
                        stat["timeouts"] += 1
                        log.warning("qloo %s timed out", path)  # never log params, URL query or key
                    else:
                        stat["network_errors"] += 1
                        log.warning("qloo %s failed: %s", path, type(exc).__name__)
                    return {}
                except httpx.HTTPError as exc:
                    stat["network_errors"] += 1
                    log.warning("qloo %s failed: %s", path, type(exc).__name__)
                    return {}
                if response.status_code != 429 or attempt >= 3:
                    break
                attempt += 1
                stat["retries"] += 1
                # Short, capped backoff keeps p95 under the 8 s budget.
                await asyncio.sleep(min(float(response.headers.get("retry-after") or 0) or 0.4 * attempt, 2.0))
        if response.status_code == 429:
            stat["rate_limited"] += 1
            log.warning("qloo %s rate limited after retries", path)
            return {}
        if response.status_code >= 400:
            stat["http_errors"] += 1
            log.warning("qloo %s returned HTTP %s", path, response.status_code)
            return {}
        try:
            data = response.json()
        except ValueError:
            stat["http_errors"] += 1
            return {}
        stat["ok"] += 1
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

VIBE_JUNK = re.compile(r"midday|convenience|delivery|disinfect|contact|mask|staff|sanitiz|temperature|alcohol|reservation|payment|credit|debit|wheelchair|restroom|parking|checks?$|dine-?in")

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


def place_matches_city(place: QlooEntity, city: QlooEntity, by_radius: bool = False) -> bool:
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
    if by_radius:  # coordinates already bound the area; nearby towns are fine
        return True
    if mentions(address_text, city["name"]):
        return True
    localities = [fields.get(key) for fields in (geocode, address_fields)
                  for key in ("city", "locality") if fields.get(key)]
    if localities:
        return any(mentions(locality, city["name"]) for locality in localities)
    return True


IRRELEVANT_CATEGORY = re.compile(
    r"\b(hotel|motel|lodging|resort hotel|extended stay|apartment|condominium|real estate|school|educational|"
    r"kindergarten|preschool|university|college|office|supermarket|grocery|discount store|dollar store|variety store|convenience store|gas station|fuel|subway station|train station|bus (stop|station)|transit|"
    r"rest stop|parking|bank|atm|hospital|clinic|medical|dentist|pharmacy|car (rental|dealer|repair|wash)|"
    r"storage|post office|government|courthouse|police|embassy|conference center|warehouse|factory|airport)\b")


def place_categories(place: QlooEntity) -> list[str]:
    """Qloo category tags for a venue (urn:tag:category:place)."""
    return [tag.get("name") or "" for tag in (place.get("tags") or [])
            if "category" in (tag.get("type") or "") or "category" in (tag.get("id") or "")]


ADDRESS_LIKE = re.compile(r"^\d|#|\b(calle|avenida|street|st\.|road|rd\.)\s*\d", re.IGNORECASE)


def is_relevant_place(place: QlooEntity) -> bool:
    """Drop venues a relocator wouldn't seek out (hotels, schools, offices, gas stations, transit...).

    Kept if any category is relevant (e.g. a hotel that is also a 'Bar' or 'Restaurant').
    """
    if ADDRESS_LIKE.search(place.get("name") or ""):
        return False
    categories = [normalize(c) for c in place_categories(place) if c]
    if not categories:
        return True
    return any(not IRRELEVANT_CATEGORY.search(c) for c in categories)


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


def local_relevant(places: list[QlooEntity], city: QlooEntity, by_radius: bool = False) -> list[QlooEntity]:
    """Places that are in the right area and worth recommending."""
    city_key = normalize(city.get("name") or "")
    def is_city_itself(p: QlooEntity) -> bool:  # e.g. a venue literally named "San Juan, Puerto Rico"
        name = normalize(p.get("name") or "")
        return name == city_key or name.startswith(city_key + ",")
    return [p for p in places if place_matches_city(p, city, by_radius) and is_relevant_place(p)
            and not is_city_itself(p)]


def summarize_places(places: list[QlooEntity], city: QlooEntity,
                     by_radius: bool = False) -> tuple[list[Neighborhood], list[str], list[str]]:
    """Group cleaned neighborhoods and rank distinct local spots (by taste affinity) and vibe tags."""
    hoods: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}
    vibe: Counter[str] = Counter()
    local_places = sorted(local_relevant(places, city, by_radius), key=affinity, reverse=True)
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
    candidate_name = (profile.get("compare") or "").strip()[:60]
    (entities, tags), candidate = await asyncio.gather(
        resolve_profile(q, profile, trace),
        q.search(candidate_name, "urn:entity:destination") if candidate_name else _none())
    if not entities and not tags:
        if q.failed:  # upstream trouble, not a genuine no-match
            return {"error": "Qloo is not responding right now, so we couldn't read your tastes. Please try again shortly.",
                    "trace": trace, "health": q.health()}
        return {"error": "Couldn't match any of your tastes in Qloo. Try specific artists, films or restaurants.", "trace": trace,
                "health": q.health()}
    signals = _signals(entities, tags)
    country_filter: Parameters = {}
    if profile.get("country"):
        country_filter["filter__geocode__country_code"] = profile["country"].upper()

    t1 = time.perf_counter()
    # Round 2: rank destinations on the combined profile (+ score the user's candidate city).
    cand_id = candidate["entity_id"] if candidate else None
    ranked = q.insights(filter__type="urn:entity:destination", take=n_cities * 3, **signals, **country_filter)
    if cand_id:
        destinations, cand_hits = await asyncio.gather(
            ranked, q.insights(filter__type="urn:entity:destination", filter__results__entities=cand_id, take=1, **signals))
    else:
        destinations, cand_hits = await ranked, []
    trace.append(f"insights(destination, all signals) → {len(destinations)} candidates")
    cities = unique_cities(destinations, n_cities)
    cand_entity = cand_hits[0] if cand_hits else None
    if cand_entity:
        trace.append(f"insights(destination, filter.results.entities={cand_entity['name']}) → {int(affinity(cand_entity)*100)}/100")
    t2 = time.perf_counter()

    # Round 3 (all parallel): exact per-taste affinity for exactly the shown cities via
    # filter.results.entities, plus taste-matched venues around each city.
    if cand_entity and any(c["entity_id"] == cand_entity["entity_id"] for c in cities):
        cand_entity = next(c for c in cities if c["entity_id"] == cand_entity["entity_id"])
        shown = cities  # already shown: don't fetch its venues twice
    else:
        shown = cities + ([cand_entity] if cand_entity else [])
    ids = ",".join(c["entity_id"] for c in shown)
    attribution = [q.insights(filter__type="urn:entity:destination", signal__interests__entities=e["id"],
                              filter__results__entities=ids, take=len(shown)) for e in entities]
    place_jobs = [find_places(q, c, signals) for c in shown]
    gathered = await asyncio.gather(*attribution, *place_jobs)
    # Keyed by Qloo entity ID so two tastes with the same display name never overwrite each other.
    # An empty result means the call failed (the filter always returns the shown cities), so that
    # taste is left out as "not scored" rather than reported as a real 0.
    per_taste: dict[str, dict[str, float]] = {
        e["id"]: {m["entity_id"]: affinity(m) for m in matches}
        for e, matches in zip(entities, gathered[:len(entities)]) if matches}
    trace.append(f"insights(destination, filter.results.entities=shown cities) per-taste x{len(entities)} for attribution")
    built = [build_city(c, found, per_taste, entities) for c, found in zip(shown, gathered[len(entities):])]
    if cand_entity and len(shown) == len(cities):
        cand_result = next(b for b in built if b["city"] == cand_entity["name"])
    else:
        cand_result = built.pop() if cand_entity else None
    results = built
    t3 = time.perf_counter()
    narrative = await llm_summary(profile, results)
    timings = {"resolve_ms": round((t1 - t0) * 1000), "rank_ms": round((t2 - t1) * 1000),
               "neighborhoods_ms": round((t3 - t2) * 1000), "total_ms": round((time.perf_counter() - t0) * 1000),
               "qloo_calls": q.calls, "cache_hits": q.cache_hits, "qloo_failed": q.failed}
    trace.append(f"done in {timings['total_ms']} ms · {q.calls} Qloo calls · {q.cache_hits} cache hits")
    out: ScoutResult = {"matched": entities, "lifestyle_tags": tags, "cities": results, "narrative": narrative,
                        "trace": trace, "timings": timings, "health": q.health()}
    if q.failed:
        out["warning"] = ("Some Qloo requests failed or timed out, so these results may be incomplete. "
                          "Try again in a moment.")
    if candidate_name:
        scored = [e for e in entities if e["id"] in per_taste]
        out["compare"] = compare(candidate_name, cand_result, results[0] if results else None, scored)
    return out


async def _none() -> None:
    return None


def _signals(entities: list[MatchedTaste], tags: list[LifestyleTag]) -> Parameters:
    signals: Parameters = {}
    if entities:
        signals["signal__interests__entities"] = ",".join(entity["id"] for entity in entities)
    if tags:
        signals["signal__interests__tags"] = ",".join(tag["id"] for tag in tags)
    return signals


async def blend(profile_a: dict[str, str], profile_b: dict[str, str], n_cities: int = 4,
                labels: tuple[str, str] = ("You", "Partner")) -> ScoutResult:
    """Couple/family mode: find cities where the *less* satisfied person is happiest.

    Each person's tastes are resolved separately. A candidate pool is ranked on the combined
    signals, then every candidate is scored for each person alone (filter.results.entities),
    and cities are ordered by min(score_a, score_b), a compromise rather than one person's list.
    """
    q = Qloo()
    trace: list[str] = []
    t0 = time.perf_counter()
    (ea, ta), (eb, tb) = await asyncio.gather(resolve_profile(q, profile_a, trace), resolve_profile(q, profile_b, trace))
    if not (ea or ta) or not (eb or tb):
        who = labels[0] if not (ea or ta) else labels[1]
        return {"error": f"Couldn't match any of {who}'s tastes in Qloo.", "trace": trace, "health": q.health()}
    country = (profile_a.get("country") or profile_b.get("country") or "").upper()
    country_filter: Parameters = {"filter__geocode__country_code": country} if country else {}
    combined = _signals(ea + [e for e in eb if e["id"] not in {x["id"] for x in ea}],
                        ta + [t for t in tb if t["id"] not in {x["id"] for x in ta}])
    destinations = await q.insights(filter__type="urn:entity:destination", take=n_cities * 4, **combined, **country_filter)
    pool = unique_cities(destinations, n_cities * 3)
    trace.append(f"insights(destination, both profiles) → {len(pool)} candidate cities")
    if not pool:
        return {"error": "Qloo found no destinations for this pair of profiles.", "trace": trace, "health": q.health()}
    ids = ",".join(c["entity_id"] for c in pool)
    hits_a, hits_b = await asyncio.gather(
        q.insights(filter__type="urn:entity:destination", filter__results__entities=ids, take=len(pool), **_signals(ea, ta)),
        q.insights(filter__type="urn:entity:destination", filter__results__entities=ids, take=len(pool), **_signals(eb, tb)))
    score_a = {m["entity_id"]: affinity(m) for m in hits_a}
    score_b = {m["entity_id"]: affinity(m) for m in hits_b}
    trace.append(f"insights(destination, filter.results.entities=pool) scored separately for {labels[0]} and {labels[1]}")
    ranked = sorted(pool, key=lambda c: (-min(score_a.get(c["entity_id"], 0), score_b.get(c["entity_id"], 0)), -affinity(c)))
    shown = ranked[:n_cities]
    found = await asyncio.gather(*[find_places(q, c, combined) for c in shown])
    per_person = {"person-a": score_a, "person-b": score_b} if hits_a and hits_b else {}
    people: list[MatchedTaste] = [{"id": "person-a", "name": labels[0], "category": "person"},
                                  {"id": "person-b", "name": labels[1], "category": "person"}]
    cities = [build_city(c, f, per_person, people) for c, f in zip(shown, found)]
    total = round((time.perf_counter() - t0) * 1000)
    trace.append(f"done in {total} ms · {q.calls} Qloo calls · {q.cache_hits} cache hits")
    out: ScoutResult = {"matched": ea + eb, "lifestyle_tags": ta + tb, "cities": cities, "narrative": None, "trace": trace,
                        "timings": {"total_ms": total, "qloo_calls": q.calls, "cache_hits": q.cache_hits, "qloo_failed": q.failed},
                        "health": q.health(), "blend": {"a": labels[0], "b": labels[1]}}
    if q.failed:
        out["warning"] = "Some Qloo requests failed or timed out, so these results may be incomplete. Try again in a moment."
    return out


async def spots(profile: dict[str, str], city_name: str, focus: str = "") -> dict[str, Any]:
    """Zoom into one city for this profile, optionally nudged by a focus keyword ("coffee", "jazz bars")."""
    q = Qloo()
    trace: list[str] = []
    (entities, tags), city_hit, focus_tags = await asyncio.gather(
        resolve_profile(q, profile, trace), q.search(city_name[:60], "urn:entity:destination"),
        q.tags(focus[:40]) if focus else _none())
    if not city_hit:
        return {"error": f"Couldn't find '{city_name}' as a destination in Qloo.", "health": q.health()}
    focus_tag = (focus_tags or [None])[0]
    if focus_tag:
        tags = tags + [{"id": focus_tag["id"], "name": focus_tag["name"], "word": focus}]
        trace.append(f"tags: '{focus}' → {focus_tag['id']}")
    if not entities and not tags:
        return {"error": "Couldn't match any tastes in Qloo.", "health": q.health()}
    signals = _signals(entities, tags)
    scored = await q.insights(filter__type="urn:entity:destination", filter__results__entities=city_hit["entity_id"],
                              take=1, **signals)
    city = scored[0] if scored else city_hit
    places, by_radius, radius_km = await find_places(q, city, signals)
    neighborhoods, top_spots, vibe = summarize_places(places, city, by_radius)
    trace.append(f"insights(place, near {city.get('name')}) → {len(places)} venues")
    return {"city": city.get("name"), "affinity": round(affinity(city), 3), "focus": focus_tag["name"] if focus_tag else None,
            "neighborhoods": neighborhoods, "top_spots": top_spots, "vibe": vibe, "radius_km": radius_km,
            "trace": trace, "health": q.health()}


def compare(name: str, cand: CityResult | None, top: CityResult | None,
            entities: list[MatchedTaste]) -> Comparison:
    """Explain 'why not X?' with Qloo affinity: overall gap plus per-taste deltas vs the top pick."""
    if not cand:
        return {"error": f"Couldn't find '{name}' as a destination in Qloo."}
    if not top:
        return {"city": cand, "top_pick": "", "gap": 0.0, "rows": [], "verdict": cand["why"]}
    cand_scores = {d["id"]: d["affinity"] for d in cand["drivers"]}
    top_scores = {d["id"]: d["affinity"] for d in top["drivers"]}
    rows: list[ComparisonRow] = [{"taste": e["name"], "candidate": cand_scores.get(e["id"], 0.0),
                                  "top_pick": top_scores.get(e["id"], 0.0)} for e in entities]
    gap = round(top["affinity"] - cand["affinity"], 3)
    if cand["city"] == top["city"]:
        verdict = f"{cand['city']} is already your top pick."
    elif gap <= 0:
        verdict = f"{cand['city']} matches your taste as well as {top['city']} ({int(cand['affinity']*100)} vs {int(top['affinity']*100)})."
    else:
        losers = sorted((r for r in rows if r["top_pick"] > r["candidate"]), key=lambda r: r["candidate"] - r["top_pick"])[:2]
        why = (" The biggest gaps: " + ", ".join(f"{r['taste']} ({int(r['candidate']*100)} vs {int(r['top_pick']*100)})" for r in losers) + ".") if losers else ""
        verdict = (f"{cand['city']} scores {int(cand['affinity']*100)}/100, {int(round(gap*100))} points behind "
                   f"{top['city']} ({int(top['affinity']*100)}).{why}")
    return {"city": cand, "top_pick": top["city"], "gap": gap, "rows": rows, "verdict": verdict}


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
        country = geocode.get("country_code")
        if key in seen or any(near(destination, kept) and same_country(country, ((kept.get("properties") or {}).get("geocode") or {}).get("country_code"))
                              for kept in cities):
            continue
        seen.add(key)
        cities.append(destination)
        if len(cities) >= limit:
            break
    return cities


def build_city(city: QlooEntity, found: tuple[list[QlooEntity], bool, int | None],
               per_taste: dict[str, dict[str, float]], entities: list[MatchedTaste] | None = None) -> CityResult:
    """Build one city card from destination affinity, per-taste attribution and local places."""
    geocode = (city.get("properties") or {}).get("geocode") or {}
    location = f"{city['name']}, {geocode.get('admin1_region') or ''} {geocode.get('country_code') or ''}".strip()
    places, by_radius, radius_km = found
    neighborhoods, top_spots, vibe = summarize_places(places, city, by_radius)
    names = {e["id"]: e["name"] for e in entities or []}
    categories = {e["id"]: e["category"] for e in entities or []}
    drivers = sorted(((tid, matches.get(city["entity_id"], 0)) for tid, matches in per_taste.items()),
                     key=lambda item: -item[1])
    relevant = local_relevant(places, city, by_radius)
    result: CityResult = {
        "city": city["name"], "location": location, "country": geocode.get("country_code"),
        "affinity": round(affinity(city), 3),
        "city_tags": [tag["name"] for tag in city.get("tags") or []][:5],
        "neighborhoods": neighborhoods, "top_spots": top_spots, "vibe": vibe,
        "drivers": [{"taste": names.get(tid, tid), "id": tid, "category": categories.get(tid, ""), "affinity": round(score, 3)}
                    for tid, score in drivers if score > 0][:MAX_ENTITIES],
        "why": "", "radius_km": radius_km,
        "coverage": {"places_returned": len(places), "places_relevant": len(relevant),
                     "neighborhoods": len(neighborhoods), "tastes_scored": sum(1 for _, s in drivers if s > 0),
                     "tastes_total": len(entities) if entities else len(per_taste)},
    }
    result["why"] = explain(result, [])
    return result


URBAN_RADIUS_KM = 15
WIDE_RADIUS_KM = 60
MIN_LOCAL_SPOTS = 3


def point(city: QlooEntity) -> str | None:
    """WKT point for a destination's coordinates (Qloo filter.location)."""
    loc = city.get("location") or {}
    if isinstance(loc.get("lat"), (int, float)) and isinstance(loc.get("lon"), (int, float)):
        return f"POINT({loc['lon']} {loc['lat']})"
    return None


async def find_places(q: Qloo, city: QlooEntity, signals: Parameters) -> tuple[list[QlooEntity], bool, int | None]:
    """Find taste-matched venues around a city.

    Uses the destination's own coordinates (filter.location + radius), because a bare name is
    ambiguous ("Flores" resolves to a Buenos Aires barrio). Widens to 60 km for small towns with
    thin data, and only falls back to a name query if coordinates are missing.
    """
    wkt = point(city)
    if not wkt:
        return await q.insights(filter__type="urn:entity:place", filter__location__query=city["name"],
                                take=30, **signals), False, None
    places: list[QlooEntity] = []
    for radius in (URBAN_RADIUS_KM, WIDE_RADIUS_KM):
        places = await q.insights(filter__type="urn:entity:place", filter__location=wkt,
                                  filter__location__radius=radius * 1000, take=30, **signals)
        if len(local_relevant(places, city, True)) >= MIN_LOCAL_SPOTS:
            return places, True, radius
    return places, True, WIDE_RADIUS_KM


def explain(result: CityResult, tags: list[LifestyleTag]) -> str:
    """Rule-based explanation grounded only in Qloo data (works with no LLM)."""
    parts = [f"{result['city']} scores {int(result['affinity']*100)}/100 taste affinity with your profile."]
    if result["drivers"]:
        parts.append("Strongest pull: " + ", ".join(f"{driver['taste']} ({int(driver['affinity']*100)})" for driver in result["drivers"][:3]) + ".")
    if result["city_tags"]:
        parts.append("Known for: " + ", ".join(result["city_tags"][:3]).lower() + ".")
    if result["neighborhoods"]:
        neighborhood = result["neighborhoods"][0]
        parts.append(f"Start in {neighborhood['name']}: spots like {', '.join(neighborhood['spots'][:2])} match your taste.")
    elif result.get("top_spots"):
        area = f" within {result['radius_km']} km" if result.get("radius_km", 0) and result["radius_km"] > URBAN_RADIUS_KM else ""
        parts.append(f"Top taste-matched spots{area}: " + ", ".join(result["top_spots"][:2]) + ".")
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
            if response.status_code != 200:
                return None
            return response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
