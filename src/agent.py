"""Relocation Scout agent: turns a taste profile into city + neighborhood picks using Qloo's Insights API."""
import asyncio
import math
import os
import re
import time
from collections import OrderedDict
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
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
# The hackathon key allows 5 requests/second (x-second-ratelimit-limit). Pacing to it avoids the 429s
# that bursting caused (~60% of first attempts) and the backoff sleeps that came with them.
# Strict spacing (burst 1) measured best: Qloo counts per fixed one-second window, so bursts still tripped it.
QLOO_RATE_PER_S = float(os.getenv("QLOO_RATE_PER_S", "4.5"))
QLOO_BURST = int(os.getenv("QLOO_BURST", "1"))


class _Pacer:
    """Token bucket shared by all requests in this process (one API key, one limit)."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.burst = rate, burst
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self.lock: asyncio.Lock | None = None

    async def wait(self) -> None:
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                await asyncio.sleep((1 - self.tokens) / self.rate)
                self.updated = time.monotonic()
                self.tokens = 1.0
            self.tokens -= 1

    def drain(self) -> None:
        """Qloo said we're over the limit (another isolate or user shares the key): start from empty."""
        self.tokens = 0.0
        self.updated = time.monotonic()


_PACER = _Pacer(QLOO_RATE_PER_S, QLOO_BURST)

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


class LifestyleTag(TypedDict, total=False):
    """A lifestyle keyword resolved to Qloo tags.

    Qloo ignores tags passed as `signal.interests.tags` when ranking destinations and places
    (measured: same order, scores only shift down), so lifestyle words are applied as
    `filter.tags` instead: a destination genre tag narrows the cities, place tags narrow the venues.
    """

    id: str
    name: str
    word: str
    destination_tag: str
    place_tags: list[str]


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
    venue_filter: list[str]
    mislocated: bool
    coverage: dict[str, int]


# find_places result: (places, by_radius, radius_km, names of the lifestyle filters that shaped them)
Found = tuple[list[QlooEntity], bool, int | None, list[str]]


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
                    await _PACER.wait()
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
                _PACER.drain()
                # Wait exactly until Qloo's per-second window resets (header), capped to keep p95 in budget.
                reset = response.headers.get("x-second-ratelimit-reset") or response.headers.get("retry-after") or ""
                try:
                    delay = float(reset)
                except ValueError:
                    delay = 0.4 * attempt
                await asyncio.sleep(min(max(delay, 0.05) + 0.05, 2.0))
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
        """Resolve one free-text taste to the closest-named entity, or None if nothing is close.

        Qloo search always returns *something* ("zzqxv qwerty" → the band Qwerty), so the top
        few results are checked against the query and weak matches are rejected.
        """
        data = await self.get("/search", {"query": query, "types": entity_type, "take": 5})
        return best_match(query, data.get("results") or [])

    async def tags(self, query: str, parent: str = "urn:entity:place") -> list[QlooEntity]:
        """Candidate tags for a lifestyle word, limited to tags that apply to `parent` entities."""
        data = await self.get("/v2/tags", {"filter.query": query, "filter.parents.types": parent, "take": 8})
        return (data.get("results") or {}).get("tags", [])

    async def insights(self, **params: str | int) -> list[QlooEntity]:
        """Query destination or place insights with dotted Qloo parameters."""
        query = {key.replace("__", "."): value for key, value in params.items()}
        data = await self.get("/v2/insights", query)
        return (data.get("results") or {}).get("entities", [])

NOISE = re.compile(r"^(place|morning|afternoon|evening|night|routine visit|commercial|destination|locals|takeout|delivery|dine in|curbside.*|.*accessible.*|.*card.*|.*parking.*|cash.*|\d.*|restroom.*|wi-?fi.*|nfc.*)$")

VIBE_JUNK = re.compile(r"midday|convenience|delivery|disinfect|contact|mask|staff|sanitiz|temperature|alcohol|reservation|payment|credit|debit|wheelchair|restroom|parking|checks?$|dine-?in"
                       r"|dining drinking|full bar|^bars?$|tourist attraction|outdoor seating|good for|^restaurant$"
                       r"|onsite|free admission|^moderate$|^inexpensive$|^expensive$|large group|groups? of friends|family friendly|^social$")

MAX_ITEMS_PER_FIELD = 5
MAX_ENTITIES = 12
MAX_TAGS = 5
ADMIN_PREFIX = re.compile(r"^(?:upzs?|localidad)(?:\s+|$)", re.IGNORECASE)
COUNTRY_ALIASES = {
    "US": ("united states", "united states of america", "usa", "u.s.a.", "estados unidos"),
    "PR": ("puerto rico",),
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


STOPWORDS = {"the", "a", "an", "of", "and", "in", "on", "la", "le", "el", "de", "&"}


def _tokens(value: str) -> list[str]:
    words = re.sub(r"[^\w& ]", " ", normalize(value)).split()
    return [t for t in words if t not in STOPWORDS] or words  # "The The", "A" are names too


def match_quality(query: str, name: str) -> float:
    """How well an entity name answers a query, 0-1. Every meaningful query word must appear
    (typos allowed) for a high score, so extra garbage words sink the match."""
    q, n = _tokens(query), _tokens(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    whole = SequenceMatcher(None, " ".join(q), " ".join(n)).ratio()
    found = sum(1 for word in q if any(SequenceMatcher(None, word, other).ratio() >= 0.85 for other in n))
    coverage = found / len(q)
    return max(whole, 0.9 * coverage if coverage == 1 else 0.6 * coverage)


MIN_MATCH = 0.8


def best_match(query: str, results: list[QlooEntity]) -> QlooEntity | None:
    """The closest-named result (ties keep Qloo's order), or None below MIN_MATCH."""
    scored = [(match_quality(query, r.get("name") or ""), -i, r) for i, r in enumerate(results)]
    if not scored:
        return None
    score, _, pick = max(scored, key=lambda item: item[:2])
    return pick if score >= MIN_MATCH else None


def display_name(name: str) -> str:
    """Short label for long venue names ("Biryani Zone, Marathahalli, , Hyderabadi Dum..." → "Biryani Zone")."""
    return name.split(",")[0].strip() if len(name) > 40 and "," in name else name


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


def address_text(place: QlooEntity) -> str:
    address = (place.get("properties") or {}).get("address") or ""
    return address if isinstance(address, str) else ", ".join(v for v in address.values() if isinstance(v, str))


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


LODGING_CATEGORY = re.compile(r"\b(hotel|motel|lodging|hostel|inn|ryokan|bed (and )?breakfast|guest ?house|resort|"
                              r"extended stay|vacation (home )?rental|holiday (home|apartment)|serviced apartment)\b")
IRRELEVANT_CATEGORY = re.compile(
    r"\b(hotel|motel|lodging|resort hotel|extended stay|apartment|condominium|real estate|school|educational|"
    r"kindergarten|preschool|university|college|office|supermarket|grocery|discount store|dollar store|variety store|convenience store|gas station|fuel|subway station|train station|bus (stop|station)|transit|"
    r"rest stop|parking|bank|atm|hospital|clinic|medical|dentist|pharmacy|car (rental|dealer|repair|wash)|"
    r"storage|post office|government|courthouse|police|embassy|conference center|warehouse|factory|airport|"
    r"wedding|banquet|event planner|meeting planning|student housing|dormitory|residence hall|tutoring|child care|day care|language school|"
    r"health food store|organic food store|natural goods|delivery service|rental agency|travel agency)\b")


def place_categories(place: QlooEntity) -> list[str]:
    """Qloo category tags for a venue (urn:tag:category:place)."""
    return [tag.get("name") or "" for tag in (place.get("tags") or [])
            if "category" in (tag.get("type") or "") or "category" in (tag.get("id") or "")]


LODGING_NAME = re.compile(r"\b(hotel|motel|inn|suites|resort|lodge|apartments?)\b", re.IGNORECASE)
HOSPITALITY_NAME = re.compile(r"\b(bar|restaurant|caf[eé]|coffee|kitchen|lounge|bistro|brasserie|pub|grill|rooftop|izakaya|eatery)\b", re.IGNORECASE)
ADDRESS_LIKE = re.compile(r"^\d|#|\b(calle|avenida|street|st\.|road|rd\.)\s*\d", re.IGNORECASE)


def is_relevant_place(place: QlooEntity) -> bool:
    """Drop venues a relocator wouldn't seek out (hotels, schools, offices, gas stations, transit...).

    Kept if any category is relevant (e.g. a hotel that is also a 'Bar' or 'Restaurant').
    """
    name = place.get("name") or ""
    if ADDRESS_LIKE.search(name):
        return False
    categories = [normalize(c) for c in place_categories(place) if c]
    hospitality = HOSPITALITY_NAME.search(name) or any(HOSPITALITY_NAME.search(c) for c in categories)
    # Lodging by name ("The Nishat Hotel") unless it's really a bar/restaurant: the name says so ("Kex Hostel
    # Restaurant") or its categories do without also saying hotel. Lodging by category ("Hotel", "Bed breakfast")
    # unless a category also says restaurant/café (a ryokan with a dining room).
    lodging_category = any(LODGING_CATEGORY.search(c) for c in categories)
    if LODGING_NAME.search(name) and not HOSPITALITY_NAME.search(name) and (lodging_category or not hospitality):
        return False
    if lodging_category and not hospitality:
        return False
    if not categories:
        return True
    # Mostly-irrelevant venues go (a supermarket that also has a "Bakery" tag, a hotel that's a "Wedding venue").
    irrelevant = sum(1 for c in categories if IRRELEVANT_CATEGORY.search(c))
    return irrelevant * 2 <= len(categories) or (bool(hospitality) and irrelevant < len(categories))


# Tag kinds that describe character and mood. Everything else on a venue (amenities, payments,
# service options, room types, menus, meal times) reads as noise in a "local vibe" list.
VIBE_KINDS = ("ambience", "neighborhood_characteristic", "setting", "decor", "culinary_style", "inclusivity", "interests")
VIBE_BLAND = re.compile(r"^(clean|unique|commercial|business|touristic|tourists|modern|casual|place|urban|"
                        r"city center|central|convenient|natural|traditional|authentic)$")


def vibe_tag(tag: QlooEntity) -> str | None:
    """Keep descriptive mood/character tags; drop amenities, services, numbers and filler."""
    name = (tag.get("name") or "").strip()
    kind = (tag.get("type") or "").casefold()
    if kind and not any(k in kind for k in VIBE_KINDS):
        return None
    if (len(name) < 3 or len(name) > 28 or any(char.isdigit() for char in name) or VIBE_JUNK.search(name.casefold())
            or NOISE.search(name.casefold())):
        return None
    return name


def pct(score: float) -> int:
    """0-1 affinity as a 0-100 integer, rounded the same way the UI rounds it."""
    return round(score * 100)


def affinity(entity: QlooEntity) -> float:
    """Read the optional taste-affinity score, defaulting to zero."""
    return float((entity.get("query") or {}).get("affinity") or 0)


def local_relevant(places: list[QlooEntity], city: QlooEntity, by_radius: bool = False) -> list[QlooEntity]:
    """Places that are in the right area and worth recommending."""
    city_key = normalize(city.get("name") or "")
    geocode = (city.get("properties") or {}).get("geocode") or {}
    regions = {normalize(r) for r in [geocode.get("admin1_region") or "", *COUNTRY_ALIASES.get(geocode.get("country_code") or "", ())] if r}
    def is_city_itself(p: QlooEntity) -> bool:  # a "venue" that is a town: "San Juan, Puerto Rico", "Cambria, California"
        name = normalize(p.get("name") or "")
        return name == city_key or name.startswith(city_key + ",") or any(name.endswith(", " + r) for r in regions)
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
        name = display_name((place.get("name") or "").strip())
        neighborhood = clean_neighborhood_name((place.get("properties") or {}).get("neighborhood"))
        if name and neighborhood:
            key = normalize(neighborhood)
            labels.setdefault(key, neighborhood)
            if name not in hoods[key]:
                hoods[key].append(name)
        for descriptive in {v for tag in place.get("tags") or [] if (v := vibe_tag(tag))}:
            vibe[descriptive] += 1
    ranked_hoods = sorted(hoods, key=lambda key: -len(hoods[key]))[:3]
    neighborhoods: list[Neighborhood] = [{"name": labels[key], "spots": hoods[key][:4]} for key in ranked_hoods]
    top_spots: list[str] = []
    seen_spots: set[str] = set()
    for place in sorted(local_places, key=affinity, reverse=True):
        name = display_name((place.get("name") or "").strip())
        key = normalize(name)
        if key and key not in seen_spots:
            seen_spots.add(key)
            top_spots.append(name)
        if len(top_spots) == 4:
            break
    # Distinctive words first ("Creative district" beats "Casual"); bland ones only fill gaps.
    ranked_vibe = sorted(vibe, key=lambda v: (bool(VIBE_BLAND.match(v.casefold())), -vibe[v]))
    return neighborhoods, top_spots, ranked_vibe[:6]


# Lifestyle word → Qloo destination genre tag (the small fixed set Qloo puts on cities).
DESTINATION_TAGS: list[tuple[re.Pattern[str], str]] = [(re.compile(pattern), "urn:tag:genre:destination:" + tag) for pattern, tag in [
    (r"\b(beach|surf|ocean|seaside|coast|island)", "beaches"),
    (r"\b(nature|forest|wildlife|parks?\b|green)", "nature"),
    (r"\b(hik|trail|outdoor|camp|climb|kayak|cycl|bik|adventure)", "outdoor_activities"),
    (r"\b(ski|snow)", "snow_ski"),
    (r"\b(nightlife|club|party|bars?\b|cocktail|danc)", "great_nightlife"),
    (r"\b(art|galler|museum|culture|design)", "art_culture"),
    (r"\b(theat|concert|jazz|opera|perform|live music|comedy)", "entertainment_performing_arts"),
    (r"\b(wine|vineyard)", "wine_country"),
    (r"\b(kid|family|families|child)", "kids"),
    (r"\b(food|restaurant|cuisine|cafe|coffee|dining|vegan)", "foodie_destination"),
    (r"\b(histor|heritage|old town|castle)", "history"),
    (r"\b(shop|boutique|fashion|market)", "great_shopping"),
    (r"\barchitect", "great_architecture"),
    (r"\b(romant|couple)", "romantic_getaways"),
    (r"\b(scenic|view|picturesque|lake|landscape)", "scenic_picturesque"),
    (r"\b(urban|city|walkab|metropol|cowork|tech|transit)", "urban_destinations"),
]]
# Lifestyle words whose literal tag search lands on the wrong thing ("kids" → "Kids' menu").
PLACE_TAG_QUERY = {"kids": "good for kids", "kid": "good for kids", "family": "good for kids", "children": "good for kids",
                   "vinyl": "record store", "records": "record store", "coffee": "coffee shop", "history": "history museum"}
# Venue tag kinds that describe what a place *is* or is good for, most specific first.
PLACE_TAG_KINDS = ("category:place:", "genre:place:", "activities:place:", "children:place:good", "good_for:qloo:",
                   "inclusivity:place:", "ambience:qloo:", "neighborhood_characteristic:qloo:", "setting:qloo:",
                   "dietary_option:qloo:", "nearby_attraction:qloo:")


def destination_tag(word: str) -> str | None:
    folded = normalize(word)
    return next((tag for pattern, tag in DESTINATION_TAGS if pattern.search(folded)), None)


def singular(word: str) -> str:
    """"beaches" → "beach", "galleries" → "gallery", "cafes" → "cafe" (tag search matches singulars best)."""
    folded = normalize(word)
    if len(folded) <= 3:
        return folded
    if re.search(r"(ch|sh|x|ss|z)es$", folded):
        return folded[:-2]
    if folded.endswith("ies"):
        return folded[:-3] + "y"
    return folded[:-1] if folded.endswith("s") and not folded.endswith("ss") else folded


def place_tags(candidates: list[QlooEntity], query: str = "", limit: int = 2) -> list[QlooEntity]:
    """The most specific venue tags among Qloo's candidates (no hotel/restaurant sub-genres, decor, menus),
    keeping only tags that name every word of the query ("record store" → record_store, not plain "store")."""
    words = [w for w in re.findall(r"\w+", normalize(query)) if w not in STOPWORDS and w != "for"]
    def rank(tag: QlooEntity) -> int | None:
        tid = (tag.get("id") or "").removeprefix("urn:tag:")
        if ":hotel:" in tid or ":restaurant:" in tid or not all(w in tid.rsplit(":", 1)[-1] for w in words):
            return None
        return next((i for i, kind in enumerate(PLACE_TAG_KINDS) if tid.startswith(kind)), None)
    ranked = sorted(((r, i, t) for i, t in enumerate(candidates) if (r := rank(t)) is not None), key=lambda x: x[:2])
    return [t for _, _, t in ranked[:limit]]


def book_author(book: QlooEntity) -> str | None:
    """Author name from a book's disambiguation ("2018, Sally Rooney")."""
    parts = (book.get("disambiguation") or "").split(",", 1)
    author = " ".join(parts[1].split()) if len(parts) == 2 else ""
    return author or None


async def resolve_book(q: Qloo, name: str) -> tuple[MatchedTaste | None, str]:
    """Qloo rejects books as destination signals (HTTP 400; ignored when mixed with others), but
    accepts people. So a book title or an author name resolves to the author as a person entity."""
    found = await q.get("/search", {"query": name, "types": "urn:entity:book,urn:entity:person", "take": 5})
    hit = best_match(name, found.get("results") or [])
    if not hit:
        return None, f"search: '{name}' → no match"
    if "urn:entity:person" in (hit.get("types") or []):
        return {"id": hit["entity_id"], "name": hit["name"], "category": "books"}, f"search: '{name}' → author {hit['name']} (person)"
    author = book_author(hit)
    person = await q.search(author, "urn:entity:person") if author else None
    if not person:
        return None, f"search: '{name}' → {hit['name']} (book), but Qloo can't rank cities by books alone"
    return ({"id": person["entity_id"], "name": person["name"], "category": "books"},
            f"search: '{name}' → {hit['name']} (book) → author {person['name']} (person)")


async def resolve_taste(q: Qloo, category: str, name: str) -> tuple[MatchedTaste | None, str]:
    """One free-text favorite → a matched taste (or None) and a trace line."""
    if category == "books":
        return await resolve_book(q, name)
    entity = await q.search(name, CATEGORY_TYPES[category])
    if not entity:
        return None, f"search: '{name}' → no match"
    label = display_name(entity["name"])
    return {"id": entity["entity_id"], "name": label, "category": category}, f"search: '{name}' → {label} ({category})"


async def resolve_lifestyle(q: Qloo, word: str) -> tuple[LifestyleTag | None, str]:
    """One lifestyle word → a destination genre filter and venue tag filters."""
    query = PLACE_TAG_QUERY.get(normalize(word), singular(word))
    venue = place_tags(await q.tags(query), query)
    city_tag = destination_tag(word)
    if not venue and not city_tag:
        return None, f"tags: '{word}' → no match"
    tag: LifestyleTag = {"id": (venue[0]["id"] if venue else city_tag) or "", "word": word,
                         "name": venue[0]["name"] if venue else word.title(), "place_tags": [t["id"] for t in venue]}
    if city_tag:
        tag["destination_tag"] = city_tag
    parts = ([f"cities {city_tag.rsplit(':', 1)[-1]}"] if city_tag else []) + ([f"venues {', '.join(t['id'].removeprefix('urn:tag:') for t in venue)}"] if venue else [])
    return tag, f"tags: '{word}' → filter " + " · ".join(parts)


async def resolve_profile(q: Qloo, profile: dict[str, str], trace: list[str]) -> tuple[list[MatchedTaste], list[LifestyleTag]]:
    """Step 1: resolve free-text likes to Qloo entity IDs and lifestyle words to tag filters (one parallel round)."""
    jobs = [(category, name) for category in CATEGORY_TYPES for name in split(profile.get(category, ""))][:MAX_ENTITIES]
    words = split(profile.get("lifestyle", ""))[:MAX_TAGS]
    results = await asyncio.gather(*[resolve_taste(q, category, name) for category, name in jobs],
                                   *[resolve_lifestyle(q, word) for word in words])
    entities: list[MatchedTaste] = []
    tags: list[LifestyleTag] = []
    for found, line in results[:len(jobs)]:
        trace.append(line)
        if found and found["id"] not in {e["id"] for e in entities}:
            entities.append(found)
    for found, line in results[len(jobs):]:
        trace.append(line)
        if found:
            tags.append(found)
    return entities, tags


def unmatched(trace: list[str]) -> list[str]:
    """Favorites Qloo couldn't match, for the UI ("not found: ...")."""
    return [m.group(1) for line in trace if (m := re.match(r"(?:search|tags): '(.+)' → (?:no match|.* \(book\), but Qloo can't rank cities by books alone)$", line))]

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
    if not entities:
        if q.failed:  # upstream trouble, not a genuine no-match
            return {"error": "Qloo is not responding right now, so we couldn't read your tastes. Please try again shortly.",
                    "trace": trace, "health": q.health()}
        missed = unmatched(trace)
        hint = (" Lifestyle words narrow the list, but Qloo ranks cities by the specific things you love, "
                "so add at least one artist, film, show, book, restaurant or brand.") if tags else ""
        return {"error": "Couldn't match any of your favorites in Qloo." + (f" Not found: {', '.join(missed)}." if missed else "") + hint,
                "trace": trace, "unmatched": missed, "health": q.health()}
    signals = _signals(entities)
    country_filter: Parameters = {}
    if profile.get("country"):
        country_filter["filter__geocode__country_code"] = profile["country"].upper()

    t1 = time.perf_counter()
    # Round 2: rank destinations on the combined profile (+ score the user's candidate city).
    cand_id = candidate["entity_id"] if candidate else None
    ranked = rank_destinations(q, n_cities, signals, country_filter, tags, trace)
    if cand_id:
        destinations, cand_hits = await asyncio.gather(
            ranked, q.insights(filter__type="urn:entity:destination", filter__results__entities=cand_id, take=1, **signals))
    else:
        destinations, cand_hits = await ranked, []
    # With a country filter the user asked for one country, so only spread across its regions.
    # Spare cities, in case Qloo's coordinates for a pick turn out to be somewhere else (see mislocated).
    cities = unique_cities(destinations, n_cities + SPARE_CITIES, max_per_country=None if country_filter else 2)
    if not cities:
        if q.failed:
            return {"error": "Qloo is not responding right now. Please try again shortly.", "trace": trace, "health": q.health()}
        return {"error": "Qloo found no cities for this mix of tastes" + (" in that country" if country_filter else "")
                         + ". Try other favorites" + (" or remove the country limit." if country_filter else "."),
                "matched": entities, "lifestyle_tags": tags, "unmatched": unmatched(trace), "trace": trace, "health": q.health()}
    cand_entity = cand_hits[0] if cand_hits else None
    if cand_entity:
        trace.append(f"insights(destination, filter.results.entities={cand_entity['name']}) → {pct(affinity(cand_entity))}/100")
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
    place_jobs = [find_places(q, c, signals, tags) for c in shown]
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
    results = located(built, n_cities, trace)
    t3 = time.perf_counter()
    narrative = await llm_summary(profile, results)
    timings = {"resolve_ms": round((t1 - t0) * 1000), "rank_ms": round((t2 - t1) * 1000),
               "neighborhoods_ms": round((t3 - t2) * 1000), "total_ms": round((time.perf_counter() - t0) * 1000),
               "qloo_calls": q.calls, "cache_hits": q.cache_hits, "qloo_failed": q.failed}
    trace.append(f"done in {timings['total_ms']} ms · {q.calls} Qloo calls · {q.cache_hits} cache hits")
    out: ScoutResult = {"matched": entities, "lifestyle_tags": tags, "unmatched": unmatched(trace), "cities": results,
                        "narrative": narrative, "trace": trace, "timings": timings, "health": q.health()}
    if q.failed:
        out["warning"] = ("Some Qloo requests failed or timed out, so these results may be incomplete. "
                          "Try again in a moment.")
    if candidate_name:
        scored = [e for e in entities if e["id"] in per_taste]
        out["compare"] = compare(candidate_name, cand_result, results[0] if results else None, scored)
    return out


async def _none() -> None:
    return None


def _signals(entities: list[MatchedTaste]) -> Parameters:
    """Taste signals. Lifestyle tags are applied as filters, not signals (see LifestyleTag)."""
    return {"signal__interests__entities": ",".join(entity["id"] for entity in entities)} if entities else {}


def _tag_filter(tags: list[LifestyleTag], key: str) -> Parameters:
    ids = list(dict.fromkeys(i for tag in tags for i in ([tag[key]] if isinstance(tag.get(key), str) else tag.get(key) or [])))
    return {"filter__tags": ",".join(ids)} if ids else {}


async def rank_destinations(q: Qloo, n_cities: int, signals: Parameters, country_filter: Parameters,
                            tags: list[LifestyleTag], trace: list[str], take: int | None = None) -> list[QlooEntity]:
    """Cities ranked by taste affinity, narrowed to the lifestyle's destination genres (any of them).
    If the genres leave too few cities, the rest of the list comes from the unfiltered ranking."""
    take = take or n_cities * 3
    genre = _tag_filter(tags, "destination_tag")
    destinations = await q.insights(filter__type="urn:entity:destination", take=take, **signals, **country_filter, **genre)
    if genre:
        names = ", ".join(dict.fromkeys(t["destination_tag"].rsplit(":", 1)[-1].replace("_", " ") for t in tags if t.get("destination_tag")))
        trace.append(f"insights(destination, taste signals, filter.tags={names}) → {len(destinations)} candidates")
        if len(_distinct(destinations)) < n_cities:
            more = await q.insights(filter__type="urn:entity:destination", take=take, **signals, **country_filter)
            destinations += more
            trace.append(f"insights(destination, taste signals) → {len(more)} more (lifestyle filter was too narrow)")
    else:
        trace.append(f"insights(destination, taste signals) → {len(destinations)} candidates")
    return destinations


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
    if not ea or not eb:
        who = labels[0] if not ea else labels[1]
        return {"error": f"Couldn't match any of {who}'s favorites in Qloo. Name at least one artist, film, show, book, "
                         "restaurant or brand.", "trace": trace, "health": q.health()}
    country = (profile_a.get("country") or profile_b.get("country") or "").upper()
    country_filter: Parameters = {"filter__geocode__country_code": country} if country else {}
    combined = _signals(ea + [e for e in eb if e["id"] not in {x["id"] for x in ea}])
    both_tags = ta + tb
    destinations = await rank_destinations(q, n_cities * 3, combined, country_filter, both_tags, trace, take=n_cities * 4)
    pool = unique_cities(destinations, n_cities * 3)
    if not pool:
        return {"error": "Qloo found no destinations for this pair of profiles.", "trace": trace, "health": q.health()}
    ids = ",".join(c["entity_id"] for c in pool)
    hits_a, hits_b = await asyncio.gather(
        q.insights(filter__type="urn:entity:destination", filter__results__entities=ids, take=len(pool), **_signals(ea)),
        q.insights(filter__type="urn:entity:destination", filter__results__entities=ids, take=len(pool), **_signals(eb)))
    score_a = {m["entity_id"]: affinity(m) for m in hits_a}
    score_b = {m["entity_id"]: affinity(m) for m in hits_b}
    trace.append(f"insights(destination, filter.results.entities=pool) scored separately for {labels[0]} and {labels[1]}")
    ranked = sorted(pool, key=lambda c: (-min(score_a.get(c["entity_id"], 0), score_b.get(c["entity_id"], 0)), -affinity(c)))
    shown = ranked[:n_cities + SPARE_CITIES]
    found = await asyncio.gather(*[find_places(q, c, combined, both_tags) for c in shown])
    per_person = {"person-a": score_a, "person-b": score_b} if hits_a and hits_b else {}
    people: list[MatchedTaste] = [{"id": "person-a", "name": labels[0], "category": "person"},
                                  {"id": "person-b", "name": labels[1], "category": "person"}]
    cities = located([build_city(c, f, per_person, people) for c, f in zip(shown, found)], n_cities, trace)
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
    (entities, tags), city_hit, focus_hit = await asyncio.gather(
        resolve_profile(q, profile, trace), q.search(city_name[:60], "urn:entity:destination"),
        resolve_lifestyle(q, focus[:40]) if focus else _none())
    if not city_hit:
        return {"error": f"Couldn't find '{city_name}' as a destination in Qloo.", "health": q.health()}
    focus_tag = focus_hit[0] if focus_hit else None
    if focus_hit:
        trace.append(focus_hit[1])
    if not entities:
        return {"error": "Couldn't match any of your favorites in Qloo.", "health": q.health()}
    signals = _signals(entities)
    scored = await q.insights(filter__type="urn:entity:destination", filter__results__entities=city_hit["entity_id"],
                              take=1, **signals)
    city = scored[0] if scored else city_hit
    # A focus ("coffee", "jazz bars") narrows venues on its own; otherwise the profile's lifestyle does.
    places, by_radius, radius_km = (await find_places(q, city, signals, [focus_tag] if focus_tag else tags))[:3]
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
        verdict = f"{cand['city']} matches your taste as well as {top['city']} ({pct(cand['affinity'])} vs {pct(top['affinity'])})."
    else:
        losers = sorted((r for r in rows if r["top_pick"] > r["candidate"]), key=lambda r: r["candidate"] - r["top_pick"])[:2]
        why = (" The biggest gaps: " + ", ".join(f"{r['taste']} ({pct(r['candidate'])} vs {pct(r['top_pick'])})" for r in losers) + ".") if losers else ""
        verdict = (f"{cand['city']} scores {pct(cand['affinity'])}/100, {pct(top['affinity']) - pct(cand['affinity'])} points behind "
                   f"{top['city']} ({pct(top['affinity'])}).{why}")
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


NEARBY_KM = 80
SPARE_CITIES = 2  # extra picks scored and searched in the same calls; mislocated ones are swapped out


def unique_cities(destinations: list[QlooEntity], limit: int, diverse: bool = True,
                  max_per_country: int | None = 2) -> list[QlooEntity]:
    """Distinct cities in ranked order, spread across regions so a shortlist offers real choices.

    Near-duplicates (same name, or within 30 km in the same country) are always dropped. With
    `diverse`, the first pass takes at most one city per state/region, `max_per_country` per
    country, and nothing within NEARBY_KM of a pick (San Juan and Fajardo share venues); leftover slots are then filled in rank order, so diversity never shrinks the list.
    """
    candidates = _distinct(destinations)
    if not diverse:
        return candidates[:limit]
    picked: list[QlooEntity] = []
    regions: set[tuple[str | None, str | None]] = set()
    per_country: Counter[str | None] = Counter()
    for city in candidates:
        geocode = (city.get("properties") or {}).get("geocode") or {}
        country = COUNTRY_EQUIVALENTS.get(geocode.get("country_code"), geocode.get("country_code"))
        region = (country, geocode.get("admin1_region"))
        if ((region[1] and region in regions) or (max_per_country and country and per_country[country] >= max_per_country)
                or any(near(city, other, NEARBY_KM) for other in picked)):
            continue
        picked.append(city)
        regions.add(region)
        per_country[country] += 1
        if len(picked) >= limit:
            return picked
    picked_ids = {id(c) for c in picked}
    leftovers = [c for c in candidates if id(c) not in picked_ids]
    ranked = {id(c): i for i, c in enumerate(candidates)}
    return sorted(picked + leftovers[:limit - len(picked)], key=lambda c: ranked[id(c)])


def _distinct(destinations: list[QlooEntity]) -> list[QlooEntity]:
    """Keep the first destination per city/country pair, merging near-duplicates, in ranked order."""
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
    return cities


def location_label(name: str, geocode: dict[str, Any]) -> str:
    """Where Qloo's data places a destination. If its geocode names another town (a district of a
    metro, or occasionally a misplaced entity such as "Billund" at Post Falls, Idaho), say so, because
    the venues on the card come from those coordinates."""
    region = " ".join(part for part in (geocode.get("admin1_region"), geocode.get("country_code")) if part)
    town = geocode.get("name") or ""
    a, b = (re.sub(r"[^\w ]", "", normalize(x)) for x in (name, town))  # "Hale'iwa" == "Haleiwa"
    label = f"{name}, {region}".rstrip(", ")
    if b and not (a in b or b in a or set(a.split()) & set(b.split())):
        label += f" · near {town}"
    return label


def build_city(city: QlooEntity, found: Found,
               per_taste: dict[str, dict[str, float]], entities: list[MatchedTaste] | None = None) -> CityResult:
    """Build one city card from destination affinity, per-taste attribution and local places."""
    geocode = (city.get("properties") or {}).get("geocode") or {}
    location = location_label(city["name"], geocode)
    places, by_radius, radius_km, venue_filter = found
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
        "why": "", "radius_km": radius_km, "venue_filter": venue_filter,
        "coverage": {"places_returned": len(places), "places_relevant": len(relevant),
                     "neighborhoods": len(neighborhoods), "tastes_scored": sum(1 for _, s in drivers if s > 0),
                     "tastes_total": len(entities) if entities else len(per_taste)},
    }
    result["why"] = explain(result, [])
    result["mislocated"] = mislocated(city, location, relevant)
    return result


def mislocated(city: QlooEntity, label: str, relevant: list[QlooEntity]) -> bool:
    """Qloo occasionally pins a destination far from the real town ("Doolin" at Pottstown, PA;
    "Cambridge" in London). Then its geocode names another town *and* none of the venues found
    around its coordinates has the town in its address. Real districts ("Shinjuku" near Tokyo,
    "Venice" near Marina del Rey) always have some venues that do."""
    addresses = [a for a in (address_text(p) for p in relevant) if a]
    return " · near " in label and len(addresses) >= 5 and not any(mentions(a, city["name"]) for a in addresses)


def located(built: list[CityResult], n_cities: int, trace: list[str]) -> list[CityResult]:
    """Drop mislocated cities (the spare fills in) and trim to n_cities."""
    for city in built:
        if city.get("mislocated"):
            trace.append(f"skipped {city['city']}: Qloo's coordinates for it point to {city['location'].split(' · near ')[-1]} "
                         f"(no venue there lists {city['city']} in its address)")
    return [c for c in built if not c.get("mislocated")][:n_cities]


URBAN_RADIUS_KM = 15
WIDE_RADIUS_KM = 60
MIN_LOCAL_SPOTS = 3


def point(city: QlooEntity) -> str | None:
    """WKT point for a destination's coordinates (Qloo filter.location)."""
    loc = city.get("location") or {}
    if isinstance(loc.get("lat"), (int, float)) and isinstance(loc.get("lon"), (int, float)):
        return f"POINT({loc['lon']} {loc['lat']})"
    return None


async def find_places(q: Qloo, city: QlooEntity, signals: Parameters, tags: list[LifestyleTag] | None = None) -> Found:
    """Find taste-matched venues around a city: (places, by_radius, radius_km, lifestyle filter names).

    Uses the destination's own coordinates (filter.location + radius), because a bare name is
    ambiguous ("Flores" resolves to a Buenos Aires barrio). Venues are first narrowed to the
    lifestyle's place tags (cafés, hiking, coworking...); if that leaves too few, the filter is
    dropped. Widens to 60 km for small towns with thin data. Falls back to a name query only if
    coordinates are missing.
    """
    wkt = point(city)
    lifestyle = _tag_filter(tags or [], "place_tags")
    names = list(dict.fromkeys(t["name"] for t in tags or [] if t.get("place_tags")))
    if not wkt:
        for narrow in ([lifestyle] if lifestyle else []) + [{}]:
            places = await q.insights(filter__type="urn:entity:place", filter__location__query=city["name"],
                                      take=30, **signals, **narrow)
            if not narrow or len(local_relevant(places, city)) >= MIN_LOCAL_SPOTS:
                return places, False, None, names if narrow else []
    # Local beats lifestyle-matched: a 60 km radius around a small town can reach the next city's venues.
    attempts = ([(URBAN_RADIUS_KM, lifestyle)] if lifestyle else []) + [(URBAN_RADIUS_KM, {}), (WIDE_RADIUS_KM, {})]
    places: list[QlooEntity] = []
    for radius, narrow in attempts:
        places = await q.insights(filter__type="urn:entity:place", filter__location=wkt,
                                  filter__location__radius=radius * 1000, take=30, **signals, **narrow)
        if len(local_relevant(places, city, True)) >= MIN_LOCAL_SPOTS:
            return places, True, radius, names if narrow else []
    return places, True, WIDE_RADIUS_KM, []


# Qloo destination genre names → phrases that read naturally after "Known for:".
KNOWN_FOR = {"for the history": "history", "with kids": "families with kids", "great nightlife": "nightlife",
             "great shopping": "shopping", "great architecture": "architecture", "urban destinations": "city life",
             "scenic & picturesque": "scenery", "foodie destination": "food", "romantic getaways": "romantic getaways",
             "snow and ski": "skiing", "outdoor activities": "the outdoors", "entertainment & performing arts": "performing arts"}


def known_for(city_tags: list[str]) -> list[str]:
    return [KNOWN_FOR.get(t.casefold(), t.casefold()) for t in city_tags]


def explain(result: CityResult, tags: list[LifestyleTag]) -> str:
    """Rule-based explanation grounded only in Qloo data (works with no LLM)."""
    parts = [f"{result['city']} scores {pct(result['affinity'])}/100 taste affinity with your profile."]
    if result["drivers"]:
        parts.append("Strongest pull: " + ", ".join(f"{driver['taste']} ({pct(driver['affinity'])})" for driver in result["drivers"][:3]) + ".")
    if result["city_tags"]:
        parts.append("Known for: " + ", ".join(known_for(result["city_tags"][:3])) + ".")
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
