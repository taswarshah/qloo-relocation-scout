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
MAX_STEPS = 8
MAX_HISTORY = 8  # keeps every LLM call small (cost)
MAX_MESSAGE_CHARS = 600
# Chosen by a live 5-turn grounding test (every number in each reply checked against tool output):
# 0 ungrounded numbers and correct tool use, where others wrote tool calls as text or embellished.
DEFAULT_WORKERS_AI_MODEL = "@cf/mistralai/mistral-small-3.1-24b-instruct"
# A tool call written as text ("[scout_cities(music=...)]") instead of through the tool interface.
TEXT_TOOL_CALL = re.compile(r"\[?\s*(scout_cities|check_city|explore_city|blend_profiles)\s*\(")
TOOL_NUDGE = "Call the tools through the tool-calling interface, not as text. Do not answer until a tool has returned."
# Score-like numbers in a reply ("83", "86/82"); each must come from a Qloo tool result or the conversation.
NUMBER = re.compile(r"(?<![\d.])\d{2,3}(?![\d.])")
GROUNDING_NUDGE = ("Your answer contains numbers no Qloo tool returned ({numbers}). Do not guess: call the right tool "
                   "(blend_profiles for a partner, check_city for one city, scout_cities for a new profile) and answer "
                   "only from its result.")
# Comparing places on topics Qloo has no data for ("Huatulco is more affordable than Oaxaca City", seen live).
OFF_SCOPE_CLAIM = re.compile(
    r"\b(cheaper|pricier|more (affordable|expensive)|less (affordable|expensive)|(lower|higher) (cost|prices?)|"
    r"(rent|rents|prices?|housing) (is|are) (lower|higher|cheaper)|safer|less safe|more dangerous|"
    r"(lower|higher) crime|warmer|colder|sunnier)\b", re.IGNORECASE)
SCOPE_NUDGE = ("Qloo has no data on cost of living, rent, safety, visas, jobs or weather. Rewrite without comparing "
               "places on those topics: say plainly that Qloo doesn't cover it and suggest a specialist source.")
SCOPE_ANSWER = ("Qloo covers cultural taste, not cost of living, rent, safety, visas, jobs or weather, so I can't compare "
                "cities on that. A specialist source such as Numbeo (costs) or official government sites (visas) can help.")
# "Why not Lisbon?" must get Qloo's score for Lisbon; the model sometimes answered from a scout alone (seen in review).
CITY_QUESTION = re.compile(r"(?i:\b(?:why not|what about|how about|compared? (?:it |that |this )?(?:with|to)|versus|vs\.?))"
                           r"\s+([A-ZÀ-Þ][\w'.-]*(?:\s+[A-ZÀ-Þ][\w'.-]*){0,2})")
NOT_CITY = {"I", "My", "Me", "We", "Us", "The", "It", "That", "This", "You", "Your", "Our", "Them", "Her", "His", "Him"}
CITY_NUDGE = ("The user asked about {cities}. Call check_city once for each of them (one city per call) and answer "
              "only from the results.")
NO_PROFILE = ("The user's tastes aren't set yet. Call scout_cities first with every taste named in their message, even if "
              "some seem odd or contradictory (Qloo keeps what it can match), then call this tool again.")
PLACE = r"[A-ZÀ-Þ][\w'.-]*(?:\s+[A-ZÀ-Þ][\w'.-]*){0,2}"
CITY_PAIR = re.compile(rf"({PLACE})\s+(?i:or|vs\.?|versus)\s+({PLACE})")
CITY_LIST = re.compile(r"(?i:\b(?:compare|between)\b)[:\s]+([^?.!]+)")
MAX_CHECKED_CITIES = 5
MAX_CALLS_PER_STEP = MAX_CHECKED_CITIES  # "compare these four cities" fits in one step
# A question about cost, safety etc.: the reply may give taste scores, but must say what Qloo doesn't cover.
OFF_SCOPE_QUESTION = re.compile(r"(?i:\b(?:cheap\w*|cost\w*|expensive|afford\w*|rent|prices?|safe\w*|crime|dangerous|"
                                r"visas?|jobs?|salar\w*|weather|climate)\b)")
SCOPE_NOTE = "Qloo covers cultural taste, not cost of living, safety, visas, jobs or weather, so this compares taste only."
# Letters outside the Latin script ("کیا یہ شہر سستا ہے؟"): the reply should be in the same script.
NON_LATIN = re.compile(r"[^\W\d_A-Za-zÀ-ɏ]")
LANGUAGE_NUDGE = "Reply in the same language and script the user wrote in, not English. Keep every name and number."
# Another person's tastes in the message ("My partner loves Bad Bunny") means couple mode.
PARTNER = re.compile(r"(?i:\b(?:my|our)\s+(?:partner|wife|husband|girlfriend|boyfriend|spouse|fianc[ée]e?|friend|roommate|"
                     r"kids?|family|son|daughter|mum|mom|dad|mother|father)\b[^.?!]*?\b(?:loves?|likes?|is into|enjoys?|prefers?|adores?)\b)")
PARTNER_NUDGE = ("The user described another person's tastes. The user's own tastes are the ones after 'I love/like'; "
                 "the other person's follow 'my partner loves' (or friend, wife...). If the profile is empty, call "
                 "scout_cities with the user's own tastes first; then call blend_profiles with only the other person's.")
# Code in a reply means the model went off task ("write me a Python script").
CODE_REPLY = re.compile(r"```|^\s*(?:def |import |class |print\()", re.MULTILINE)
OFF_TOPIC_ANSWER = "I can only help you find places to live that fit your taste. Tell me what you love and I'll check Qloo."
AI_BINDING: Any = None  # Workers AI binding (env.AI), injected by the Worker entrypoint

SYSTEM_PROMPT = """You are Relocation Scout, an agent that helps people decide where to live based on their cultural taste.
Your only source of facts about places is the Qloo tools. Rules:
- Call a tool before recommending or comparing anything. Never invent cities, neighborhoods, venues or scores.
- Quote Qloo numbers as given (0-100 affinity) and name the tastes that drive them.
- Describe each city only with its own known_for, vibe and spots. Never say "all" or "both" cities share a trait
  unless every one of them lists it.
- Keep the user's existing profile and change only what they ask. Profile fields are comma-separated names:
  music (artists), films, tv, books, food (restaurants/cafes), brands, lifestyle (keywords), country (2-letter ISO code or empty).
- If a tool lists unmatched_tastes, tell the user Qloo didn't recognize them and suggest a more specific name.
- When the user mentions a partner, friend or family member with different taste, use blend_profiles. If the profile
  is empty, first call scout_cities with the user's own tastes, then blend_profiles with only the other person's.
  After blend_profiles, recommend only its cities (they suit both people), not either person's own list.
- When the user names cities, call check_city once for each of them; for "where in X" or "best area for coffee in X",
  use explore_city. Never describe a city that no tool returned in this conversation.
- Qloo covers taste, not cost of living, visas, jobs, safety or weather. If asked, say so plainly and suggest a
  specialist source. Never compare or rank places on those topics, not even from general knowledge.
- Only help with choosing where to live and exploring places by taste. Decline anything else (writing code, essays,
  general questions) in one sentence.
- Reply in the language the user writes in, in at most 120 words, warm and specific. The app shows full city cards, so summarize rather than list everything."""

_PROFILE_PROPS = {
    "music": {"type": "string", "description": "Comma-separated artists"},
    "films": {"type": "string", "description": "Comma-separated films"},
    "tv": {"type": "string", "description": "Comma-separated TV shows"},
    "books": {"type": "string", "description": "Comma-separated books or authors"},
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
                       "the lower of the two people's Qloo scores. Pass ONLY the other person's tastes (the user's profile "
                       "is already known) and a short label such as 'Partner'.",
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
            "top_spots": city["top_spots"][:4], "vibe": city["vibe"][:4],
            **({"no_spots_found_for": city["venue_filter_missed"]} if city.get("venue_filter_missed") else {})}


def _brief_result(result: dict[str, Any]) -> dict[str, Any]:
    """Compact, fact-only view of a pipeline result for the LLM (keeps tokens low)."""
    if result.get("error"):
        return {"error": result["error"]}
    unmatched = result.get("unmatched") or agent.unmatched(result.get("trace", []))
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


def blend_reply(result: dict[str, Any]) -> str:
    """The couple-mode answer, written from Qloo fields only. The model mixed up whose tastes were whose
    ("you both love Radiohead") and added places Qloo never returned, so this reply is not left to it."""
    a, b = result["blend"]["a"], result["blend"]["b"]
    lines = ["Cities that suit you both, ranked by the lower of your two Qloo taste scores so neither of you is settling:"]
    for city in result.get("cities", []):
        scores = {d["taste"]: round(d["affinity"] * 100) for d in city["drivers"]}
        parts = [f"**{city['location']}**: {a} {scores.get(a, '?')} · {b} {scores.get(b, '?')}."]
        if city["city_tags"]:
            parts.append(f"Known for {', '.join(agent.known_for(city['city_tags'][:3]))}.")
        if city["neighborhoods"]:
            hood = city["neighborhoods"][0]
            parts.append(f"Start in {hood['name']} ({', '.join(hood['spots'][:2])}).")
        elif city["top_spots"]:
            parts.append(f"Spots to try: {', '.join(city['top_spots'][:2])}.")
        if city["vibe"]:
            parts.append(f"Vibe: {', '.join(v.lower() for v in city['vibe'][:3])}.")
        lines.append("- " + " ".join(parts))
    people = result.get("people") or {}
    if people:
        lines.append(f"Matched in Qloo for {a}: {', '.join(people.get(a, [])) or 'nothing'}. "
                     f"For {b}: {', '.join(people.get(b, [])) or 'nothing'}.")
    if result.get("unmatched"):
        lines.append(f"Qloo didn't recognize: {', '.join(result['unmatched'])}. Try a more specific name.")
    return "\n".join(lines)


class Session:
    """One chat turn's state: the current profile, the latest renderable result and the step log."""

    def __init__(self, profile: dict[str, str], parts: tuple[str, str] | None = None):
        self.profile = profile
        self.result: dict[str, Any] | None = None
        self.steps: list[str] = []
        self.parts = parts  # (user's words, other person's words) when the message describes both
        self.final: str | None = None  # a reply built from tool data, used instead of the model's text

    def misplaced(self, profile: dict[str, str], own: bool) -> list[str]:
        """Tastes given to the wrong person: named only in the other person's part of the message."""
        if not self.parts:
            return []
        mine, theirs = (p.lower() for p in self.parts)
        if not own:
            mine, theirs = theirs, mine
        return [name for k, v in profile.items() if k != "country" for name in agent.split(v)
                if name.lower() in theirs and name.lower() not in mine]

    async def run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "scout_cities":
            wrong = self.misplaced(clean_profile(args), own=True)
            if wrong:
                return {"error": f"{', '.join(wrong)}: these are the partner's tastes, not the user's. Call scout_cities "
                                 "with only the user's own tastes, then blend_profiles with the partner's."}
            self.profile = clean_profile(args)
            self.steps.append("scout_cities(" + ", ".join(f"{k}={v}" for k, v in self.profile.items()) + ")")
            self.result = dict(await agent.scout(self.profile))
            return _brief_result(self.result)
        if not any(v for k, v in self.profile.items() if k != "country"):
            return {"error": NO_PROFILE}
        if name == "check_city":
            city = str(args.get("city") or "")[:60].strip()
            self.steps.append(f"check_city({city})")
            self.result = dict(await agent.scout({**self.profile, "compare": city}))
            return _brief_result(self.result)
        if name == "explore_city":
            city, focus = str(args.get("city") or "")[:60].strip(), str(args.get("focus") or "")[:40].strip()
            self.steps.append(f"explore_city({city}{', ' + focus if focus else ''})")
            found = await agent.spots(self.profile, city, focus)
            brief = {k: v for k, v in found.items() if k not in ("trace", "health")}
            if "affinity" in brief:
                brief["affinity"] = round(brief["affinity"] * 100)  # the 0-100 scale the cards and other tools use
            return brief
        if name == "blend_profiles":
            other = clean_profile(args)
            tastes = {k: v for k, v in other.items() if k != "country"}
            wrong = self.misplaced(other, own=False)
            if wrong:
                return {"error": f"{', '.join(wrong)}: these are the user's own tastes. Pass only the partner's tastes."}
            if not tastes or tastes == {k: v for k, v in self.profile.items() if k != "country"}:
                return {"error": "Pass the other person's own tastes (e.g. music='Bad Bunny, Daft Punk', lifestyle='beaches'), "
                                 "not the user's profile."}
            label = re.sub(r"[^\w '\-]", "", str(args.get("label") or "Partner"))[:24].strip()
            if not label or re.search(r"\b(you|your|me|my|us|both)\b", label, re.IGNORECASE):
                label = "Partner"  # models sometimes pass "you and your partner"; the user is already "You"
            self.steps.append(f"blend_profiles(You + {label}: " + ", ".join(f"{k}={v}" for k, v in tastes.items()) + ")")
            self.result = dict(await agent.blend(self.profile, other, labels=("You", label)))
            if not self.result.get("error"):
                self.final = blend_reply(self.result)
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
    except Exception as exc:  # noqa: BLE001 - JsException; message may echo the prompt, so log the type only
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


def ungrounded(reply: str, evidence: str) -> list[str]:
    """Score-like numbers in the reply that appear nowhere in tool output or the conversation."""
    known = set(NUMBER.findall(evidence))
    return sorted(set(NUMBER.findall(reply)) - known)


def asked_city(text: str) -> str | None:
    """The city in "why not X?" / "what about X?" questions, if the user named one."""
    match = CITY_QUESTION.search(text)
    if not match or match.group(1).split()[0] in NOT_CITY:
        return None
    return match.group(1)


def split_partner(text: str) -> tuple[str, str] | None:
    """(user's words, other person's words) when one message describes both people's tastes."""
    match = PARTNER.search(text)
    if not match:
        return None
    end = re.search(r"[.?!;]|(?:,|\b(?:and|but|while|whereas)\b)?\s*\bI(?:'m| am)?\s+(?:love|like|into|enjoy|prefer|adore)",
                    text[match.end():])
    stop = match.end() + end.start() if end else len(text)
    return text[:match.start()] + text[stop:], text[match.start():stop]


def non_latin_share(text: str) -> float:
    """Share of letters outside the Latin script."""
    letters = [ch for ch in text if ch.isalpha()]
    return sum(1 for ch in letters if NON_LATIN.match(ch)) / len(letters) if letters else 0.0


def asked_cities(text: str) -> list[str]:
    """Every city the user asks about: "why not X?", "X or Y?", "compare A, B and C" (at most MAX_CHECKED_CITIES)."""
    found = [m.group(1) for m in CITY_QUESTION.finditer(text)]
    if "?" in text or CITY_LIST.search(text):
        found += [name for m in CITY_PAIR.finditer(text) for name in m.groups()]
    for m in CITY_LIST.finditer(text):
        found += [part.strip() for part in re.split(r",|\band\b|\bor\b|\bvs\.?", m.group(1)) if part.strip()]
    cities: list[str] = []
    for name in found:
        if re.fullmatch(PLACE, name) and name.split()[0] not in NOT_CITY and name not in cities:
            cities.append(name)
    return cities[:MAX_CHECKED_CITIES]


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
    session = Session(clean_profile(profile), split_partner(history[-1].get("content", "") if history else ""))
    turns = clean_history(history)
    if not turns or turns[-1]["role"] != "user":
        return {"error": "Send a message to the agent."}
    context = f"Current profile: {json.dumps(session.profile) if session.profile else 'empty'}"
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + context}, *turns]
    # Everything the reply may quote numbers from: the conversation, plus every tool result this turn.
    evidence = [t["content"] for t in turns]
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0)) as client:
        nudged = grounded_retry = scope_retry = city_nudged = partner_nudged = False
        cities = asked_cities(turns[-1]["content"])
        partner = bool(PARTNER.search(turns[-1]["content"]))
        foreign = non_latin_share(turns[-1]["content"]) > 0.3
        language_nudged = False
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
            if not calls and CODE_REPLY.search(message.get("content") or ""):
                message = {"content": OFF_TOPIC_ANSWER}
                break
            missing = [] if calls else ungrounded(message.get("content") or "", "\n".join(evidence))
            if missing:
                # A number that no tool returned is a hallucinated score: send the model back to the tools once,
                # then refuse rather than show it.
                if grounded_retry:
                    message = {"content": "Sorry, I couldn't ground that answer in Qloo data. Please try rephrasing."}
                    break
                grounded_retry = True
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append({"role": "user", "content": GROUNDING_NUDGE.format(numbers=", ".join(missing))})
                continue
            if not calls and OFF_SCOPE_CLAIM.search(message.get("content") or ""):
                # Qloo can't back a cost/safety/weather comparison: ask once for a plain answer, then give a fixed one.
                if scope_retry:
                    message = {"content": SCOPE_ANSWER}
                    break
                scope_retry = True
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append({"role": "user", "content": SCOPE_NUDGE})
                continue
            if not calls and partner and not partner_nudged and not any(s.startswith("blend_profiles(") for s in session.steps):
                # Both people's tastes are in the message: couple mode, not "tell me more".
                partner_nudged = True
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append({"role": "user", "content": PARTNER_NUDGE})
                continue
            unchecked = [c for c in cities if not any(
                s.startswith(("check_city(", "explore_city(")) and c.lower() in s.lower() for s in session.steps)]
            if not calls and unchecked and not city_nudged and not partner:
                # Every city the user names gets Qloo's score, not a description from the model's memory.
                city_nudged = True
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                nudge = CITY_NUDGE.format(cities=", ".join(unchecked))
                if not any(v for k, v in session.profile.items() if k != "country"):
                    nudge = "First call scout_cities with every taste the user named, even if some seem contradictory. " + nudge
                messages.append({"role": "user", "content": nudge})
                continue
            has_profile = any(v for k, v in session.profile.items() if k != "country")
            if not calls and unchecked and city_nudged and has_profile and not partner:
                # Nudged once and still skipped some: look them up here rather than let the model describe them
                # from memory (seen live: "Berlin ... David Hasselhoff Museum"), then let it answer from the results.
                calls = [{"id": f"auto{i}", "type": "function",
                          "function": {"name": "check_city", "arguments": json.dumps({"city": city})}}
                         for i, city in enumerate(unchecked)]
                message = {"content": ""}
            if not calls and foreign and not language_nudged and non_latin_share(message.get("content") or "") < 0.3:
                language_nudged = True
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append({"role": "user", "content": LANGUAGE_NUDGE})
                continue
            if not calls:
                break
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
            for call in calls[:MAX_CALLS_PER_STEP]:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    args = {}
                output = await session.run_tool(str(fn.get("name")), args)
                evidence.append(json.dumps(output, ensure_ascii=False))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(output, ensure_ascii=False, separators=(",", ":"))[:4000]})
            for call in calls[MAX_CALLS_PER_STEP:]:  # every tool_call must be answered for the next request to be valid
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps({"error": "Too many tool calls in one step."})})
        else:
            message = {"content": "I ran out of steps for this question. Try asking one thing at a time."}
    reply = session.final or (message.get("content") or "").strip() or "Here's what Qloo found."
    if OFF_SCOPE_QUESTION.search(turns[-1]["content"]) and not re.search(r"(?i)\b(?:doesn't|does not|don't|do not|can't|cannot)\b.{0,40}\b(?:cover|compare|help|data)", reply) and not foreign:
        reply += "\n\n" + SCOPE_NOTE
    return {"reply": reply,
            "profile": session.profile, "result": session.result, "steps": session.steps}
