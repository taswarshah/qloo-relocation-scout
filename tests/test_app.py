"""Offline tests. Qloo is mocked with synthetic data; no real Qloo responses are stored in the repo."""
import os, sys
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
    monkeypatch.setattr(agent.Qloo, "get", fake_get)
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
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
