"""Relocation Scout web app with security hardening (headers, limits, rate limiting)."""
import asyncio, logging, os, re, time
from collections import defaultdict, deque
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
# Endpoints are all `async def`: sync ones run in a thread pool, and Python Workers (Pyodide) have no threads.
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from agent import scout
import chat_agent

log = logging.getLogger("scout")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

PROD = os.getenv("ENV", "production") != "development"
RATE_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "12"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SCOUTS", "3"))
SCOUT_TIMEOUT = float(os.getenv("SCOUT_TIMEOUT_S", "60"))
MAX_BODY = 4096
MAX_CHAT_BODY = 16384  # a short chat history plus the profile
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT_S", "90"))
TRUST_PROXY = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
# Two-letter ISO 3166-1 alpha-2 format; Qloo ignores unknown codes, so format check is sufficient.
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

app = FastAPI(title="Relocation Scout", docs_url=None if PROD else "/docs",
              redoc_url=None, openapi_url=None if PROD else "/openapi.json")
# Concurrent scout/chat runs, as {token: start time}. Not a semaphore: a Worker request torn down mid-run
# never releases, and leaked permits made every later request wait. A stale entry expires instead.
_slots: dict[object, float] = {}

def _take_slot() -> object | None:
    now = time.monotonic()
    for token, started in list(_slots.items()):
        if now - started > max(SCOUT_TIMEOUT, CHAT_TIMEOUT) + 10:
            _slots.pop(token, None)
    if len(_slots) >= MAX_CONCURRENT:
        return None
    token = object()
    _slots[token] = now
    return token
_hits: dict[str, deque] = defaultdict(deque)

Field80 = lambda: Field(default="", max_length=400)

class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    music: str = Field80(); films: str = Field80(); tv: str = Field80(); books: str = Field80()
    food: str = Field80(); brands: str = Field80(); lifestyle: str = Field80()
    country: str = Field(default="", max_length=2)
    compare: str = Field(default="", max_length=60)

    @field_validator("compare")
    @classmethod
    def _compare(cls, v: str) -> str:
        if v and not re.fullmatch(r"[^\x00-\x1f\x7f<>{}]{1,60}", v):
            raise ValueError("invalid city name")
        return v

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        v = v.upper()
        if v and not COUNTRY_RE.match(v):
            raise ValueError("country must be a 2-letter ISO code")
        return v

def client_ip(req: Request) -> str:
    if TRUST_PROXY:  # only enable behind a trusted proxy (e.g. Cloudflare)
        # The leftmost X-Forwarded-For entry is client-controlled; the rightmost is the one our proxy appended.
        ip = req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if ip: return ip
    return req.client.host if req.client else "unknown"

def rate_limited(ip: str) -> bool:
    now, q = time.monotonic(), _hits[ip]
    while q and now - q[0] > 60: q.popleft()
    if len(q) >= RATE_PER_MIN: return True
    q.append(now)
    if len(_hits) > 10000:  # bound memory: drop IPs with no hit in the last minute
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]: _hits.pop(k, None)
    return False

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

@app.middleware("http")
async def security(request: Request, call_next):
    cl = request.headers.get("content-length")
    limit = MAX_CHAT_BODY if request.url.path == "/api/chat" else MAX_BODY
    if cl and (not cl.isdigit() or int(cl) > limit):
        resp = JSONResponse({"error": "Request too large."}, status_code=413)
    elif request.method == "POST" and "content-length" not in request.headers:
        resp = JSONResponse({"error": "Content-Length required."}, status_code=411)
    else:
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("unhandled error on %s", request.url.path)
            resp = JSONResponse({"error": "Something went wrong."}, status_code=500)
    h = resp.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    if PROD: h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

@app.exception_handler(RequestValidationError)
async def _bad_input(request: Request, exc: RequestValidationError):
    return JSONResponse({"error": "Invalid input. Use short comma-separated lists and a 2-letter country code."}, status_code=422)

@app.post("/api/scout")
async def api_scout(p: Profile, request: Request):
    if rate_limited(client_ip(request)):
        return JSONResponse({"error": "Too many requests. Try again in a minute."}, status_code=429, headers={"Retry-After": "60"})
    slot = _take_slot()
    if slot is None:
        return JSONResponse({"error": "Busy right now. Try again shortly."}, status_code=503, headers={"Retry-After": "10"})
    try:
        try:
            result = await asyncio.wait_for(scout(p.model_dump()), timeout=SCOUT_TIMEOUT)
            if result.get("error") and (result.get("health") or {}).get("failed_calls"):
                return JSONResponse(result, status_code=503)  # upstream outage, not "no matches"
            return result
        except asyncio.TimeoutError:
            return JSONResponse({"error": "Took too long. Try fewer tastes."}, status_code=504)
        except Exception:
            log.exception("scout failed")  # httpx errors include URL only; key is in a header, never logged
            return JSONResponse({"error": "Something went wrong."}, status_code=502)
    finally:
        _slots.pop(slot, None)

# Local/Docker: FastAPI serves the frontend. On Cloudflare Workers the same files are served as static assets.
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=chat_agent.MAX_MESSAGE_CHARS * 2)

class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(min_length=1, max_length=chat_agent.MAX_HISTORY * 2)
    profile: Profile = Field(default_factory=Profile)

@app.get("/api/config")
async def config(): return {"chat": chat_agent.configured()}

@app.post("/api/chat")
async def api_chat(body: ChatRequest, request: Request):
    if not chat_agent.configured():
        return JSONResponse({"error": "The chat agent isn't enabled on this server. Use the form instead."}, status_code=501)
    if rate_limited(client_ip(request)):
        return JSONResponse({"error": "Too many requests. Try again in a minute."}, status_code=429, headers={"Retry-After": "60"})
    slot = _take_slot()
    if slot is None:
        return JSONResponse({"error": "Busy right now. Try again shortly."}, status_code=503, headers={"Retry-After": "10"})
    try:
        try:
            profile = body.profile.model_dump(exclude={"compare"})
            result = await asyncio.wait_for(chat_agent.chat([m.model_dump() for m in body.messages], profile), timeout=CHAT_TIMEOUT)
            return JSONResponse(result, status_code=422 if result.get("error") else 200)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "The agent took too long. Try a simpler question."}, status_code=504)
        except Exception:
            log.exception("chat failed")
            return JSONResponse({"error": "The agent is unavailable right now. The form still works."}, status_code=502)
    finally:
        _slots.pop(slot, None)

@app.get("/healthz")
async def health(): return {"ok": True}

if os.path.isdir(STATIC):
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon(): return FileResponse(os.path.join(STATIC, "favicon.svg"), media_type="image/svg+xml")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    @app.get("/")
    async def index(): return FileResponse(os.path.join(STATIC, "index.html"))
