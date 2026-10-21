"""Cloudflare Python Worker entrypoint: serves the same FastAPI app (static files are Workers assets)."""
import asyncio
import json
import os
from urllib.parse import urlparse

from pyodide.ffi import create_proxy
from workers import Response, WorkerEntrypoint, wait_until

# Worker vars/secrets arrive on self.env, not os.environ; app/agent read os.environ at import time.
ENV_KEYS = ("QLOO_API_KEY", "QLOO_BASE_URL", "QLOO_CONCURRENCY", "QLOO_CACHE_TTL_S", "QLOO_CACHE_MAX",
            "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "ENV", "TRUST_PROXY_HEADERS", "RATE_LIMIT_PER_MIN",
            "MAX_CONCURRENT_SCOUTS", "SCOUT_TIMEOUT_S", "CHAT_TIMEOUT_S")
_app = None


def _load_app(env):
    global _app
    if _app is None:
        for key in ENV_KEYS:
            try:
                value = getattr(env, key)
            except AttributeError:
                value = None
            if value is not None and str(value) not in ("", "undefined"):
                os.environ[key] = str(value)
        from app import app
        import chat_agent
        try:
            chat_agent.AI_BINDING = env.AI  # Workers AI binding powers the chat agent when no LLM_* endpoint is set
        except AttributeError:
            pass
        _app = app
    return _app


# path -> [(binding, key)]: "ip" keys per client, "global" is one shared bucket (a spend ceiling).
EDGE_LIMITS = {
    "/api/scout": [("SCOUT_LIMITER", "ip")],
    "/api/chat": [("CHAT_LIMITER", "ip"), ("CHAT_GLOBAL_LIMITER", "global")],
}


async def _edge_limited(env, request) -> bool:
    if request.method != "POST":
        return False
    rules = EDGE_LIMITS.get(urlparse(str(request.url)).path, [])
    ip = request.headers.get("cf-connecting-ip") or "unknown"
    for binding, scope in rules:
        try:
            limiter = getattr(env, binding)
        except AttributeError:
            continue  # binding not configured (e.g. local dev): the app's in-memory limiter still applies
        outcome = await limiter.limit({"key": ip if scope == "ip" else "global"})
        if not outcome["success"]:
            return True
    return False


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if await _edge_limited(self.env, request):
            return Response(json.dumps({"error": "Too many requests. Try again in a minute."}), status=429,
                            headers={"content-type": "application/json", "retry-after": "60",
                                     "x-content-type-options": "nosniff", "cache-control": "no-store"})
        import asgi
        task = asyncio.ensure_future(asgi.fetch(_load_app(self.env), request, self.env))
        if request.method == "POST":
            # A client that disconnects mid-scout (refresh, closed tab) cancels the request's pending fetches and
            # leaves the run suspended forever, holding its concurrency slot. waitUntil lets the run finish (and
            # fill the cache for the retry). Same proxy pattern as the SDK's own websocket handler.
            proxy = create_proxy(task)
            task.add_done_callback(lambda _: proxy.destroy())
            wait_until(proxy)
        return await task
