"""Offline tests. Qloo is mocked with synthetic data; no real Qloo responses are stored in the repo."""
import os, sys
import selectors
import socket
from html.parser import HTMLParser
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("QLOO_API_KEY", "test-key-not-real")
os.environ["RATE_LIMIT_PER_MIN"] = "3"
import pytest
from fastapi.testclient import TestClient
import app as appmod, agent

FAKE_CITY = {"name": "<img src=x onerror=alert(1)>", "entity_id": "c1", "properties": {"geocode": {"country_code": "ZZ"}},
             "query": {"affinity": 0.5}, "tags": [{"name": "Fake tag"}]}

async def fake_get(self, path, params):
    if path == "/search": return {"results": [{"entity_id": "e1", "name": params["query"]}]}  # an exact-name match
    if path == "/v2/tags": return {"results": {"tags": [{"id": "urn:tag:fake:destination:x", "name": "X"}]}}
    if params.get("filter.type") == "urn:entity:destination": return {"results": {"entities": [FAKE_CITY]}}
    return {"results": {"entities": [{"name": "Spot", "properties": {"neighborhood": "Hood"}, "tags": [{"name": "Cozy"}, {"name": "Takeout"}]}]}}

@pytest.fixture(autouse=True)
def mock_qloo(monkeypatch):
    """Keep tests offline and poll portals when sandbox socket wakeups are denied."""
    reader, writer = socket.socketpair()
    with reader, writer:
        try:
            writer.send(b"\0")
        except PermissionError:
            original_select = selectors.DefaultSelector.select
            def bounded_select(self, timeout=None):
                return original_select(self, 0.01 if timeout is None else min(timeout, 0.01))
            monkeypatch.setattr(selectors.DefaultSelector, "select", bounded_select)
    monkeypatch.setattr(agent.Qloo, "get", fake_get)
    async def no_summary(*args, **kwargs):
        return None
    async def no_network(*args, **kwargs):
        raise AssertionError("Offline tests must not make network requests")
    monkeypatch.setattr(agent, "llm_summary", no_summary)
    monkeypatch.setattr(agent.httpx.AsyncClient, "send", no_network)
    appmod._hits.clear()

c = TestClient(appmod.app)

def test_scout_ok_and_headers():
    r = c.post("/api/scout", json={"music": "Anything", "lifestyle": "walkable"})
    assert r.status_code == 200
    body = r.json()
    assert body["cities"][0]["neighborhoods"][0]["name"] == "Hood"
    assert "Takeout" not in body["cities"][0]["vibe"]
    for h in ["content-security-policy", "x-content-type-options", "referrer-policy", "x-frame-options"]:
        assert h in r.headers
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]

def test_validation():
    assert c.post("/api/scout", json={"country": "USA"}).status_code == 422
    assert c.post("/api/scout", json={"country": "1!"}).status_code == 422
    assert c.post("/api/scout", json={"music": "a" * 401}).status_code == 422
    assert c.post("/api/scout", json={"evil": "x"}).status_code == 422
    assert c.post("/api/scout", content=b'{"music":"' + b"a" * 5000 + b'"}', headers={"content-type": "application/json"}).status_code == 413

def test_item_caps():
    assert len(agent.split(",".join(["x"] * 50))) == agent.MAX_ITEMS_PER_FIELD
    assert all(len(x) <= 80 for x in agent.split("y" * 300))
    assert agent.split("a\x00b") == ["ab"]

def test_rate_limit():
    codes = [c.post("/api/scout", json={"music": "A"}).status_code for _ in range(5)]
    assert codes[:3] == [200] * 3 and codes[-1] == 429

def test_no_docs_in_prod_and_generic_errors(monkeypatch):
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    async def boom(*a, **k): raise RuntimeError("secret test-key-not-real")
    monkeypatch.setattr(appmod, "scout", boom)
    r = c.post("/api/scout", json={"music": "A"})
    assert r.status_code == 502 and "test-key" not in r.text and "Traceback" not in r.text

def test_no_cors():
    r = c.options("/api/scout", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers

def test_frontend_has_no_innerhtml():
    js = open(os.path.join(os.path.dirname(__file__), "..", "static", "app.js")).read()
    assert all(sink not in js for sink in ("innerHTML", "insertAdjacentHTML", "outerHTML"))


def scout_places(monkeypatch, places, city="Bogotá", country="CO"):
    """Exercise the API with invented destinations and places, never live data."""
    destination = {
        "name": city, "entity_id": "synthetic-city",
        "properties": {"geocode": {"country_code": country}},
        "query": {"affinity": 0.812}, "tags": [],
    }

    async def synthetic_get(self, path, params):
        assert path in {"/search", "/v2/insights", "/v2/tags"}
        if path == "/v2/insights":
            entities = [destination] if params.get("filter.type") == "urn:entity:destination" else places
            return {"results": {"entities": entities}}
        return await fake_get(self, path, params)

    monkeypatch.setattr(agent.Qloo, "get", synthetic_get)
    response = c.post("/api/scout", json={"music": "Synthetic Artist"})
    assert response.status_code == 200
    body = response.json()
    assert {"matched", "lifestyle_tags", "cities", "narrative", "trace"} <= body.keys()
    return body["cities"][0]


@pytest.mark.parametrize(("raw", "expected"), [
    ("UPZs Localidad Chapinero", "Chapinero"),
    ("  UPZ   Chapinero  ", "Chapinero"),
    ("Localidad Usaquén", "Usaquén"),
    ("localidad UPZ Centro", "Centro"),
    ("Zona 10", "Zona 10"),
    ("Zone 4", "Zone 4"),
    ("UPZ", ""),
    ("", ""),
])
def test_clean_neighborhood_name(raw, expected):
    assert agent.clean_neighborhood_name(raw) == expected


def test_neighborhoods_deduplicate_after_cleaning(monkeypatch):
    result = scout_places(monkeypatch, [
        {"name": "Invented Cafe", "properties": {"neighborhood": "UPZs Localidad Chapinero"}},
        {"name": "Invented Gallery", "properties": {"neighborhood": "UPZ Chapinero"}},
        {"name": "Invented Cafe", "properties": {"neighborhood": "localidad chapinero"}},
        {"name": "Invented Bakery", "properties": {"neighborhood": "Zona 10"}},
    ])
    assert result["neighborhoods"] == [
        {"name": "Chapinero", "spots": ["Invented Cafe", "Invented Gallery"]},
        {"name": "Zona 10", "spots": ["Invented Bakery"]},
    ]


def test_wrong_city_neighborhoods_and_vibes_are_dropped(monkeypatch):
    result = scout_places(monkeypatch, [
        {"name": "Invented Oakland Spot", "properties": {
            "neighborhood": "Oakland", "address": "123 Fiction St, Oakland, CA, USA",
            "geocode": {"country_code": "US", "city": "Oakland"}},
         "tags": [{"name": "Foreign vibe"}]},
        {"name": "Invented Local Spot", "properties": {
            "neighborhood": "Zona 4", "address": "123 Fiction St, Guatemala City, Guatemala",
            "geocode": {"country_code": "GT"}}},
    ], city="Guatemala City", country="GT")
    assert result["neighborhoods"] == [{"name": "Zona 4", "spots": ["Invented Local Spot"]}]
    assert "Foreign vibe" not in result["vibe"]
    assert "Invented Oakland Spot" not in result["top_spots"]


@pytest.mark.parametrize(("properties", "keep"), [
    ({"address": "123 Fiction St, BOGOTA, Colombia", "geocode": {"country_code": "CO"}}, True),
    ({"address": "123 Fiction St", "geocode": {"city": "Bogota", "country_code": "CO"}}, True),
    ({"address": {"city": "Bogota", "country_code": "CO"}}, True),
    ({"geocode": {"country_code": "CO"}}, True),
    ({"geocode": {"admin2_region": "Invented County", "country_code": "CO"}}, True),
    ({}, True),
    ({"address": "123 Fiction St, Oakland, USA"}, False),
    ({"geocode": {"country_code": "US"}}, False),
    ({"geocode": {"country": "United States"}}, False),
    ({"address": "123 Fiction St, Bogotá, USA"}, False),
    ({"address": "123 Fiction St, Québec, Canadá", "geocode": {"country_code": "CA"}}, False),
    ({"geocode": {"city": "Medellín", "country_code": "CO"}}, False),
    ({"address": {"city": "Medellín", "country_code": "CO"}}, False),
    ({"address": "123 Fiction St, Bogotaville", "geocode": {"country_code": "US"}}, False),
])
def test_venue_location_evidence(monkeypatch, properties, keep):
    place = {"name": "Invented Spot", "properties": {**properties, "neighborhood": "Centro"}}
    result = scout_places(monkeypatch, [place])
    assert bool(result["neighborhoods"]) is keep


def test_vibe_filters_junk_and_existing_noise(monkeypatch):
    names = ["35 44", "Art 2", "24-hour", "AB", "", "  ", "Wi-Fi", "Takeout", "Card payment", "Cozy", "Art", "  Creative  "]
    places = [{"name": f"Invented Spot {index}", "properties": {"neighborhood": "Centro"},
               "tags": [{"name": name}]} for index, name in enumerate(names)]
    places.append({"name": "Invented Restaurant", "properties": {},
                   "tags": [{"name": "Restaurants", "type": "urn:tag:category:place"}]})
    result = scout_places(monkeypatch, places)
    assert result["vibe"] == ["Cozy", "Art", "Creative"]


def test_top_spots_fallback_is_ranked_capped_and_explained(monkeypatch):
    places = [
        {"name": "Invented Modest Cafe", "properties": {"address": "Bogota, Colombia"}, "query": {"affinity": 0.2}},
        {"name": "Invented Favorite Gallery", "properties": {"neighborhood": "UPZ "}, "query": {"affinity": 0.9}},
        {"name": "Invented Favorite Gallery", "properties": {}, "query": {"affinity": 0.8}},
        {"name": "Invented Bookshop", "properties": {}, "query": {"affinity": 0.7}},
        {"name": "Invented Bakery", "properties": {}, "query": {"affinity": 0.6}},
        {"name": "Invented Park", "properties": {}, "query": {"affinity": 0.5}},
        {"name": "Invented Foreign Club", "properties": {"geocode": {"country_code": "US"}}, "query": {"affinity": 1}},
    ]
    result = scout_places(monkeypatch, places)
    assert result["neighborhoods"] == []
    assert result["top_spots"] == ["Invented Favorite Gallery", "Invented Bookshop", "Invented Bakery", "Invented Park"]
    assert "Top taste-matched spots" in result["why"]
    assert "Invented Favorite Gallery" in result["why"]
    assert "Invented Bookshop" in result["why"]
    assert "Invented Foreign Club" not in result["why"]


def test_empty_places_are_handled(monkeypatch):
    result = scout_places(monkeypatch, [])
    assert result["neighborhoods"] == []
    assert result["top_spots"] == []
    assert "Top taste-matched spots" not in result["why"]


class StaticSecurityParser(HTMLParser):
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        assert tag != "style"
        assert not any(name == "style" or name.startswith("on") for name in attributes)
        if tag == "script":
            assert attributes.get("src", "").startswith("/static/")
        if tag == "link":
            assert attributes.get("href", "").startswith("/static/")


def test_frontend_csp_and_product_structure():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    parser = StaticSecurityParser()
    parser.feed(html)
    assert "<header" in html and "<footer" in html and "<fieldset" in html
    assert 'aria-live="polite"' in html
    assert 'id="loading"' in html
    js = (root / "app.js").read_text()
    assert "top_spots" in js and "Top spots" in js
    assert "disabled" in js and "aria-busy" in js
    css = (root / "style.css").read_text()
    assert "@media" in css and "prefers-reduced-motion" in css


def test_timings_reported():
    body = c.post("/api/scout", json={"music": "Anything"}).json()
    assert {"total_ms", "qloo_calls", "cache_hits"} <= set(body["timings"])


def test_process_cache_and_allowed_endpoints(monkeypatch):
    """Real Qloo.get with a mock transport: repeat calls hit the in-memory cache; only allowed paths used."""
    import asyncio, httpx
    monkeypatch.undo()  # restore real Qloo.get
    seen = []
    def handler(request):
        seen.append(request.url.path)
        assert request.headers["x-api-key"]
        return httpx.Response(200, json={"results": {"entities": []}})
    client = httpx.AsyncClient(base_url="https://example.invalid", headers={"X-Api-Key": "k"}, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(agent, "_shared_client", lambda: client)
    agent._CACHE.clear()
    async def run():
        q = agent.Qloo()
        await q.insights(filter__type="urn:entity:destination", take=1)
        await q.insights(filter__type="urn:entity:destination", take=1)
        return q
    q = asyncio.run(run())
    assert seen == ["/v2/insights"] and q.calls == 1 and q.cache_hits == 1
    agent._CACHE.clear()


def test_frontend_share_link_has_no_html_injection():
    js = open(os.path.join(os.path.dirname(__file__), "..", "static", "app.js")).read()
    assert "URLSearchParams" in js and "eval(" not in js and "document.write" not in js


def test_us_territory_places_kept():
    city = {"name": "San Juan", "properties": {"geocode": {"country_code": "PR"}}}
    place = {"name": "X", "properties": {"address": "300 Calle, San Juan, 00909 Puerto Rico",
                                         "geocode": {"country_code": "US", "admin1_region": "Puerto Rico"}}}
    assert agent.place_matches_city(place, city)


def test_covid_and_long_vibe_tags_dropped():
    for junk in ["No contact delivery", "Staff required to disinfect surfaces between visits", "Alcohol"]:
        assert agent.vibe_tag({"name": junk}) is None
    assert agent.vibe_tag({"name": "Lively"}) == "Lively"


def test_nearby_duplicate_cities_merged():
    a = {"name": "Cartagena De Indias", "location": {"lat": 10.39, "lon": -75.51}, "properties": {"geocode": {"country_code": "CO"}}}
    b = {"name": "Cartagena", "location": {"lat": 10.42, "lon": -75.53}, "properties": {"geocode": {"country_code": "CO"}}}
    assert [c["name"] for c in agent.unique_cities([a, b], 4)] == ["Cartagena De Indias"]


# ---------- Round 3: edge cases, observability, relevance, thin data, comparison ----------
import asyncio
import httpx


def _dest(name, cc, lat, lon, eid=None):
    return {"name": name, "entity_id": eid or name, "location": {"lat": lat, "lon": lon},
            "properties": {"geocode": {"country_code": cc}}, "query": {"affinity": 0.5}}


def test_nearby_cities_across_border_not_merged():
    # El Paso (US) and Ciudad Juárez (MX) are ~5 km apart but in different countries.
    a, b = _dest("El Paso", "US", 31.76, -106.49), _dest("Ciudad Juarez", "MX", 31.72, -106.46)
    assert [c["name"] for c in agent.unique_cities([a, b], 4)] == ["El Paso", "Ciudad Juarez"]


def test_same_name_tastes_keyed_by_id():
    """Two different entities called 'Up' (film vs band) must keep separate attribution."""
    city = _dest("Testville", "ZZ", 1, 1, "city1")
    entities = [{"id": "film-up", "name": "Up", "category": "films"}, {"id": "band-up", "name": "Up", "category": "music"}]
    per_taste = {"film-up": {"city1": 0.9}, "band-up": {"city1": 0.4}}
    result = agent.build_city(city, ([], True, 15, []), per_taste, entities)
    assert sorted((d["id"], d["affinity"]) for d in result["drivers"]) == [("band-up", 0.4), ("film-up", 0.9)]


def _run_with_transport(monkeypatch, handler, coro_fn):
    client = httpx.AsyncClient(base_url="https://example.invalid", headers={"X-Api-Key": "k"},
                               transport=httpx.MockTransport(handler))
    monkeypatch.setattr(agent, "_shared_client", lambda: client)
    monkeypatch.setattr(agent.asyncio, "sleep", _nosleep)
    agent._CACHE.clear()
    try:
        return asyncio.run(coro_fn())
    finally:
        agent._CACHE.clear()


async def _nosleep(*_a, **_k):
    return None


def test_upstream_errors_tracked_not_silent(monkeypatch):
    monkeypatch.undo()
    def handler(request):
        if request.url.path == "/search":
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.path == "/v2/tags":
            return httpx.Response(429)
        return httpx.Response(500, text="secret upstream body")
    async def go():
        q = agent.Qloo()
        await q.search("x", "urn:entity:artist")
        await q.tags("y")
        await q.insights(filter__type="urn:entity:destination")
        return q
    q = _run_with_transport(monkeypatch, handler, go)
    h = q.health()
    assert h["partial"] and h["failed_calls"] == 3
    assert h["endpoints"]["/search"]["timeouts"] == 1
    assert h["endpoints"]["/v2/tags"]["rate_limited"] == 1 and h["endpoints"]["/v2/tags"]["retries"] == 3
    assert h["endpoints"]["/v2/insights"]["http_errors"] == 1
    assert "secret upstream body" not in str(h) and "k" != str(h)
    assert not agent._CACHE  # failures are never cached


def test_outage_is_503_with_safe_message(monkeypatch):
    monkeypatch.undo()
    def handler(request):
        return httpx.Response(502, text="internal stack trace test-key-not-real")
    client = httpx.AsyncClient(base_url="https://example.invalid", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(agent, "_shared_client", lambda: client)
    agent._CACHE.clear(); appmod._hits.clear()
    r = c.post("/api/scout", json={"music": "Anything"})
    assert r.status_code == 503
    assert "not responding" in r.json()["error"] and "stack trace" not in r.text and "test-key" not in r.text


def test_partial_results_flagged():
    async def flaky_get(self, path, params):
        if params.get("filter.type") == "urn:entity:place":
            self.stats[path]["calls"] += 1; self.stats[path]["timeouts"] += 1
            return {}
        return await fake_get(self, path, params)
    import pytest as _p
    mp = _p.MonkeyPatch(); mp.setattr(agent.Qloo, "get", flaky_get)
    try:
        body = c.post("/api/scout", json={"music": "Anything"}).json()
    finally:
        mp.undo()
    assert body["health"]["partial"] is True and "incomplete" in body["warning"]


def test_irrelevant_place_types_filtered():
    def p(name, *cats):
        return {"name": name, "tags": [{"type": "urn:tag:category:place", "name": c} for c in cats]}
    assert not agent.is_relevant_place(p("Grand Hotel", "Hotel"))
    assert not agent.is_relevant_place(p("Lincoln High", "Educational institution"))
    assert not agent.is_relevant_place(p("Shell", "Gas station"))
    assert not agent.is_relevant_place(p("15 Avenida 5 calle"))
    assert agent.is_relevant_place(p("Hotel Bar", "Hotel", "Bar"))  # hotel that is also a bar
    assert agent.is_relevant_place(p("Gallery", "Art gallery"))


def test_spots_ranked_by_affinity():
    city = _dest("Town", "ZZ", 1, 1)
    places = [{"name": n, "query": {"affinity": a}, "properties": {}, "tags": []} for n, a in [("Low", 0.2), ("High", 0.9), ("Mid", 0.5)]]
    _, spots, _ = agent.summarize_places(places, city, True)
    assert spots == ["High", "Mid", "Low"]


def test_thin_city_widens_radius(monkeypatch):
    calls = []
    async def fake_insights(self, **params):
        calls.append(params.get("filter__location__radius"))
        n = 0 if params.get("filter__location__radius") == 15000 else 4
        return [{"name": f"Spot{i}", "properties": {}, "tags": [], "query": {"affinity": 0.5}} for i in range(n)]
    monkeypatch.setattr(agent.Qloo, "insights", fake_insights)
    async def go():
        return await agent.find_places(agent.Qloo.__new__(agent.Qloo), _dest("Flores", "GT", 16.9, -89.9), {})
    places, by_radius, radius, venue_filter = asyncio.run(go())
    assert venue_filter == [] and calls == [15000, 60000] and radius == 60 and by_radius and len(places) == 4


def test_location_uses_coordinates_not_ambiguous_name():
    assert agent.point(_dest("Flores", "GT", 16.93, -89.89)) == "POINT(-89.89 16.93)"


def test_compare_verdict_uses_real_scores():
    ents = [{"id": "a", "name": "A", "category": "music"}, {"id": "b", "name": "B", "category": "films"}]
    top = {"city": "Top", "affinity": 0.8, "drivers": [{"id": "a", "taste": "A", "affinity": 0.9, "category": "music"},
                                                       {"id": "b", "taste": "B", "affinity": 0.7, "category": "films"}], "why": ""}
    cand = {"city": "Cand", "affinity": 0.6, "drivers": [{"id": "a", "taste": "A", "affinity": 0.5, "category": "music"}], "why": ""}
    cmp = agent.compare("Cand", cand, top, ents)
    assert cmp["gap"] == 0.2 and cmp["rows"][1] == {"taste": "B", "candidate": 0.0, "top_pick": 0.7}
    assert "20 points behind Top" in cmp["verdict"] and "A (50 vs 90)" in cmp["verdict"]
    assert "error" in agent.compare("Nowhere", None, top, ents)


def test_compare_input_validated():
    assert c.post("/api/scout", json={"music": "A", "compare": "<script>"}).status_code == 422
    assert c.post("/api/scout", json={"music": "A", "compare": "x" * 61}).status_code == 422


def test_default_rate_limit_is_12(monkeypatch):
    import importlib
    monkeypatch.delenv("RATE_LIMIT_PER_MIN", raising=False)
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "app.py")).read()
    assert 'os.getenv("RATE_LIMIT_PER_MIN", "12")' in src


def test_city_named_venue_excluded():
    city = _dest("San Juan", "PR", 18.4, -66.1)
    places = [{"name": "San Juan, Puerto Rico", "properties": {}, "tags": []}, {"name": "La Placita", "properties": {}, "tags": []}]
    assert [p["name"] for p in agent.local_relevant(places, city, True)] == ["La Placita"]


def test_eval_claim_checker_detects_unbacked_claims():
    import sys as _s; _s.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
    import run_eval
    city = {"city": "X", "affinity": 0.8, "drivers": [{"taste": "A", "affinity": 0.9}], "city_tags": ["Beaches"],
            "neighborhoods": [{"name": "Old Town", "spots": ["Cafe, Bar", "Museum"]}], "top_spots": [], "vibe": ["Lively"],
            "why": "X scores 80/100 taste affinity with your profile. Strongest pull: A (90). Known for: beaches. "
                   "Start in Old Town: spots like Cafe, Bar, Museum match your taste. Local vibe: lively."}
    assert run_eval.claims_backed(city)[2] == []
    city["why"] = city["why"].replace("A (90)", "A (99)").replace("beaches", "skiing")
    assert set(run_eval.claims_backed(city)[2]) == {"driver A", "city tag"}


def _real_qloo(monkeypatch, handler):
    import httpx
    monkeypatch.undo()  # restore real Qloo.get
    client = httpx.AsyncClient(base_url="https://example.invalid", headers={"X-Api-Key": "k"}, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(agent, "_shared_client", lambda: client)
    agent._CACHE.clear()


def test_transient_timeout_is_retried_once(monkeypatch):
    import asyncio, httpx
    attempts = []
    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("cold connection")
        return httpx.Response(200, json={"results": [{"entity_id": "e1", "name": "X"}]})
    _real_qloo(monkeypatch, handler)
    async def run():
        q = agent.Qloo()
        return q, await q.search("x", "urn:entity:artist")
    q, found = asyncio.run(run())
    assert found["entity_id"] == "e1" and len(attempts) == 2 and q.failed == 0
    agent._CACHE.clear()


def test_persistent_timeout_counts_as_failure(monkeypatch):
    import asyncio, httpx
    def handler(request):
        raise httpx.ReadTimeout("slow")
    _real_qloo(monkeypatch, handler)
    async def run():
        q = agent.Qloo()
        return q, await q.search("x", "urn:entity:artist")
    q, found = asyncio.run(run())
    assert found is None and q.failed == 1 and q.health()["partial"]
    agent._CACHE.clear()


def test_failed_attribution_is_not_reported_as_zero(monkeypatch):
    calls = {"attr": 0}
    async def get(self, path, params):
        if path == "/search":
            if "destination" in params["types"]:
                return {"results": [{"entity_id": "cand", "name": "Cand"}]}
            return {"results": [{"entity_id": "e-" + params["query"], "name": params["query"]}]}
        if params.get("filter.type") == "urn:entity:destination":
            top = {"name": "Top", "entity_id": "top", "query": {"affinity": 0.9}}
            cand = {"name": "Cand", "entity_id": "cand", "query": {"affinity": 0.7}}
            if "filter.results.entities" not in params:
                return {"results": {"entities": [top]}}
            if params["filter.results.entities"] == "cand":
                return {"results": {"entities": [cand]}}
            calls["attr"] += 1
            if params["signal.interests.entities"] == "e-B":
                return {}  # upstream failure for this taste
            return {"results": {"entities": [dict(top, query={"affinity": 0.8}), dict(cand, query={"affinity": 0.5})]}}
        return {"results": {"entities": []}}
    monkeypatch.setattr(agent.Qloo, "get", get)
    import asyncio
    out = asyncio.run(agent.scout({"music": "A, B", "compare": "Cand"}))
    rows = out["compare"]["rows"]
    assert [r["taste"] for r in rows] == ["A"]  # B is "not scored", not "0 vs 0"
    assert out["cities"][0]["coverage"] == {**out["cities"][0]["coverage"], "tastes_scored": 1, "tastes_total": 2}


def test_candidate_already_shown_is_not_fetched_twice(monkeypatch):
    place_calls = []
    async def get(self, path, params):
        if path == "/search":
            if "destination" in params["types"]:
                return {"results": [{"entity_id": "top", "name": "Top"}]}
            return {"results": [{"entity_id": "e1", "name": "A"}]}
        top = {"name": "Top", "entity_id": "top", "query": {"affinity": 0.9}, "location": {"lat": 1.0, "lon": 2.0}}
        if params.get("filter.type") == "urn:entity:destination":
            return {"results": {"entities": [top]}}
        place_calls.append(params)
        return {"results": {"entities": []}}
    monkeypatch.setattr(agent.Qloo, "get", get)
    import asyncio
    out = asyncio.run(agent.scout({"music": "A", "compare": "Top"}))
    assert out["compare"]["verdict"] == "Top is already your top pick."
    assert len(place_calls) == 2  # one city x (15 km, 60 km); not doubled by the candidate


def test_forwarded_for_uses_proxy_appended_entry(monkeypatch):
    class Req:
        headers = {"x-forwarded-for": "6.6.6.6, 10.0.0.1"}
        client = None
    monkeypatch.setattr(appmod, "TRUST_PROXY", True)
    assert appmod.client_ip(Req()) == "10.0.0.1"  # leftmost entry is spoofable


def test_rate_limit_table_prunes_stale_ips(monkeypatch):
    import time
    from collections import deque
    old = time.monotonic() - 120
    for i in range(10001):
        appmod._hits[f"ip{i}"] = deque([old])
    appmod.rate_limited("fresh")
    assert len(appmod._hits) == 1 and "fresh" in appmod._hits


import chat_agent


def _script_llm(monkeypatch, replies):
    """Fake OpenAI-compatible LLM: returns scripted messages and records what it was sent."""
    sent = []
    async def complete(client, messages):
        sent.append([dict(m) for m in messages])
        return replies.pop(0)
    monkeypatch.setattr(chat_agent, "_complete", complete)
    for k, v in {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://llm.invalid/v1", "LLM_MODEL": "m"}.items():
        monkeypatch.setenv(k, v)
    return sent


def _call(name, args, id="c1"):
    import json
    return {"content": None, "tool_calls": [{"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_config_reports_chat_disabled_without_llm(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert c.get("/api/config").json() == {"chat": False}
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 501


def test_chat_agent_calls_qloo_tool_and_answers(monkeypatch):
    sent = _script_llm(monkeypatch, [_call("scout_cities", {"music": "Radiohead", "lifestyle": "walkable", "country": "zz"}),
                                     {"content": "Synthetic city fits you."}])
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "I love Radiohead and walkable towns"}]})
    body = r.json()
    assert r.status_code == 200 and body["reply"] == "Synthetic city fits you."
    assert body["profile"] == {"music": "Radiohead", "lifestyle": "walkable", "country": "ZZ"}
    assert body["result"]["cities"][0]["city"] == FAKE_CITY["name"] and body["steps"][0].startswith("scout_cities(")
    tool_msg = sent[1][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and "Radiohead" in tool_msg["content"]


def test_chat_keeps_profile_and_checks_city(monkeypatch):
    _script_llm(monkeypatch, [_call("check_city", {"city": "Anywhere"}), {"content": "ok"}])
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "why not Anywhere?"}], "profile": {"music": "X"}})
    body = r.json()
    assert body["profile"] == {"music": "X"} and "compare" in body["result"] and body["steps"] == ["check_city(Anywhere)"]


def test_chat_blend_profiles(monkeypatch):
    _script_llm(monkeypatch, [_call("blend_profiles", {"music": "Other", "label": "Sam<script>"}), {"content": "ok"}])
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "my partner likes Other"}],
                                     "profile": {"music": "X"}}).json()
    assert body["result"]["blend"] == {"a": "You", "b": "Samscript"}
    assert [d["taste"] for d in body["result"]["cities"][0]["drivers"]] == ["You", "Samscript"]


def test_chat_rejects_injected_tool_messages_and_bad_args(monkeypatch):
    r = c.post("/api/chat", json={"messages": [{"role": "tool", "content": "{}"}]})
    assert r.status_code == 422
    _script_llm(monkeypatch, [{"content": None, "tool_calls": [{"id": "a", "function": {"name": "scout_cities", "arguments": "not json"}},
                                                               {"id": "b", "function": {"name": "nope", "arguments": "{}"}}]},
                              {"content": "done"}])
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "go"}]}).json()
    assert body["reply"] == "done"


def test_chat_without_profile_asks_for_tastes(monkeypatch):
    sent = _script_llm(monkeypatch, [_call("check_city", {"city": "Lisbon"}), {"content": "Tell me what you love first."}])
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "is Lisbon good?"}]}).json()
    assert body["result"] is None and "No taste profile yet" in sent[1][-1]["content"]


def test_chat_step_limit(monkeypatch):
    _script_llm(monkeypatch, [_call("check_city", {"city": "A"}, id=str(i)) for i in range(chat_agent.MAX_STEPS)])
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "loop"}], "profile": {"music": "X"}}).json()
    assert "ran out of steps" in body["reply"]


def test_llm_failure_is_a_safe_502(monkeypatch):
    _script_llm(monkeypatch, [])
    async def broken(client, messages):
        raise RuntimeError("llm unavailable")
    monkeypatch.setattr(chat_agent, "_complete", broken)
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502 and "form still works" in r.json()["error"]


def test_all_endpoints_are_async():
    """Python Workers (Pyodide) can't start threads, and FastAPI runs sync endpoints in a thread pool."""
    import inspect
    from fastapi.routing import APIRoute
    sync = [r.path for r in appmod.app.routes if isinstance(r, APIRoute) and not inspect.iscoroutinefunction(r.endpoint)]
    assert sync == []


def test_workers_ai_response_shapes_are_normalized():
    openai_shape = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "x", "function": {"name": "n", "arguments": "{}"}}]}}]}
    assert chat_agent._normalize(openai_shape)["tool_calls"][0]["id"] == "x"
    legacy = chat_agent._normalize({"response": None, "tool_calls": [{"name": "check_city", "arguments": {"city": "Lima"}}]})
    call = legacy["tool_calls"][0]
    assert call["function"] == {"name": "check_city", "arguments": '{"city": "Lima"}'} and len(call["id"]) == 9


def test_binding_enables_chat_without_http_llm(monkeypatch):
    for k in ("LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(chat_agent, "AI_BINDING", object())
    assert c.get("/api/config").json() == {"chat": True}
    monkeypatch.setattr(chat_agent, "AI_BINDING", None)
    assert c.get("/api/config").json() == {"chat": False}


def test_text_tool_call_is_retried_then_refused(monkeypatch):
    sent = _script_llm(monkeypatch, [{"content": "[scout_cities(music=X)] Lisbon scores 84!"},
                                     _call("scout_cities", {"music": "X"}), {"content": "grounded"}])
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "go"}]}).json()
    assert body["reply"] == "grounded" and body["steps"][0].startswith("scout_cities(")
    assert sent[1][-1]["content"] == chat_agent.TOOL_NUDGE
    _script_llm(monkeypatch, [{"content": "[scout_cities(music=X)]"}, {"content": "check_city(city=Lisbon) says 84"}])
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "go"}]}).json()
    assert "couldn't query Qloo" in body["reply"] and "84" not in body["reply"]


def test_blend_label_never_duplicates_the_user(monkeypatch):
    _script_llm(monkeypatch, [_call("blend_profiles", {"music": "Other", "label": "you and your partner"}), {"content": "ok"}])
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "blend"}], "profile": {"music": "X"}}).json()
    assert body["result"]["blend"] == {"a": "You", "b": "Partner"}


def test_scores_round_like_the_ui():
    """The card badge rounds (Math.round); the text must say the same number (0.829 -> 83, not 82)."""
    city = {"name": "Roundville", "entity_id": "c9", "query": {"affinity": 0.829}, "properties": {"geocode": {}}}
    result = agent.build_city(city, ([], True, 15, []), {"t": {"c9": 0.869}}, [{"id": "t", "name": "Taste", "category": "music"}])
    assert "scores 83/100" in result["why"] and "Taste (87)" in result["why"]


def test_pacer_spaces_requests_to_qloo_rate_limit():
    import asyncio, time
    pacer = agent._Pacer(rate=20.0, burst=1)
    async def run():
        t = time.monotonic()
        for _ in range(5):
            await pacer.wait()
        return time.monotonic() - t
    elapsed = asyncio.run(run())
    assert elapsed >= 0.19  # 5 requests at 20/s with no burst: the last waits ~4 x 50 ms


def test_429_waits_for_qloo_reset_header(monkeypatch):
    import asyncio, httpx
    seen = []
    def handler(request):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(429, headers={"x-second-ratelimit-reset": "0.1"})
        return httpx.Response(200, json={"results": [{"entity_id": "e1", "name": "X"}]})
    _real_qloo(monkeypatch, handler)
    slept = []
    real_sleep = asyncio.sleep
    async def fake_sleep(d, *a, **k):
        slept.append(d); await real_sleep(0)
    monkeypatch.setattr(agent.asyncio, "sleep", fake_sleep)
    async def run():
        q = agent.Qloo()
        return q, await q.search("x", "urn:entity:artist")
    q, found = asyncio.run(run())
    assert found["entity_id"] == "e1" and q.stats["/search"]["retries"] == 1 and q.failed == 0
    assert any(abs(d - 0.15) < 1e-6 for d in slept)  # reset 0.1 s + 50 ms margin, not a blind backoff
    agent._CACHE.clear()


def _region_dest(name, cc, region, lat, lon):
    d = _dest(name, cc, lat, lon)
    d["properties"]["geocode"]["admin1_region"] = region
    return d


def test_shortlist_spreads_across_regions_and_countries():
    ranked = [_region_dest("Reykjavik", "IS", "Capital", 64.1, -21.9),
              _region_dest("Pittsburgh", "US", "Pennsylvania", 40.4, -80.0),
              _region_dest("Lancaster", "US", "Pennsylvania", 40.0, -76.3),
              _region_dest("Scranton", "US", "Pennsylvania", 41.4, -75.7),
              _region_dest("Portland", "US", "Oregon", 45.5, -122.7),
              _region_dest("Asheville", "US", "North Carolina", 35.6, -82.6),
              _region_dest("Edinburgh", "GB", "Scotland", 55.9, -3.2)]
    names = [c["name"] for c in agent.unique_cities(ranked, 4)]
    assert names == ["Reykjavik", "Pittsburgh", "Portland", "Edinburgh"]  # 1 per state, 2 per country, rank order kept
    # A country filter means "only the US": still one per state, no per-country cap.
    us = [c["name"] for c in agent.unique_cities(ranked[1:6], 3, max_per_country=None)]
    assert us == ["Pittsburgh", "Portland", "Asheville"]


def test_diversity_never_shrinks_the_shortlist():
    same_state = [_region_dest(n, "US", "Pennsylvania", 40 + i, -76 - i) for i, n in enumerate(["A", "B", "C", "D"])]
    assert [c["name"] for c in agent.unique_cities(same_state, 4)] == ["A", "B", "C", "D"]


def test_lodging_without_categories_is_filtered_by_name():
    p = lambda name: {"name": name, "tags": []}
    assert not agent.is_relevant_place(p("Kimpton Hotel Monaco Pittsburgh"))
    assert agent.is_relevant_place(p("Kex Hostel Restaurant")) and agent.is_relevant_place(p("Hotel Bar Rooftop"))
    assert agent.is_relevant_place(p("Wood Street Galleries"))


def test_amenity_tags_are_not_vibe():
    for junk in ("Dining drinking", "Full bar", "Bar", "Tourist attraction", "Outdoor seating"):
        assert agent.vibe_tag({"name": junk}) is None
    assert agent.vibe_tag({"name": "Historic"}) == "Historic"


def test_location_label_reveals_where_qloo_places_a_city():
    assert agent.location_label("Billund", {"name": "Post Falls", "admin1_region": "Idaho", "country_code": "US"}) == \
        "Billund, Idaho US · near Post Falls"
    assert agent.location_label("Solvang", {"name": "Solvang", "admin1_region": "California", "country_code": "US"}) == \
        "Solvang, California US"
    assert agent.location_label("Haleiwa", {"name": "Hale'iwa", "admin1_region": "Hawaii", "country_code": "US"}) == "Haleiwa, Hawaii US"
    assert agent.location_label("Lisbon", {"country_code": "PT"}) == "Lisbon, PT"


def test_hotel_with_only_lodging_categories_is_dropped():
    p = {"name": "Kimpton Hotel Monaco", "tags": [{"name": "Lodging", "type": "urn:tag:category:place"}]}
    assert not agent.is_relevant_place(p)
    bar = {"name": "Hotel Monaco", "tags": [{"name": "Cocktail bar", "type": "urn:tag:category:place"}]}
    assert agent.is_relevant_place(bar)


# --- Name matching: Qloo search always returns something, so weak matches must be rejected.

@pytest.mark.parametrize(("query", "name", "ok"), [
    ("Radiohead", "Radiohead", True), ("Amelie", "Amélie", True), ("Radiohed", "Radiohead", True),
    ("The Bear", "The Bear", True), ("Lord of the Rings", "The Lord of the Rings: The Fellowship of the Ring", True),
    ("Taylor", "Taylor Swift", True), ("A", "A", True),
    ("zzqxv qwerty", "Qwerty", False), ("Sally Rooney", "Witch of the Glens (Sally Watson Family Series)", False),
    ("asdfgh", "Asdf Studio Hamburg", False),
])
def test_match_quality(query, name, ok):
    assert (agent.match_quality(query, name) >= agent.MIN_MATCH) is ok


def test_best_match_prefers_exact_name_over_qloo_order():
    results = [{"name": "Haruki Murakami's The Wind-up Bird Chronicle: A Reader's Guide"}, {"name": "Haruki Murakami"}]
    assert agent.best_match("Haruki Murakami", results)["name"] == "Haruki Murakami"
    assert agent.best_match("zzqxv", [{"name": "Qwerty"}]) is None
    assert agent.best_match("x", []) is None


def test_gibberish_taste_is_not_matched(monkeypatch):
    async def get(self, path, params):
        if path == "/search": return {"results": [{"entity_id": "q", "name": "Qwerty"}]}
        return {"results": {"tags": []}}
    monkeypatch.setattr(agent.Qloo, "get", get)
    r = c.post("/api/scout", json={"music": "zzqxv qwerty"})
    body = r.json()
    assert body["error"].startswith("Couldn't match any of your favorites") and body["unmatched"] == ["zzqxv qwerty"]


def test_books_resolve_to_their_author_as_a_person(monkeypatch):
    """Qloo rejects books as destination signals, so a title or author name becomes the author (a person)."""
    async def get(self, path, params):
        if path == "/search" and params["types"] == "urn:entity:book,urn:entity:person":
            if params["query"] == "Normal People":
                return {"results": [{"entity_id": "bk", "name": "Normal People", "types": ["urn:entity:book"],
                                     "disambiguation": "2018, Sally Rooney"}]}
            return {"results": [{"entity_id": "b0", "name": "Haruki Murakami's Reader's Guide", "types": ["urn:entity:book"]},
                                {"entity_id": "hm", "name": "Haruki Murakami", "types": ["urn:entity:person"]}]}
        if path == "/search" and params["types"] == "urn:entity:person":
            assert params["query"] == "Sally Rooney"
            return {"results": [{"entity_id": "sr", "name": "Sally Rooney", "types": ["urn:entity:person"]}]}
        raise AssertionError(params)
    monkeypatch.setattr(agent.Qloo, "get", get)
    import asyncio
    trace = []
    async def run():
        return await agent.resolve_profile(agent.Qloo(), {"books": "Normal People, Haruki Murakami"}, trace)
    entities, _ = asyncio.run(run())
    assert entities == [{"id": "sr", "name": "Sally Rooney", "category": "books"},
                        {"id": "hm", "name": "Haruki Murakami", "category": "books"}]
    assert trace[0] == "search: 'Normal People' → Normal People (book) → author Sally Rooney (person)"


def test_long_venue_names_are_shortened():
    assert agent.display_name("Biryani Zone, Marathahalli, , Hyderabadi Dum Biryani Restaurant") == "Biryani Zone"
    assert agent.display_name("Café de Flore") == "Café de Flore"
    assert agent.display_name("Bar, Restaurant") == "Bar, Restaurant"  # short names keep their commas


# --- Lifestyle words: Qloo ignores tags as signals, so they must be sent as filters.

def test_lifestyle_words_become_filters_not_signals(monkeypatch):
    seen = []
    async def get(self, path, params):
        seen.append(dict(params))
        if path == "/v2/tags":
            assert params["filter.parents.types"] == "urn:entity:place"
            return {"results": {"tags": [{"id": "urn:tag:decor:qloo:cafe", "name": "Cafe decor"},
                                         {"id": "urn:tag:genre:place:hotel:cafe", "name": "Cafe"},
                                         {"id": "urn:tag:category:place:cafe", "name": "Cafe"}]}}
        return await fake_get(self, path, params)
    monkeypatch.setattr(agent.Qloo, "get", get)
    r = c.post("/api/scout", json={"music": "Radiohead", "lifestyle": "cafes"})
    body = r.json()
    assert body["lifestyle_tags"][0]["place_tags"] == ["urn:tag:category:place:cafe"]
    assert body["lifestyle_tags"][0]["destination_tag"] == "urn:tag:genre:destination:foodie_destination"
    insights = [p for p in seen if "filter.type" in p]
    assert all("signal.interests.tags" not in p for p in insights)
    ranking = next(p for p in insights if p["filter.type"] == "urn:entity:destination" and "filter.results.entities" not in p)
    assert ranking["filter.tags"] == "urn:tag:genre:destination:foodie_destination"
    venues = [p for p in insights if p["filter.type"] == "urn:entity:place"]
    assert venues and venues[0]["filter.tags"] == "urn:tag:category:place:cafe"


def test_narrow_lifestyle_filter_falls_back_to_full_ranking(monkeypatch):
    calls = []
    async def fake_insights(self, **params):
        calls.append(params)
        if "filter__tags" in params: return []
        return [{"name": f"City{i}", "entity_id": f"c{i}", "query": {"affinity": 0.8}} for i in range(4)]
    monkeypatch.setattr(agent.Qloo, "insights", fake_insights)
    import asyncio
    trace = []
    tags = [{"id": "t", "name": "Beach", "word": "beach", "destination_tag": "urn:tag:genre:destination:beaches", "place_tags": []}]
    out = asyncio.run(agent.rank_destinations(agent.Qloo.__new__(agent.Qloo), 4, {"signal__interests__entities": "e"}, {}, tags, trace))
    assert len(out) == 4 and len(calls) == 2 and "too narrow" in trace[-1]


def test_lifestyle_only_profile_asks_for_a_favorite(monkeypatch):
    async def get(self, path, params):
        if path == "/v2/tags": return {"results": {"tags": [{"id": "urn:tag:category:place:beach", "name": "Beach"}]}}
        return {"results": {"entities": []}}
    monkeypatch.setattr(agent.Qloo, "get", get)
    body = c.post("/api/scout", json={"lifestyle": "beaches"}).json()
    assert "add at least one artist" in body["error"]


@pytest.mark.parametrize(("word", "tag"), [
    ("beaches", "beaches"), ("hiking", "outdoor_activities"), ("nightlife", "great_nightlife"), ("kids", "kids"),
    ("walkable", "urban_destinations"), ("art", "art_culture"), ("party", "great_nightlife"), ("skiing", "snow_ski"),
    ("vinyl", None),
])
def test_destination_genre_mapping(word, tag):
    found = agent.destination_tag(word)
    assert (found.rsplit(":", 1)[-1] if found else None) == tag


def test_place_tag_choice_skips_hotel_and_decor_tags():
    tags = [{"id": "urn:tag:children:place:kids_menu"}, {"id": "urn:tag:genre:place:restaurant:cafe"},
            {"id": "urn:tag:nearby_attraction:qloo:cafes"}, {"id": "urn:tag:category:place:cafe"}, {"id": "urn:tag:decor:qloo:cafe_style"}]
    assert [t["id"] for t in agent.place_tags(tags, "cafe")] == ["urn:tag:category:place:cafe", "urn:tag:nearby_attraction:qloo:cafes"]
    stores = [{"id": "urn:tag:category:place:store"}, {"id": "urn:tag:category:place:record_store"}]
    assert [t["id"] for t in agent.place_tags(stores, "record store")] == ["urn:tag:category:place:record_store"]


# --- Venue relevance and vibe.

def _place(name, *categories):
    return {"name": name, "tags": [{"name": c, "type": "urn:tag:category:place"} for c in categories]}


@pytest.mark.parametrize(("place", "keep"), [
    (_place("The Ritz-Carlton, San Juan", "Wedding venue", "Hotel"), False),
    (_place("Honu Kai Bed & Breakfast", "Hotel", "Bed breakfast"), False),
    (_place("Whole Foods Market", "Bakery", "Grocery store", "Health food store", "Supermarket", "Organic food store"), False),
    (_place("InStyle Wedding KYOTO", "Wedding chapel", "Banquet hall", "Wedding venue"), False),
    (_place("University of Kent Conference and Events", "Conference center", "Event planner", "Event venue"), False),
    (_place("Zaborin Ryokan", "Hotel", "Japanese restaurant", "Japanese inn"), True),
    (_place("earth hostel", "Hostel", "Cafe"), True),
    (_place("Museum of Art", "Art museum", "Tourist attraction"), True),
    (_place("Kex Hostel Restaurant", "Restaurant"), True),
])
def test_venue_relevance(place, keep):
    assert agent.is_relevant_place(place) is keep


def test_vibe_keeps_mood_tags_only():
    assert agent.vibe_tag({"name": "Lively", "type": "urn:tag:ambience:qloo"}) == "Lively"
    assert agent.vibe_tag({"name": "Creative district", "type": "urn:tag:setting:qloo"}) == "Creative district"
    for name, kind in [("Hair dryer", "amenity:qloo"), ("Telephone", "amenity:qloo"), ("Lunch", "dining_options:place"),
                       ("Price level", "payments:place"), ("Drive thru", "service_type:qloo"), ("Anniversary", "event:place")]:
        assert agent.vibe_tag({"name": name, "type": "urn:tag:" + kind}) is None


def test_known_for_reads_naturally():
    assert agent.known_for(["For the History", "With Kids", "Beaches"]) == ["history", "families with kids", "beaches"]


def _at(address):
    return {"name": "Spot", "properties": {"address": address}}


def test_mislocated_city_is_skipped_for_the_spare():
    city = {"name": "Doolin"}
    pottstown = [_at(f"{i} High St Pottstown, PA 19464") for i in range(6)]
    assert agent.mislocated(city, "Doolin, Pennsylvania US · near Stowe", pottstown)
    assert not agent.mislocated(city, "Doolin, Clare IE", pottstown)  # no "near": the geocode agrees with the name
    assert not agent.mislocated({"name": "Venice"}, "Venice, California US · near Marina del Rey",
                                pottstown + [_at("1 Ocean Front Walk Venice, CA 90291")])
    assert not agent.mislocated(city, "Doolin · near Stowe", pottstown[:3])  # too little evidence to judge
    trace = []
    built = [{"city": "A", "location": "A", "mislocated": False}, {"city": "Doolin", "location": "Doolin · near Stowe", "mislocated": True},
             {"city": "B", "location": "B", "mislocated": False}, {"city": "C", "location": "C", "mislocated": False}]
    assert [c["city"] for c in agent.located(built, 2, trace)] == ["A", "B"]
    assert trace == ["skipped Doolin: Qloo's coordinates for it point to Stowe (no venue there lists Doolin in its address)"]


@pytest.mark.parametrize(("word", "expected"), [("beaches", "beach"), ("galleries", "gallery"), ("cafes", "cafe"),
                                                ("parks", "park"), ("glass", "glass"), ("boxes", "box"), ("art", "art")])
def test_singular(word, expected):
    assert agent.singular(word) == expected


def test_shortlist_skips_a_town_next_to_a_pick():
    san_juan = _dest("San Juan", "PR", 18.46, -66.10)
    fajardo = _dest("Fajardo", "PR", 18.33, -65.65)   # ~50 km away, a different municipality
    bogota = _dest("Bogota", "CO", 4.71, -74.07)
    for i, c in enumerate([san_juan, fajardo, bogota]):
        c["properties"]["geocode"]["admin1_region"] = c["name"]
    assert [c["name"] for c in agent.unique_cities([san_juan, fajardo, bogota], 2)] == ["San Juan", "Bogota"]


def test_no_cities_returns_a_clear_error_without_empty_follow_up_calls(monkeypatch):
    seen = []
    async def get(self, path, params):
        seen.append(dict(params))
        if path == "/search": return {"results": [{"entity_id": "e1", "name": params["query"]}]}
        return {"results": {"entities": []}}
    monkeypatch.setattr(agent.Qloo, "get", get)
    body = c.post("/api/scout", json={"music": "Obscure", "country": "AQ"}).json()
    assert body["error"].startswith("Qloo found no cities") and "country limit" in body["error"]
    assert all(p.get("filter.results.entities", "x") for p in seen)  # never an empty filter (Qloo answers 400)


def test_town_names_are_not_venues():
    city = {"name": "San Simeon", "properties": {"geocode": {"admin1_region": "California", "country_code": "US"}}}
    places = [{"name": "Cambria, California"}, {"name": "Hearst Castle"}, {"name": "Anytown, United States"}]
    assert [p["name"] for p in agent.local_relevant(places, city, True)] == ["Hearst Castle"]


def test_chat_rejects_numbers_no_tool_returned(monkeypatch):
    """The model skipping the tool and inventing scores ("Barcelona 86/82") is sent back, then refused."""
    sent = _script_llm(monkeypatch, [{"content": "Barcelona (86/82) suits you both."},
                                     {"content": "Barcelona (86/82) really suits you both."}])
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "My partner loves Bad Bunny. Find a city for both."}]})
    body = r.json()
    assert body["reply"].startswith("Sorry, I couldn't ground that answer")
    assert "82, 86" in sent[1][-1]["content"] and sent[1][-1]["role"] == "user"


def test_chat_numbers_from_tool_output_are_allowed(monkeypatch):
    _script_llm(monkeypatch, [{"content": "Off the record: 77 points."},
                              _call("scout_cities", {"music": "Radiohead"}),
                              {"content": "Your top city scores 50."}])  # FAKE_CITY affinity 0.5 → 50
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "I love Radiohead"}]}).json()
    assert body["reply"] == "Your top city scores 50." and body["steps"][0].startswith("scout_cities(")


def test_chat_numbers_from_the_conversation_are_allowed(monkeypatch):
    _script_llm(monkeypatch, [{"content": "Yes, Glasgow's 83 came from Qloo."}])
    history = [{"role": "user", "content": "Where?"}, {"role": "assistant", "content": "Glasgow scores 83."},
               {"role": "user", "content": "Was 83 from Qloo?"}]
    assert c.post("/api/chat", json={"messages": history}).json()["reply"] == "Yes, Glasgow's 83 came from Qloo."


def test_blend_with_the_users_own_profile_is_rejected(monkeypatch):
    sent = _script_llm(monkeypatch, [_call("blend_profiles", {"music": "Radiohead", "label": "Partner"}),
                                     {"content": "Please tell me your partner's favorites."}])
    body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "Blend with my partner"}],
                                     "profile": {"music": "Radiohead"}}).json()
    assert "other person's own tastes" in sent[1][-1]["content"] and body["result"] is None
