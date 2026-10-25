document.documentElement.classList.add("js");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const presets = [
  { music: "Radiohead, Bon Iver", films: "Amelie, Lost in Translation", food: "Blue Bottle Coffee", lifestyle: "art, cafes, walkable" },
  { music: "Daft Punk, Bad Bunny", tv: "The Bear", food: "Nobu", lifestyle: "nightlife, beaches" },
  { music: "Coldplay, Taylor Swift", films: "Up, Paddington", lifestyle: "hiking, kids, nature", country: "US" },
  { music: "Tycho, Bonobo", films: "Her, Ex Machina", tv: "Black Mirror", lifestyle: "coworking, cafes, walkable" }
];
const form = document.getElementById("f");
const output = document.getElementById("out");
const loading = document.getElementById("loading");
const submit = document.getElementById("submit");
const presetButtons = document.querySelectorAll("[data-preset]");
let busy = false;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function chips(items, className = "chip") {
  const list = element("div", null, "chips");
  for (const item of items || []) list.append(element("span", item, className));
  return list;
}

function message(title, text, isError = false) {
  const card = element("div", null, isError ? "message error-state" : "empty-state");
  card.append(element("h3", title), element("p", text));
  output.replaceChildren(card);
}

function scoreBadge(affinity) {
  const score = Math.round(Math.min(1, Math.max(0, Number(affinity) || 0)) * 100);
  const badge = element("div", null, "score-badge");
  const number = element("strong", reduceMotion ? `${score}` : "0");
  badge.append(number, element("span", "/ 100"), element("small", "taste affinity"));
  if (!reduceMotion) countUp(number, score);
  return badge;
}

function countUp(node, target, ms = 900) {
  const start = performance.now();
  const tick = now => {
    const t = Math.min(1, (now - start) / ms);
    node.textContent = String(Math.round(target * (1 - Math.pow(1 - t, 3))));
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function bar(score, label) {
  const track = element("span", null, "bar");
  track.setAttribute("role", "img");
  track.setAttribute("aria-label", label);
  const fill = element("span", null, "bar-fill");
  track.append(fill);
  // Set after the first paint so the width transition animates from zero (CSSOM writes are CSP-safe).
  requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = `${score}%`; }));
  return track;
}

function localSection(city) {
  const section = element("section", null, "local-section");
  if (city.venue_filter?.length) section.append(element("p", `Spots picked for: ${city.venue_filter.join(" · ")}`, "venue-filter"));
  else if (city.venue_filter_missed?.length) section.append(element("p", `No spots tagged ${city.venue_filter_missed.join(" · ")} found here, so these are your top taste matches.`, "venue-filter"));
  if (city.neighborhoods?.length) {
    section.append(element("h4", "Neighborhoods to explore"));
    const list = element("ul", null, "neighborhood-list");
    for (const neighborhood of city.neighborhoods) {
      const item = element("li");
      item.append(element("strong", neighborhood.name), element("p", (neighborhood.spots || []).join(" · ")));
      list.append(item);
    }
    section.append(list);
  } else if (city.top_spots?.length) {
    section.append(element("h4", "Top spots"), element("p", "No neighborhood detail yet. Start with these taste-matched places.", "muted"));
    const list = element("ul", null, "spot-list");
    for (const spot of city.top_spots) list.append(element("li", spot));
    section.append(list);
  } else {
    section.append(element("p", "Local spot detail is unavailable for this city. Explore the cultural match as a starting point.", "muted"));
  }
  return section;
}

const HUES = [160, 28, 200, 340, 95, 250];
function cityCard(city, index) {
  const card = element("article", null, "city-card");
  card.style.setProperty("--i", String(index));
  card.style.setProperty("--hue", String(HUES[index % HUES.length]));
  const header = element("div", null, "city-heading");
  const title = element("div");
  title.append(element("p", `MATCH ${String(index + 1).padStart(2, "0")}`, "eyebrow"), element("h3", city.city || city.location));
  title.append(element("p", city.location, "muted"));
  header.append(title, scoreBadge(city.affinity));
  card.append(header, element("p", city.why, "city-explanation"));
  const pick = element("label", null, "compare-toggle");
  const box = document.createElement("input");
  box.type = "checkbox"; box.dataset.index = String(index);
  box.addEventListener("change", () => onPick(box));
  pick.append(box, " Compare side by side");
  card.append(pick);
  if (city.drivers?.length) {
    const drivers = element("div", null, "taste-drivers");
    drivers.append(element("h4", "Your strongest connections"));
    const bars = element("ul", null, "driver-bars");
    for (const driver of city.drivers) {
      const score = Math.round(Math.min(1, Math.max(0, Number(driver.affinity) || 0)) * 100);
      const row = element("li");
      row.append(element("span", driver.taste, "driver-name"), bar(score, `${driver.taste} affinity ${score} of 100`),
                 element("span", String(score), "driver-score"));
      bars.append(row);
    }
    drivers.append(bars);
    card.append(drivers);
  }
  card.append(localSection(city));
  if (city.vibe?.length) {
    const vibe = element("div", null, "vibe-section");
    vibe.append(element("h4", "Local vibe"), chips(city.vibe));
    card.append(vibe);
  }
  return card;
}

let shownCities = [];
const picked = [];
function onPick(box) {
  const i = Number(box.dataset.index);
  if (box.checked) { picked.push(i); if (picked.length > 2) { const drop = picked.shift(); const other = output.querySelector(`input[data-index="${drop}"]`); if (other) other.checked = false; } }
  else { const at = picked.indexOf(i); if (at >= 0) picked.splice(at, 1); }
  renderComparison();
}

function pct(x) { return Math.round(Math.min(1, Math.max(0, Number(x) || 0)) * 100); }

function signals(city) { return new Set([...(city.city_tags || []), ...(city.vibe || [])].map(t => String(t).toLowerCase())); }

function renderComparison() {
  const slot = document.getElementById("comparison");
  if (!slot) return;
  if (picked.length < 2) {
    slot.replaceChildren(element("p", picked.length ? "Pick one more city to compare." : "Tick “Compare side by side” on any two cities.", "muted"));
    return;
  }
  const [a, b] = picked.map(i => shownCities[i]);
  const table = element("table", null, "compare-table");
  const cap = element("caption", "Side by side · real Qloo affinity scores (0–100), no blended score");
  const head = element("tr"); head.append(element("th", ""), element("th", a.city), element("th", b.city));
  const thead = element("thead"); thead.append(head);
  const body = element("tbody");
  const row = (label, va, vb) => { const tr = element("tr"); const th = element("th", label); th.scope = "row"; tr.append(th, element("td", va), element("td", vb)); body.append(tr); };
  row("Overall taste affinity", `${pct(a.affinity)}`, `${pct(b.affinity)}`);
  const ids = []; for (const d of [...(a.drivers || []), ...(b.drivers || [])]) if (!ids.find(x => x.id === d.id)) ids.push(d);
  const score = (city, id) => { const d = (city.drivers || []).find(x => x.id === id); return d ? `${pct(d.affinity)}` : "–"; };
  for (const d of ids.slice(0, 6)) row(`↳ ${d.taste}`, score(a, d.id), score(b, d.id));
  const sa = signals(a), sb = signals(b);
  const shared = [...sa].filter(x => sb.has(x)), onlyA = [...sa].filter(x => !sb.has(x)), onlyB = [...sb].filter(x => !sa.has(x));
  row("Shared cultural signals", shared.join(", ") || "none", shared.join(", ") || "none");
  row("Unique signals", onlyA.join(", ") || "none", onlyB.join(", ") || "none");
  const hoods = c => (c.neighborhoods || []).length
    ? c.neighborhoods.map(n => `${n.name} (${(n.spots || []).length})`).join(", ")
    : (c.top_spots || []).length ? `No neighborhood labels · ${c.top_spots.length} top spots` : "No local evidence";
  row("Neighborhood evidence (spots)", hoods(a), hoods(b));
  const cov = c => { const v = c.coverage || {}; return `${v.places_relevant ?? 0}/${v.places_returned ?? 0} venues kept · ${v.tastes_scored ?? 0}/${v.tastes_total ?? 0} tastes scored · ${c.radius_km ?? "?"} km radius`; };
  row("Data coverage", cov(a), cov(b));
  table.append(cap, thead, body);
  const wrap = element("div", null, "table-wrap"); wrap.append(table);
  slot.replaceChildren(wrap);
}

function renderResults(result) {
  const fragment = document.createDocumentFragment();
  picked.length = 0;
  if (result.warning) {
    const banner = element("div", null, "message warning-state");
    banner.setAttribute("role", "alert");
    banner.append(element("strong", "Partial results"), element("p", result.warning));
    fragment.append(banner);
  }
  const matched = element("div", null, "matched-panel");
  matched.append(element("h3", "Your taste, connected"));
  const names = (result.matched || []).map(taste => taste.name);
  names.push(...(result.lifestyle_tags || []).map(tag => `#${tag.word || tag.name}`));
  matched.append(chips(names));
  if (result.unmatched?.length) {
    const missing = element("p", null, "unmatched-note");
    missing.append(element("span", "Not found in Qloo: ", "muted"), chips(result.unmatched, "chip chip-missing"));
    matched.append(missing);
  }
  fragment.append(matched);
  if (result.narrative) {
    const narrative = element("section", null, "narrative-card");
    narrative.append(element("h3", "Your relocation story"), element("p", result.narrative));
    fragment.append(narrative);
  }
  const cities = result.cities || [];
  shownCities = cities.slice();
  if (result.blend) {
    const panel = element("section", null, "candidate-panel");
    panel.append(element("h3", `Compromise cities · ${result.blend.a} + ${result.blend.b}`));
    panel.append(element("p", "Each city is scored on each person’s taste separately and ranked by the lower of the two scores, so neither of you gets a city only the other loves."));
    fragment.append(panel);
  }
  const cmp = result.compare;
  if (cmp) {
    const panel = element("section", null, "candidate-panel");
    panel.append(element("h3", "Why not your city?"));
    panel.append(element("p", cmp.error || cmp.verdict));
    fragment.append(panel);
  }
  if (cities.length) {
    const grid = element("div", null, "results-grid");
    cities.forEach((city, index) => grid.append(cityCard(city, index)));
    if (cmp && cmp.city && !cities.some(c => c.city === cmp.city.city)) {
      shownCities.push(cmp.city);
      const card = cityCard(cmp.city, shownCities.length - 1);
      card.classList.add("candidate-card");
      card.querySelector(".eyebrow").textContent = "YOUR PICK";
      grid.append(card);
    }
    fragment.append(grid);
    const compareSection = element("section", null, "comparison-section");
    compareSection.append(element("h3", "Compare two cities"));
    const slot = element("div"); slot.id = "comparison";
    compareSection.append(slot);
    fragment.append(compareSection);
  } else {
    const empty = element("div", null, "empty-state");
    empty.append(element("h3", "No city matches this time"), element("p", "Try different favorites, or remove the country limit to cast a wider net."));
    fragment.append(empty);
  }
  fragment.append(shareBar(result));
  const trace = element("details", null, "trace");
  trace.append(element("summary", "Behind the shortlist · Agent trace"), element("pre", (result.trace || []).join("\n")));
  fragment.append(trace);
  output.replaceChildren(fragment);
  renderComparison();
}

const FIELDS = ["music", "films", "tv", "books", "food", "brands", "lifestyle", "country", "compare"];

function shareUrl(profile) {
  const params = new URLSearchParams();
  for (const name of FIELDS) if (profile[name]) params.set(name, profile[name]);
  return `${location.origin}${location.pathname}?${params}`;
}

let lastProfile = {};
function shareBar(result) {
  const bar = element("div", null, "share-bar");
  const timing = result.timings;
  if (timing) bar.append(element("p", `Scouted in ${(timing.total_ms / 1000).toFixed(1)} s · ${timing.qloo_calls} live Qloo calls · ${timing.cache_hits} cached${timing.qloo_failed ? ` · ${timing.qloo_failed} failed` : ""}`, "muted"));
  const button = element("button", "Copy shareable link", "secondary-button");
  button.type = "button";
  button.addEventListener("click", async () => {
    const url = shareUrl(lastProfile);
    try { await navigator.clipboard.writeText(url); button.textContent = "Link copied ✓"; }
    catch { window.prompt("Copy this link", url); }
  });
  bar.append(button);
  return bar;
}

const LOADING_STEPS = ["Resolving your favorites in Qloo…", "Ranking cities by taste affinity…",
                       "Scoring each taste city by city…", "Finding neighborhoods and local spots…"];
let loadingTimer = null;
function setBusy(value) {
  busy = value;
  loading.hidden = !value;
  output.setAttribute("aria-busy", String(value));
  submit.disabled = value;
  submit.classList.toggle("is-loading", value);
  submit.querySelector(".btn-label").textContent = value ? "Scouting your cities…" : "Find my cities";
  submit.querySelector(".spinner")?.remove();
  if (value) submit.prepend(element("span", null, "spinner"));
  for (const button of presetButtons) button.disabled = value;
  for (const field of form.querySelectorAll("input")) field.disabled = value;
  clearInterval(loadingTimer);
  const step = document.getElementById("loading-step");
  if (value && step) {
    let i = 0;
    step.textContent = LOADING_STEPS[0];
    loadingTimer = setInterval(() => { i = Math.min(i + 1, LOADING_STEPS.length - 1); step.textContent = LOADING_STEPS[i]; }, 1400);
  }
}

function scrollToResults() {
  document.getElementById("shortlist").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
}

for (const button of presetButtons) {
  button.addEventListener("click", () => {
    const preset = presets[Number(button.dataset.preset)];
    for (const field of form.elements) {
      if (!field.name) continue;
      field.value = preset[field.name] || "";
      if (field.value && !reduceMotion) { field.classList.remove("flash"); void field.offsetWidth; field.classList.add("flash"); }
    }
    for (const option of presetButtons) option.setAttribute("aria-pressed", String(option === button));
    document.getElementById("music").focus();
  });
}
form.addEventListener("input", () => {
  for (const button of presetButtons) button.setAttribute("aria-pressed", "false");
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  const profile = Object.fromEntries(new FormData(form));
  if (!Object.entries(profile).some(([name, value]) => name !== "country" && value.trim())) {
    message("Give us a little taste", "Add an artist, film, favorite place or lifestyle keyword to start.", true);
    document.getElementById("music").focus();
    return;
  }
  lastProfile = profile;
  history.replaceState(null, "", shareUrl(profile));
  setBusy(true);
  output.replaceChildren();
  scrollToResults();
  try {
    const request = () => fetch("/api/scout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile)
    });
    let response;
    try { response = await request(); }
    catch { await new Promise(resolve => setTimeout(resolve, 900)); response = await request(); }  // one retry on a dropped connection (scouts are read-only)
    const result = await response.json();
    if (!response.ok || result.error) {
      message("We couldn’t finish this scout", result.error || "Something went wrong. Please try again.", true);
      return;
    }
    renderResults(result);
    scrollToResults();
  } catch {
    message("We couldn’t connect", "Please try again. Your taste profile is still here.", true);
  } finally {
    setBusy(false);
  }
});

// Shareable links: prefill from ?music=...&films=... and run automatically.
(() => {
  const params = new URLSearchParams(location.search);
  let any = false;
  for (const name of FIELDS) {
    const value = params.get(name);
    if (value) { form.elements[name].value = value.slice(0, name === "country" ? 2 : name === "compare" ? 60 : 400); any = true; }
  }
  if (any) form.requestSubmit();
})();

// Conversational agent: shown only when the server has an LLM configured.
const agentSection = document.getElementById("agent");
const agentForm = document.getElementById("agent-form");
const agentInput = document.getElementById("agent-input");
const agentSend = document.getElementById("agent-send");
const agentLog = document.getElementById("agent-log");
const chatHistory = [];
let agentBusy = false;

function profileFromForm() {
  const profile = {};
  for (const name of FIELDS) if (name !== "compare" && form.elements[name].value.trim()) profile[name] = form.elements[name].value.trim();
  return profile;
}

// Agent replies use **bold** for city names; render just that, as text nodes (never HTML).
function richText(text) {
  const p = element("p");
  String(text).split(/\*\*(.+?)\*\*/g).forEach((part, i) => {
    if (part) p.append(i % 2 ? element("strong", part) : document.createTextNode(part));
  });
  return p;
}

function addBubble(role, text, steps) {
  const item = element("li", null, role === "user" ? "from-user" : "from-agent");
  item.append(role === "user" ? element("p", text) : richText(text));
  if (steps?.length) {
    const list = element("ul", null, "agent-steps");
    for (const step of steps) list.append(element("li", step));
    item.append(list);
  }
  agentLog.append(item);
  agentLog.scrollTop = agentLog.scrollHeight;
  return item;
}

async function askAgent(text) {
  if (agentBusy || !text.trim()) return;
  agentBusy = true; agentSend.disabled = true; agentInput.value = "";
  chatHistory.push({ role: "user", content: text.trim().slice(0, 600) });
  addBubble("user", text.trim());
  const pending = element("li", null, "from-agent thinking");
  const dots = element("span", null, "typing");
  dots.setAttribute("aria-label", "The scout is querying Qloo");
  dots.append(element("i"), element("i"), element("i"));
  // Comparing cities takes ~15-30 s of live Qloo calls; say what's happening so it doesn't look stuck.
  const multi = /\b(vs\.?|versus|compare|or|why not|what about|how about)\b/i.test(text) && /\b[A-Z][a-z]/.test(text.slice(1));
  const status = element("span", multi ? "Checking cities on Qloo…" : "Asking Qloo…", "thinking-status");
  pending.append(dots, status);
  const timers = [
    setTimeout(() => { status.textContent = "Scoring on Qloo’s live taste data. This can take 20–30 seconds…"; }, 10000),
    setTimeout(() => { status.textContent = "Almost there, writing up what Qloo found…"; }, 25000),
  ];
  agentLog.append(pending);
  agentLog.scrollTop = agentLog.scrollHeight;
  try {
    const response = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chatHistory.slice(-8), profile: profileFromForm() })
    });
    const body = await response.json();
    pending.remove();
    if (!response.ok || body.error) { addBubble("agent", body.error || "Something went wrong."); chatHistory.pop(); return; }
    chatHistory.push({ role: "assistant", content: String(body.reply).slice(0, 600) });
    addBubble("agent", body.reply, body.steps);
    if (body.profile) {
      for (const name of FIELDS) if (name !== "compare") form.elements[name].value = body.profile[name] || "";
      lastProfile = body.profile;
    }
    if (body.result && !body.result.error) { renderResults(body.result); scrollToResults(); }
  } catch {
    pending.remove();
    chatHistory.pop();
    addBubble("agent", "We couldn’t reach the agent. Please try again.");
  } finally {
    timers.forEach(clearTimeout);
    agentBusy = false; agentSend.disabled = false;
  }
}

agentForm.addEventListener("submit", event => { event.preventDefault(); askAgent(agentInput.value); });
for (const button of document.querySelectorAll("[data-ask]")) button.addEventListener("click", () => askAgent(button.dataset.ask));
fetch("/api/config").then(r => r.json()).then(cfg => { if (cfg.chat) agentSection.hidden = false; }).catch(() => {});

// Empty-state shortcut: load the first example profile and scout it.
output.addEventListener("click", event => {
  const trigger = event.target.closest("[data-action=example]");
  if (!trigger || busy) return;
  presetButtons[0].click();
  form.requestSubmit();
});

// Scroll reveal: sections ease in as they enter the viewport (everything stays visible without JS).
(() => {
  const items = document.querySelectorAll(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) { for (const item of items) item.classList.add("is-visible"); return; }
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) { entry.target.classList.add("is-visible"); observer.unobserve(entry.target); }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
  for (const item of items) observer.observe(item);
  // Safety net for tools that never scroll (full-page screenshots, previews): show everything eventually.
  setTimeout(() => { for (const item of items) item.classList.add("is-visible"); }, 3000);
})();
