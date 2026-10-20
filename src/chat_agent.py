"""Conversational Relocation Scout: an LLM tool-use loop whose only source of facts is Qloo.

The LLM plans and talks; every recommendation, score, venue and neighborhood comes from a tool
backed by the Qloo pipeline in agent.py. Two LLM backends, both speaking OpenAI-style tool calls:
  * a Cloudflare Workers AI binding (set by src/entry.py on Workers; model from LLM_MODEL), or
  * any OpenAI-compatible endpoint (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL).
Without either, the chat panel is hidden and the app still runs the form flow.
"""
import json
import logging
import os
import re
from typing import Any

import httpx

import agent

log = logging.getLogger("scout.chat")

PROFILE_FIELDS = ("music", "films", "tv", "books", "food", "brands", "lifestyle", "country")
MAX_STEPS = 5
MAX_HISTORY = 8  # keeps every LLM call small (cost)
MAX_MESSAGE_CHARS = 600
# Chosen by a live 5-turn grounding test (every number in each reply checked against tool output):
# 0 ungrounded numbers and correct tool use, where others wrote tool calls as text or embellished.
DEFAULT_WORKERS_AI_MODEL = "@cf/mistralai/mistral-small-3.1-24b-instruct"
# A tool call written as text ("[scout_cities(music=...)]") instead of through the tool interface.
TEXT_TOOL_CALL = re.compile(r"\[?\s*(" + "|".join(["scout_cities", "check_city", "explore_city", "blend_profiles"]) + r")\s*\(")
TOOL_NUDGE = "Call the tools through the tool-calling interface, not as text. Do not answer until a tool has returned."
AI_BINDING: Any = None  # Workers AI binding (env.AI), injected by the Worker entrypoint

SYSTEM_PROMPT = """You are Relocation Scout, an agent that helps people decide where to live based on their cultural taste.
Your only source of facts about places is the Qloo tools. Rules:
- Call a tool before recommending or comparing anything. Never invent cities, neighborhoods, venues or scores.
- Quote Qloo numbers as given (0-100 affinity) and name the tastes that drive them.
- Keep the user's existing profile and change only what they ask. Profile fields are comma-separated names:
  music (artists), films, tv, books, food (restaurants/cafes), brands, lifestyle (keywords), country (2-letter ISO code or empty).
- When the user mentions a partner, friend or family member with different taste, use blend_profiles.
- When the user asks about a specific city, use check_city; for "where in X" or "best area for coffee in X", use explore_city.
- Qloo covers taste, not cost of living, visas, jobs, safety or weather. If asked, say so plainly; you may add a short,
  clearly labeled general-knowledge note, but never present it as Qloo data.
- Reply in at most 120 words, warm and specific. The app shows full city cards, so summarize rather than list everything."""

_PROFILE_PROPS = {
    "music": {"type": "string", "description": "Comma-separated artists"},
    "films": {"type": "string", "description": "Comma-separated films"},
    "tv": {"type": "string", "description": "Comma-separated TV shows"},
    "books": {"type": "string", "description": "Comma-separated books"},
    "food": {"type": "string", "description": "Comma-separated restaurants or cafes"},
    "brands": {"type": "string", "description": "Comma-separated brands"},
    "lifestyle": {"type": "string", "description": "Comma-separated lifestyle keywords, e.g. walkable, beaches"},
    "country": {"type": "string", "description": "Optional 2-letter ISO country code to restrict results, or empty"},
}

TOOLS = [
    {"type": "function", "function": {
        "name": "scout_cities",
        "description": "Set the user's full taste profile and get the best-matching cities from Qloo, with per-taste "
                       "attribution, neighborhoods, venues and local vibe. Pass the complete updated profile.",
        "parameters": {"type": "object", "properties": _PROFILE_PROPS, "required": []}}},
    {"type": "function", "function": {
        "name": "check_city",
        "description": "Score one specific city on the current profile and compare it with the top pick, taste by taste.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "explore_city",
        "description": "Find taste-matched neighborhoods and venues in one city for the current profile, optionally "
                       "focused on a keyword such as 'coffee', 'live music' or 'parks'.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "focus": {"type": "string"}},
                       "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "blend_profiles",
        "description": "Couple/family mode: find compromise cities for the user and another person. Cities are ranked by "
                       "the lower of the two people's Qloo scores. Pass the other person's profile and a short label.",
        "parameters": {"type": "object", "properties": {**_PROFILE_PROPS, "label": {"type": "string"}}, "required": []}}},
]


def _use_http() -> bool:
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))


def configured() -> bool:
    return _use_http() or AI_BINDING is not None


def clean_profile(raw: dict[str, Any] | None) -> dict[str, str]:
    """Same limits as the form: strings only, capped lengths, valid country code."""
    out: dict[str, str] = {}
    for name in PROFILE_FIELDS:
        value = (raw or {}).get(name)
        if isinstance(value, str) and value.strip():
            out[name] = re.sub(r"[\x00-\x1f\x7f<>{}]", "", value).strip()[:400]
    country = out.get("country", "").upper()
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        country = ""
    out["country"] = country
    if not country:
        out.pop("country")
    return out


def _brief_city(city: dict[str, Any]) -> dict[str, Any]:
    return {"city": city["city"], "location": city["location"], "affinity": round(city["affinity"] * 100),
            "drivers": [f"{d['taste']} {round(d['affinity'] * 100)}" for d in city["drivers"][:4]],
            "known_for": city["city_tags"][:3],
            "neighborhoods": [{"name": n["name"], "spots": n["spots"][:3]} for n in city["neighborhoods"][:3]],
            "top_spots": city["top_spots"][:4], "vibe": city["vibe"][:4]}


def _brief_result(result: dict[str, Any]) -> dict[str, Any]:
    """Compact, fact-only view of a pipeline result for the LLM (keeps tokens low)."""
    if result.get("error"):
        return {"error": result["error"]}
    unmatched = [line.split("'")[1] for line in result.get("trace", []) if line.endswith("→ no match") and "'" in line]
    brief: dict[str, Any] = {"matched_tastes": [m["name"] for m in result.get("matched", [])],
                             "unmatched_tastes": unmatched,
                             "cities": [_brief_city(c) for c in result.get("cities", [])]}
    if result.get("compare"):
        cmp = result["compare"]
        brief["check_city"] = {"verdict": cmp.get("verdict") or cmp.get("error"),
                               "rows": [f"{r['taste']}: {round(r['candidate'] * 100)} vs {round(r['top_pick'] * 100)}"
                                        for r in cmp.get("rows", [])]}
    if result.get("warning"):
        brief["warning"] = result["warning"]
    return brief


class Session:
    """One chat turn's state: the current profile, the latest renderable result and the step log."""

    def __init__(self, profile: dict[str, str]):
        self.profile = profile
        self.result: dict[str, Any] | None = None
        self.steps: list[str] = []

    async def run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "scout_cities":
            self.profile = clean_profile(args)
            self.steps.append("scout_cities(" + ", ".join(f"{k}={v}" for k, v in self.profile.items()) + ")")
            self.result = dict(await agent.scout(self.profile))
            return _brief_result(self.result)
        if not self.profile:
            return {"error": "No taste profile yet. Ask the user for a few favorites, then call scout_cities."}
        if name == "check_city":
            city = str(args.get("city") or "")[:60].strip()
            self.steps.append(f"check_city({city})")
            self.result = dict(await agent.scout({**self.profile, "compare": city}))
            return _brief_result(self.result)
        if name == "explore_city":
            city, focus = str(args.get("city") or "")[:60].strip(), str(args.get("focus") or "")[:40].strip()
            self.steps.append(f"explore_city({city}{', ' + focus if focus else ''})")
            found = await agent.spots(self.profile, city, focus)
            return {k: v for k, v in found.items() if k not in ("trace", "health")}
        if name == "blend_profiles":
            other = clean_profile(args)
            label = re.sub(r"[^\w '\-]", "", str(args.get("label") or "Partner"))[:24].strip()
            if not label or re.search(r"\b(you|your|me|my|us|both)\b", label, re.IGNORECASE):
                label = "Partner"  # models sometimes pass "you and your partner"; the user is already "You"
            self.steps.append(f"blend_profiles(You + {label})")
            self.result = dict(await agent.blend(self.profile, other, labels=("You", label)))
            return _brief_result(self.result)
        return {"error": f"Unknown tool {name}"}


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """OpenAI-style assistant message from either response shape Workers AI may return."""
    if data.get("choices"):
        return data["choices"][0]["message"]
    calls = [{"id": f"call{i:05d}", "type": "function",  # some models require 9-char alphanumeric ids
              "function": {"name": c.get("name"), "arguments": c["arguments"] if isinstance(c.get("arguments"), str)
                           else json.dumps(c.get("arguments") or {})}}
             for i, c in enumerate(data.get("tool_calls") or [])]
    return {"content": data.get("response"), "tool_calls": calls}


async def _complete_workers_ai(messages: list[dict[str, Any]]) -> dict[str, Any]:
    # The Python Workers SDK converts binding arguments and results (JS objects arrive as dict subclasses).
    payload = {"messages": messages, "tools": TOOLS, "max_tokens": 400, "temperature": 0.3}
    try:
        result = await AI_BINDING.run(os.getenv("LLM_MODEL") or DEFAULT_WORKERS_AI_MODEL, payload)
    except Exception as exc:  # JsException; message may echo the prompt, so log the type only
        log.warning("workers ai failed: %s", type(exc).__name__)
        raise RuntimeError("llm unavailable") from None
    return _normalize(result if isinstance(result, dict) else result.to_py())


async def _complete(client: httpx.AsyncClient, messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not _use_http():
        return await _complete_workers_ai(messages)
    response = await client.post(
        f"{os.environ['LLM_BASE_URL'].rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
        json={"model": os.environ["LLM_MODEL"], "messages": messages, "tools": TOOLS, "tool_choice": "auto",
              "max_tokens": 400, "temperature": 0.3})
    if response.status_code != 200:
        log.warning("llm returned HTTP %s", response.status_code)  # never log the body: it may echo the prompt
        raise RuntimeError("llm unavailable")
    return _normalize(response.json())


def clean_history(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Only user/assistant text from the client; tool messages are never accepted from outside."""
    out = []
    for message in (history or [])[-MAX_HISTORY:]:
        role, content = message.get("role"), message.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content.strip()[:MAX_MESSAGE_CHARS]})
    return out


async def chat(history: list[dict[str, Any]], profile: dict[str, Any] | None) -> dict[str, Any]:
    """Run one agent turn: plan, call Qloo tools (up to MAX_STEPS), then answer from tool results only."""
    session = Session(clean_profile(profile))
    turns = clean_history(history)
    if not turns or turns[-1]["role"] != "user":
        return {"error": "Send a message to the agent."}
    context = f"Current profile: {json.dumps(session.profile) if session.profile else 'empty'}"
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context}, *turns]
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0)) as client:
        nudged = False
        for _ in range(MAX_STEPS):
            message = await _complete(client, messages)
            calls = message.get("tool_calls") or []
            if not calls and TEXT_TOOL_CALL.search(message.get("content") or ""):
                # Anything after a text "tool call" is unfounded: retry once, then refuse rather than show it.
                if nudged:
                    message = {"content": "Sorry, I couldn't query Qloo for that. Please try rephrasing."}
                    break
                nudged = True
                messages.append({"role": "user", "content": TOOL_NUDGE})  # mid-chat system turns break some templates
                continue
            if not calls:
                break
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
            for call in calls[:3]:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    args = {}
                output = await session.run_tool(str(fn.get("name")), args)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(output, ensure_ascii=False, separators=(",", ":"))[:4000]})
            for call in calls[3:]:  # every tool_call must be answered for the next request to be valid
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps({"error": "Too many tool calls in one step."})})
        else:
            message = {"content": "I ran out of steps for this question. Try asking one thing at a time."}
    return {"reply": (message.get("content") or "").strip() or "Here's what Qloo found.",
            "profile": session.profile, "result": session.result, "steps": session.steps}
