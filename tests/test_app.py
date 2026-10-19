"""Offline tests. Qloo is mocked with synthetic data; no real Qloo responses are stored in the repo."""
import os, sys
import selectors
import socket
from html.parser import HTMLParser
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QLOO_API_KEY", "test-key-not-real")
os.environ["RATE_LIMIT_PER_MIN"] = "3"
import pytest
from fastapi.testclient import TestClient
import app as appmod, agent

FAKE_CITY = {"name": "<img src=x onerror=alert(1)>", "entity_id": "c1", "properties": {"geocode": {"country_code": "ZZ"}},
             "query": {"affinity": 0.5}, "tags": [{"name": "Fake tag"}]}

async def fake_get(self, path, params):
    if path == "/search": return {"results": [{"entity_id": "e1", "name": "Synthetic Artist"}]}
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
    assert "Top spots" in result["why"]
    assert "Invented Favorite Gallery" in result["why"]
    assert "Invented Bookshop" in result["why"]
    assert "Invented Foreign Club" not in result["why"]


def test_empty_places_are_handled(monkeypatch):
    result = scout_places(monkeypatch, [])
    assert result["neighborhoods"] == []
    assert result["top_spots"] == []
    assert "Top spots" not in result["why"]


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
