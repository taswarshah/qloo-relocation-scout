const presets = [
  { music: "Radiohead, Bon Iver", films: "Amelie, Lost in Translation", food: "Blue Bottle Coffee", lifestyle: "art, cafes, walkable" },
  { music: "Daft Punk, Bad Bunny", tv: "The Bear", food: "Nobu", lifestyle: "nightlife, beaches" },
  { music: "Coldplay, Taylor Swift", films: "Up, Paddington", lifestyle: "hiking, kids, nature", country: "US" }
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
  badge.append(element("strong", `${score}`), element("span", "/ 100"), element("small", "taste affinity"));
  return badge;
}

function localSection(city) {
  const section = element("section", null, "local-section");
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

function cityCard(city, index) {
  const card = element("article", null, "city-card");
  const header = element("div", null, "city-heading");
  const title = element("div");
  title.append(element("p", `MATCH ${String(index + 1).padStart(2, "0")}`, "eyebrow"), element("h3", city.city || city.location));
  title.append(element("p", city.location, "muted"));
  header.append(title, scoreBadge(city.affinity));
  card.append(header, element("p", city.why, "city-explanation"));
  if (city.drivers?.length) {
    const drivers = element("div", null, "taste-drivers");
    drivers.append(element("h4", "Your strongest connections"));
    const bars = element("ul", null, "driver-bars");
    for (const driver of city.drivers) {
      const score = Math.round(Math.min(1, Math.max(0, Number(driver.affinity) || 0)) * 100);
      const row = element("li");
      const meter = element("meter");
      meter.min = 0; meter.max = 100; meter.value = score;
      meter.setAttribute("aria-label", `${driver.taste} affinity ${score} of 100`);
      row.append(element("span", driver.taste, "driver-name"), meter, element("span", String(score), "driver-score"));
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

function renderResults(result) {
  const fragment = document.createDocumentFragment();
  const matched = element("div", null, "matched-panel");
  matched.append(element("h3", "Your taste, connected"));
  const names = (result.matched || []).map(taste => taste.name);
  names.push(...(result.lifestyle_tags || []).map(tag => `#${tag.name}`));
  matched.append(chips(names));
  fragment.append(matched);
  if (result.narrative) {
    const narrative = element("section", null, "narrative-card");
    narrative.append(element("h3", "Your relocation story"), element("p", result.narrative));
    fragment.append(narrative);
  }
  const cities = result.cities || [];
  if (cities.length) {
    const grid = element("div", null, "results-grid");
    cities.forEach((city, index) => grid.append(cityCard(city, index)));
    fragment.append(grid);
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
}

const FIELDS = ["music", "films", "tv", "books", "food", "brands", "lifestyle", "country"];

function shareUrl(profile) {
  const params = new URLSearchParams();
  for (const name of FIELDS) if (profile[name]) params.set(name, profile[name]);
  return `${location.origin}${location.pathname}?${params}`;
}

let lastProfile = {};
function shareBar(result) {
  const bar = element("div", null, "share-bar");
  const timing = result.timings;
  if (timing) bar.append(element("p", `Scouted in ${(timing.total_ms / 1000).toFixed(1)} s · ${timing.qloo_calls} live Qloo calls · ${timing.cache_hits} cached`, "muted"));
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

function setBusy(value) {
  busy = value;
  loading.hidden = !value;
  output.setAttribute("aria-busy", String(value));
  submit.disabled = value;
  submit.textContent = value ? "Scouting your cities…" : "Find my cities ↗";
  for (const button of presetButtons) button.disabled = value;
  for (const field of form.querySelectorAll("input")) field.disabled = value;
}

for (const button of presetButtons) {
  button.addEventListener("click", () => {
    const preset = presets[Number(button.dataset.preset)];
    for (const field of form.elements) if (field.name) field.value = preset[field.name] || "";
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
  try {
    const response = await fetch("/api/scout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile)
    });
    const result = await response.json();
    if (!response.ok || result.error) {
      message("We couldn’t finish this scout", result.error || "Something went wrong. Please try again.", true);
      return;
    }
    renderResults(result);
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
    if (value) { form.elements[name].value = value.slice(0, name === "country" ? 2 : 400); any = true; }
  }
  if (any) form.requestSubmit();
})();
