"""Relocation Scout web app with security hardening (headers, limits, rate limiting)."""
import asyncio, logging, os, re, time
from collections import defaultdict, deque
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from agent import scout

log = logging.getLogger("scout")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

PROD = os.getenv("ENV", "production") != "development"
RATE_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "12"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SCOUTS", "3"))
SCOUT_TIMEOUT = float(os.getenv("SCOUT_TIMEOUT_S", "60"))
MAX_BODY = 4096
TRUST_PROXY = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
# Two-letter ISO 3166-1 alpha-2 format; Qloo ignores unknown codes, so format check is sufficient.
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

app = FastAPI(title="Relocation Scout", docs_url=None if PROD else "/docs",
              redoc_url=None, openapi_url=None if PROD else "/openapi.json")
_scout_slots = asyncio.Semaphore(MAX_CONCURRENT)
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
        ip = req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if ip: return ip
    return req.client.host if req.client else "unknown"

def rate_limited(ip: str) -> bool:
    now, q = time.monotonic(), _hits[ip]
    while q and now - q[0] > 60: q.popleft()
    if len(q) >= RATE_PER_MIN: return True
    q.append(now)
    if len(_hits) > 10000:  # bound memory
        for k in [k for k, v in _hits.items() if not v][:5000]: _hits.pop(k, None)
    return False

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

@app.middleware("http")
async def security(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and (not cl.isdigit() or int(cl) > MAX_BODY):
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
    if _scout_slots.locked():
        return JSONResponse({"error": "Busy right now. Try again shortly."}, status_code=503, headers={"Retry-After": "10"})
    async with _scout_slots:
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

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return FileResponse("static/favicon.svg", media_type="image/svg+xml")

@app.get("/healthz")
def health(): return {"ok": True}

app.mount("/static", StaticFiles(directory="static"), name="static")
@app.get("/")
def index(): return FileResponse("static/index.html")
