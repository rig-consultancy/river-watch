// River Watch dashboard. Reads data/index.json and data/<river>.json written by the pipeline.

const BINS = [5, 15, 30, 50]; // hyacinth % thresholds; below the first is "clear"
const GAP_DAYS = 21; // longer gaps between readings break the chart line (clear dates are 3-10 days apart)
const tip = document.getElementById("tip");
const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
// Local YYYY-MM-DD for a timestamp (toISOString would shift to UTC and change the day in India).
const isoLocal = (t) => {
  const d = new Date(t);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const dayMs = 864e5;
const toT = (d) => new Date(d + "T00:00:00").getTime();

let map, segLayer, baseLayers = {}, current, riverIndex, idx = 0, playTimer = null, listMode = "worst", selected = null;
const state = { lang: "en", region: "all" };

// ---------- language ----------
const locale = () => (state.lang === "mr" ? "mr-IN" : "en-IN");
function t(key, vars = {}) {
  const s = STRINGS[state.lang][key] ?? STRINGS.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
}
const nf = (v, d = 1) => (v == null ? "–" : new Intl.NumberFormat(locale(), { maximumFractionDigits: d }).format(v));
const fmtDate = (d) => new Date(d + "T00:00:00").toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric" });
const fmtShort = (d) => new Date(d + "T00:00:00").toLocaleDateString(locale(), { month: "short", year: "2-digit" });

function applyStaticText() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll("[data-lang]").forEach((b) => b.classList.toggle("on", b.dataset.lang === state.lang));
  document.title = t("title");
}

// ---------- names ----------
function segName(p) {
  const place = state.lang === "mr" ? p.place_mr || p.place : p.place;
  if (place) return t("near", { place });
  const n = p.nearest;
  if (n) return t("from_place", { km: nf(n.km), place: state.lang === "mr" ? n.name_mr || n.name : n.name });
  return t("stretch", { n: nf(p.id + 1, 0) });
}
function regionName(r) {
  if (!r) return "";
  return state.lang === "mr" ? r.name_mr || r.name : r.name;
}
function segAreaLabel(p) {
  if (p.regions.length > 1) return t("pmc_pcmc_border");
  if (!p.regions.length) return t("outside");
  return regionName(current.regions.find((r) => r.id === p.regions[0]));
}
const inRegion = (p) => state.region === "all" || (state.region === "other" ? !p.regions.length : p.regions.includes(state.region));
const selectedRegion = () => current.regions.find((r) => r.id === state.region);

function binColor(pct) {
  if (pct == null || pct < BINS[0]) return css("--clear");
  if (pct < BINS[1]) return css("--ramp-1");
  if (pct < BINS[2]) return css("--ramp-2");
  if (pct < BINS[3]) return css("--ramp-3");
  return css("--ramp-4");
}

// ---------- tooltip ----------
function showTip(html, x, y) {
  tip.innerHTML = html;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  tip.style.left = Math.max(8, left) + "px";
  tip.style.top = Math.max(8, top) + "px";
}
const hideTip = () => (tip.hidden = true);

// ---------- map ----------
function initMap() {
  map = L.map("map", { zoomControl: true, attributionControl: true, scrollWheelZoom: false });
  baseLayers.map = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, className: "osm-tiles", attribution: "© OpenStreetMap contributors" });
  // Satellite: the Sentinel-2 image of the date on the slider (Copernicus data, free for any use),
  // so the map shows exactly the picture the numbers came from. Its URL follows the slider.
  baseLayers.sat = L.tileLayer("", { maxZoom: 19, maxNativeZoom: 15, attribution: "" });
  baseLayers.map.addTo(map);

  document.querySelectorAll("[data-base]").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("[data-base]").forEach((o) => o.classList.toggle("on", o === b));
    Object.values(baseLayers).forEach((l) => map.removeLayer(l));
    baseLayers[b.dataset.base].addTo(map);
  }));
}

function drawLegend() {
  const items = [[css("--clear"), t("legend_under", { n: nf(BINS[0], 0) })]]
    .concat(BINS.map((b, i) => [css(`--ramp-${i + 1}`),
      i < BINS.length - 1 ? `${nf(b, 0)}–${nf(BINS[i + 1], 0)}%` : `${nf(b, 0)}%+`]));
  $("legend").innerHTML = `<span>${t("legend")}</span>` +
    items.map(([c, s]) => `<span><i class="sw" style="background:${c}"></i>${s}</span>`).join("") +
    `<span><i class="sw cloud"></i>${t("legend_cloud")}</span>` +
    `<span><i class="sw lowconf"></i>${t("legend_lowconf")}</span>` +
    `<span><i class="sw none"></i>${t("legend_none")}</span>` +
    `<span><i class="sw green"></i>${t("legend_green")}</span>` +
    `<span><i class="sw rooted"></i>${t("legend_rooted")}</span>` +
    (state.region !== "all" ? `<span><i class="sw other"></i>${t("legend_other_area")}</span>` : "");
}

// Value of a segment on the date at slider position idx (null: clouds that day).
const valueAt = (id) => current.segment_history.hyacinth_pct[idx][id];

function segStyle(f) {
  const p = f.properties, v = valueAt(p.id), mine = inRegion(p);
  // Flood test says "rooted": almost certainly not hyacinth, so it recedes; otherwise it stands out.
  if (greenChannel(p) && p.flood_test?.verdict === "rooted")
    return { color: css("--clear"), weight: 4, opacity: mine ? 0.7 : 0.2, dashArray: "6 5", lineCap: "butt" };
  if (greenChannel(p)) return { color: css("--series-3"), weight: 5, opacity: mine ? 0.9 : 0.2, dashArray: "6 5", lineCap: "butt" };
  if (!p.measurable) return { color: css("--clear"), weight: 2, opacity: mine ? 0.6 : 0.2, dashArray: "1 5", lineCap: "round" };
  if (!mine) return { color: css("--clear"), weight: 2, opacity: 0.25, dashArray: null, lineCap: "round" };
  if (v == null) return { color: css("--clear"), weight: 3, opacity: 0.8, dashArray: "2 6", lineCap: "round" };
  const low = p.confidence === "low"; // still shown, but faded so it doesn't read as a firm finding
  return { color: binColor(v), weight: v >= BINS[0] && !low ? 6 : 3, opacity: low ? 0.45 : 0.95, dashArray: null, lineCap: "round" };
}

// Much of the channel green on nearly every clear image: weed or riverbed plants, can't tell from space.
const greenChannel = (p) => (p.measurable ? (p.confidence_why || []).includes("green_channel") : p.why === "green_channel");

// Sharp, lasting drops detected on this stretch, newest first, with their likely cause.
function cleanupFact(p) {
  const ev = (current.cleanup_events || []).filter((e) => e.segment === p.id).sort((a, b) => (a.to < b.to ? 1 : -1));
  if (!ev.length) return "";
  const rows = ev.slice(0, 5).map((e) => `<span class="conf-why">${fmtDate(e.to)}: ${nf(e.before)}% → ${nf(e.after)}% · ${t(`cause_${e.cause}`)}` +
    (e.regrowth_days != null ? ` · ${t("r_regrew", { n: nf(e.regrowth_days, 0) })}` : "") + "</span>").join("");
  return `<div><dt>${t("f_drops", { n: nf(ev.length, 0) })}</dt><dd>${rows}</dd></div>`;
}

// Flood test for a stretch's green-all-year area: verdict, plus the areas behind it.
function floodFact(p) {
  const f = p.flood_test;
  if (!f) return "";
  return `<div><dt>${t("f_flood")}</dt><dd><span class="conf conf-${f.verdict === "rooted" ? "medium" : "low"}">${t(`flood_${f.verdict}`)}</span>` +
    `<span class="conf-why">${t(`flood_why_${f.verdict}`)} ${t("flood_areas", { s: nf(f.survived_ha), c: nf(f.cleared_ha), u: nf(f.unclear_ha) })}</span></dd></div>`;
}

// Confidence badge; with reasons it explains itself in a tooltip.
function confBadge(p, long) {
  if (!p.confidence) return "";
  const why = (p.confidence_why || []).map((r) => t(`why_${r}`)).join("; ");
  return `<span class="conf conf-${p.confidence}" title="${why}">${t(`conf_${p.confidence}`)}</span>` +
    (long && why ? `<span class="conf-why">${why}</span>` : "");
}

// The Sentinel-2 photo behind a date, cropped around a stretch (Planetary Computer's free tiler).
const sceneOn = (d) => current.series.find((s) => s.date === d)?.scene;
function photoUrl(scene, f, padDeg = 0.006) {
  const xs = f.geometry.coordinates.map((c) => c[0]), ys = f.geometry.coordinates.map((c) => c[1]);
  const bb = [Math.min(...xs) - padDeg, Math.min(...ys) - padDeg, Math.max(...xs) + padDeg, Math.max(...ys) + padDeg]
    .map((v) => v.toFixed(4)).join(",");
  return `https://planetarycomputer.microsoft.com/api/data/v1/item/bbox/${bb}.png?collection=sentinel-2-l2a` +
    `&item=${scene}&assets=visual&asset_bidx=visual%7C1%2C2%2C3&nodata=0&max_size=512&color_formula=Gamma%20RGB%201.6%20Saturation%201.2`;
}
// ESA's own viewer at this place and date, as an independent second look.
function esaUrl(f, d) {
  const c = f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)];
  return `https://browser.dataspace.copernicus.eu/?zoom=15&lat=${c[1]}&lng=${c[0]}&datasetId=S2_L2A_CDAS` +
    `&fromTime=${d}T00%3A00%3A00.000Z&toTime=${d}T23%3A59%3A59.999Z&layerId=1_TRUE_COLOR`;
}
const riverFeature = () => ({ geometry: { coordinates: current.segments.features.flatMap((f) => f.geometry.coordinates) } });

function segTip(p) {
  if (!p.measurable) return `<div class="tt-title">${segName(p)} · ${segAreaLabel(p)}</div><div>${t(`why_${p.why}`)}</div>` +
    (p.flood_test ? `<div class="tt-sub">${t(`tt_flood_${p.flood_test.verdict}`)}</div>` : "");
  const gc = greenChannel(p) ? `<div class="tt-sub">${t("tt_green_channel", { ha: nf(p.green_channel_ha) })}` +
    (p.flood_test ? ` ${t(`tt_flood_${p.flood_test.verdict}`)}` : "") + "</div>" : "";
  const v = valueAt(p.id), d = current.segment_history.dates[idx];
  const val = v == null ? t("legend_cloud") : `<span class="tt-val">${nf(v)}%</span>`;
  return `<div class="tt-title">${segName(p)} · ${segAreaLabel(p)}</div><div>${val} ${confBadge(p)}</div>${gc}${p.unconfirmed && idx === current.segment_history.dates.length - 1
    ? `<div class="tt-sub">${t("unconfirmed")}</div>` : ""}<div class="tt-sub">${fmtDate(d)}</div>`;
}

function drawSegments(fit) {
  if (segLayer) map.removeLayer(segLayer);
  segLayer = L.geoJSON(current.segments, {
    style: segStyle,
    onEachFeature: (f, layer) => {
      layer.on("mousemove", (e) => showTip(segTip(f.properties), e.originalEvent.clientX, e.originalEvent.clientY));
      layer.on("mouseout", hideTip);
      layer.on("click", () => { hideTip(); selectSegment(f.properties.id, false); });
    },
  }).addTo(map);
  if (fit) {
    map.invalidateSize();
    const mine = current.segments.features.filter((f) => inRegion(f.properties));
    map.fitBounds(L.geoJSON({ type: "FeatureCollection", features: mine.length ? mine : current.segments.features }).getBounds(),
      { padding: [16, 16] });
  }
}

// ---------- date slider ----------
function initSlider() {
  const s = $("slider");
  s.addEventListener("input", () => { stopPlay(); setIdx(+s.value); });
  $("to-latest").addEventListener("click", () => { stopPlay(); setIdx(current.segment_history.dates.length - 1); });
  $("play").addEventListener("click", () => (playTimer ? stopPlay() : startPlay()));
}
function setIdx(i) {
  idx = i;
  $("slider").value = i;
  $("slider-date").textContent = t("slider_label", { date: fmtDate(current.segment_history.dates[i]) });
  segLayer.setStyle(segStyle);
  syncSatellite();
}

function syncSatellite() {
  const d = current.segment_history.dates[idx], scene = sceneOn(d), sat = baseLayers.sat;
  if (!scene) return;
  sat.setUrl(`https://planetarycomputer.microsoft.com/api/data/v1/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x` +
    `?collection=sentinel-2-l2a&item=${scene}&assets=visual&asset_bidx=visual%7C1%2C2%2C3&nodata=0` +
    `&color_formula=Gamma%20RGB%201.6%20Saturation%201.2`);
  const attr = `Contains modified Copernicus Sentinel data (${d}), via Microsoft Planetary Computer`;
  if (map.hasLayer(sat)) { map.attributionControl.removeAttribution(sat.options.attribution); map.attributionControl.addAttribution(attr); }
  sat.options.attribution = attr;
}
function startPlay() {
  const n = current.segment_history.dates.length;
  if (idx >= n - 1) setIdx(0);
  $("play").textContent = t("pause");
  playTimer = setInterval(() => (idx >= n - 1 ? stopPlay() : setIdx(idx + 1)), 700);
}
function stopPlay() {
  clearInterval(playTimer);
  playTimer = null;
  $("play").textContent = t("play");
}

// ---------- stretch lists ----------
function drawList() {
  const list = $("hotspots");
  // Rankings use only stretches we can stand behind; low-confidence ones stay on the map, faded.
  const feats = current.segments.features.map((f) => f.properties)
    .filter((p) => inRegion(p) && p.measurable && p.confidence !== "low" && !p.unconfirmed);
  const waiting = current.segments.features.filter((f) => inRegion(f.properties) && f.properties.unconfirmed).length;
  const look = current.outlook;
  $("list-hint").textContent = (listMode === "risk" && look?.alert_pct
    ? t("hint_risk", { a: nf(look.alert_pct, 0) }) : t("hint_list")) +
    (waiting ? " " + t("hint_unconfirmed", { n: nf(waiting, 0) }) : "");
  let rows;
  if (listMode === "risk") {
    rows = feats.filter((p) => p.outlook).sort((a, b) => b.outlook.p_alert - a.outlook.p_alert || b.outlook.pct - a.outlook.pct)
      .slice(0, 15).map((p) => ({ p, val: t("risk_item", { p: nf(p.outlook.p_alert * 100, 0) }), bar: p.outlook.p_alert * 100, color: binColor(p.outlook.pct) }));
    if (!rows.length) { list.innerHTML = `<li class="hint">${t("none_risk")}</li>`; return; }
  } else {
    rows = feats.filter((p) => p.hyacinth_pct >= BINS[0]).sort((a, b) => b.hyacinth_pct - a.hyacinth_pct)
      .slice(0, 15).map((p) => ({ p, val: `${nf(p.hyacinth_pct)}%`, bar: p.hyacinth_pct, color: binColor(p.hyacinth_pct) }));
    if (!rows.length) { list.innerHTML = `<li class="hint">${t("none_above", { n: nf(BINS[0], 0) })}</li>`; return; }
  }
  list.innerHTML = "";
  rows.forEach(({ p, val, bar, color }, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<button type="button"><span class="rank">${nf(i + 1, 0)}</span>
      <span class="hs-name">${segName(p)}<small>${segAreaLabel(p)} ${confBadge(p)}</small></span><span class="hs-val">${val}</span>
      <span class="hs-bar"><span style="width:${Math.min(100, bar)}%;background:${color}"></span></span></button>`;
    li.querySelector("button").addEventListener("click", () => selectSegment(p.id, true));
    list.appendChild(li);
  });
}

// ---------- stretch detail ----------
function selectSegment(id, zoom) {
  const f = current.segments.features.find((x) => x.properties.id === id);
  if (!f) return;
  selected = id;
  syncUrl();
  if (zoom) map.fitBounds(L.geoJSON(f).getBounds(), { maxZoom: 16, padding: [40, 40] });
  drawDetail();
  $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function drawDetail() {
  const box = $("detail");
  const f = selected == null ? null : current.segments.features.find((x) => x.properties.id === selected);
  if (!f) { box.hidden = true; return; }
  const p = f.properties, look = current.outlook;
  box.hidden = false;
  $("d-title").textContent = segName(p);
  if (!p.measurable) {
    $("d-facts").innerHTML = `<div><dt>${t("f_area")}</dt><dd>${segAreaLabel(p)}</dd></div>` +
      (p.green_channel_ha ? `<div><dt>${t("f_green_channel")}</dt><dd>${t("ha_val", { ha: nf(p.green_channel_ha) })}</dd></div>` : "") +
      floodFact(p) +
      `<p class="hint">${t(`why_${p.why}`)}</p>`;
    $("c-seg").innerHTML = "";
    $("d-photo").innerHTML = "";
    return;
  }
  const fact = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  const change = p.change_pct == null ? "–" : `${p.change_pct > 0 ? "+" : ""}${t("pts", { v: nf(p.change_pct) })}`;
  $("d-facts").innerHTML = [
    fact(t("f_area"), segAreaLabel(p)),
    p.wards.length ? fact(t("f_ward"), p.wards.join(" / ")) : "",
    fact(t("f_now"), `<strong>${nf(p.hyacinth_pct)}%</strong>` +
      (p.range ? ` <span class="muted">${t("likely", { lo: nf(p.range[0]), hi: nf(p.range[1]) })}</span>` : "") +
      (p.unconfirmed ? `<span class="conf-why">${t("unconfirmed")}</span>` : "")),
    fact(t("f_seen"), fmtDate(p.date)),
    fact(t("f_change"), change),
    p.outlook ? fact(t("f_outlook"), `~${nf(p.outlook.pct)}%`) : "",
    p.outlook && look?.alert_pct ? fact(t("f_chance", { a: nf(look.alert_pct, 0) }), `${nf(p.outlook.p_alert * 100, 0)}%`) : "",
    fact(t("f_km"), t("km_val", { km: nf(p.km) })),
    fact(t("f_conf"), confBadge(p, true)),
    p.area_ha != null ? fact(t("f_area_ha"), t("ha_val", { ha: nf(p.area_ha) })) : "",
    p.green_channel_ha ? fact(t("f_green_channel"), t("ha_val", { ha: nf(p.green_channel_ha) })) : "",
    floodFact(p),
    cleanupFact(p),
  ].join("");
  // Photo: the slider's date if this stretch was clear then, else its last clear date.
  const sd = current.segment_history.dates[idx], d = valueAt(p.id) != null ? sd : p.date, scene = sceneOn(d);
  $("d-photo").innerHTML = scene ? `<figure>
      <img src="${photoUrl(scene, f)}" alt="${t("photo_title", { date: fmtDate(d) })}" loading="lazy">
      <figcaption>${t("photo_title", { date: fmtDate(d) })} · ${t("photo_note")}
        <a href="${esaUrl(f, d)}" target="_blank" rel="noopener">${t("open_esa")}</a></figcaption></figure>` : "";
  const sh = current.segment_history;
  const jumps = new Set((sh.one_off || []).filter(([, k]) => k === p.id).map(([i]) => i));
  const rows = sh.dates.map((d, i) => ({ date: d, v: sh.hyacinth_pct[i][p.id], jump: jumps.has(i) })).filter((r) => r.v != null);
  const layers = [{ name: t("l_optical"), cls: "opt", rows: rows.filter((r) => !r.jump), key: "v" }];
  if (jumps.size) layers.push({ name: t("l_jumps"), cls: "jump", rows: rows.filter((r) => r.jump), key: "v", dotsOnly: true });
  lineChart($("c-seg"), layers, t("f_now"));
}

// ---------- line chart ----------
// layers: [{name, cls, rows, key, tip, gap}] sharing one % axis; the first layer is the primary
// series (area fill). A legend is shown when there is more than one layer. gap: days between
// readings that break the line (default GAP_DAYS).
function lineChart(el, layers, label) {
  // Keep missing readings (null) as markers so the line breaks there instead of bridging them.
  const toAll = (l) => l.rows.map((s) => ({ t: toT(s.date), v: s[l.key], d: s }));
  const L = layers.map((l) => {
    const all = toAll(l);
    return { ...l, all, pts: all.filter((p) => p.v != null) };
  }).filter((l) => l.pts.length);
  const legend = L.length > 1
    ? `<div class="chart-legend">${L.map((l) => `<span><i class="key ${l.cls}"></i>${l.name}</span>`).join("")}</div>` : "";
  const W = el.clientWidth, H = el.clientHeight - (legend ? 24 : 0);
  const m = { l: 40, r: 12, t: 10, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  if (iw <= 0 || ih <= 0) return; // not laid out yet (hidden tab); the resize handler redraws
  if (!L.length || L[0].pts.length < 2) {
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}"><text class="empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">${t("chart_empty")}</text></svg>`;
    return;
  }
  const all = L.flatMap((l) => l.pts);
  const t0 = Math.min(...all.map((p) => p.t)), t1 = Math.max(...all.map((p) => p.t));
  const bandMax = L.flatMap((l) => (l.band ? l.pts.map((p) => p.d[l.band]?.[1] ?? p.v) : []));
  const vmax = niceMax(Math.max(...all.map((p) => p.v), ...bandMax));
  const x = (tt) => m.l + ((tt - t0) / (t1 - t0)) * iw;
  const y = (v) => m.t + ih - (v / vmax) * ih;

  const ticksY = [0, vmax / 2, vmax];
  const ticksX = monthTicks(t0, t1, Math.max(2, Math.floor(iw / 70)));
  const seg = (run) => run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const marks = L.map((l, li) => {
    // Break the line at missing readings and long gaps (monsoon clouds) instead of drawing a
    // slope between readings weeks apart that was never observed.
    const runs = [[]];
    let prev = null;
    l.all.forEach((p) => {
      const cur = runs[runs.length - 1];
      if (p.v == null) { if (cur.length) runs.push([]); prev = null; return; }
      if (prev && p.t - prev.t > (l.gap ?? GAP_DAYS) * dayMs && cur.length) runs.push([]);
      runs[runs.length - 1].push(p);
      prev = p;
    });
    if (!runs[runs.length - 1].length) runs.pop();
    // Likely range as a shaded band (instead of the plain area fill) when the layer has one.
    const band = !l.band ? "" : `<path class="band" d="${runs.filter((r) => r.length > 1).map((r) =>
      r.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.d[l.band][1]).toFixed(1)}`).join("") +
      [...r].reverse().map((p) => `L${x(p.t).toFixed(1)},${y(p.d[l.band][0]).toFixed(1)}`).join("") + "Z").join("")}"/>`;
    const area = li || l.band ? "" : `<path class="area" d="${runs.filter((r) => r.length > 1)
      .map((r) => `${seg(r)}L${x(r[r.length - 1].t)},${y(0)}L${x(r[0].t)},${y(0)}Z`).join("")}"/>`;
    // Primary: dots only where a point stands alone. Radar and dots-only layers: every point.
    const dots = l.cls === "rad" || l.dotsOnly ? l.pts : runs.filter((r) => r.length === 1).map((r) => r[0]);
    const last = l.pts[l.pts.length - 1];
    if (l.dotsOnly) return dots.map((p) => `<circle class="dot ${l.cls}" cx="${x(p.t)}" cy="${y(p.v)}" r="4"/>`).join("");
    return `${band}${area}<path class="line ${l.cls}" d="${runs.map(seg).join("")}"/>
      ${dots.map((p) => `<circle class="dot ${l.cls}" cx="${x(p.t)}" cy="${y(p.v)}" r="${li ? 3 : 4}"/>`).join("")}
      ${li ? `<circle class="dot ${l.cls}" cx="${x(last.t)}" cy="${y(last.v)}" r="4"/>` : ""}`;
  }).join("");

  el.innerHTML = `${legend}<svg viewBox="0 0 ${W} ${H}" style="height:${H}px" role="img" aria-label="${label}">
    <g class="axis">${ticksY.map((v) => `<line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>
      <text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${nf(v)}%</text>`).join("")}
      ${ticksX.map((tt) => `<text x="${x(tt)}" y="${H - 6}" text-anchor="middle">${fmtShort(isoLocal(tt))}</text>`).join("")}
    </g>
    <line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}"/>
    ${marks}
    <g class="hover" style="display:none"><line class="cross" y1="${m.t}" y2="${m.t + ih}"/>
      ${L.map((l) => `<circle class="dot ${l.cls}" r="5"/>`).join("")}</g>
    <rect x="${m.l}" y="0" width="${iw}" height="${H}" fill="transparent"/>
  </svg>`;

  const svg = el.querySelector("svg"), hover = svg.querySelector(".hover");
  const rect = svg.querySelector("rect"), rings = hover.querySelectorAll("circle");
  const nearest = (pts, tt) => pts.reduce((a, p) => (Math.abs(p.t - tt) < Math.abs(a.t - tt) ? p : a));
  const move = (ev) => {
    const b = svg.getBoundingClientRect();
    const tt = t0 + ((((ev.clientX - b.left) / b.width) * W - m.l) / iw) * (t1 - t0);
    const at = nearest(all, tt).t;
    hover.style.display = "";
    hover.querySelector("line").setAttribute("x1", x(at));
    hover.querySelector("line").setAttribute("x2", x(at));
    const rowsHtml = [];
    L.forEach((l, i) => {
      const p = nearest(l.pts, at);
      const show = Math.abs(p.t - at) <= 6 * dayMs;
      rings[i].style.display = show ? "" : "none";
      if (!show) return;
      rings[i].setAttribute("cx", x(p.t));
      rings[i].setAttribute("cy", y(p.v));
      const name = L.length > 1 ? `<i class="key ${l.cls}"></i>${l.name}` : label;
      const when = p.t !== at ? ` (${fmtDate(p.d.date)})` : "";
      const rng = l.band && p.d[l.band] ? ` <span class="tt-sub">${t("likely", { lo: nf(p.d[l.band][0]), hi: nf(p.d[l.band][1]) })}</span>` : "";
      rowsHtml.push(`<div>${name}: <span class="tt-val">${nf(p.v)}%</span>${rng}${when}</div>${l.tip ? l.tip(p.d) : ""}`);
    });
    showTip(`<div class="tt-title">${fmtDate(isoLocal(at))}</div>${rowsHtml.join("")}`, ev.clientX, ev.clientY);
  };
  rect.addEventListener("pointermove", move);
  rect.addEventListener("pointerdown", move);
  rect.addEventListener("pointerleave", () => { hover.style.display = "none"; hideTip(); });
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const s of [1, 2, 2.5, 4, 5, 6, 8, 10]) if (s * p >= v) return s * p;
  return 10 * p;
}

function monthTicks(t0, t1, maxTicks) {
  const out = [];
  const d = new Date(t0); d.setDate(1); d.setMonth(d.getMonth() + 1);
  while (d.getTime() <= t1) { out.push(d.getTime()); d.setMonth(d.getMonth() + 1); }
  const step = Math.ceil(out.length / maxTicks) || 1;
  return out.filter((_, i) => i % step === 0);
}

// ---------- tiles ----------
// Observation in `rows` closest to `target` days before the last one, within +-`tol` days.
function closestTo(rows, tms, tol) {
  let best = null;
  rows.forEach((r) => {
    const dd = Math.abs(toT(r.date) - tms);
    if (dd <= tol * dayMs && (!best || dd < best.dd)) best = { r, dd };
  });
  return best?.r;
}

function setDelta(el, d) {
  el.textContent = d == null ? "–" : `${d > 0 ? "▲ +" : d < 0 ? "▼ " : ""}${nf(d)}`;
  el.className = "tile-value " + (d > 0 ? "delta-up" : d < 0 ? "delta-down" : "");
}

function drawTiles() {
  const s = current.series, last = s[s.length - 1];
  const reg = selectedRegion();
  const rows = reg ? reg.series : s;
  const now = rows[rows.length - 1];
  $("t-pct").textContent = now ? `${nf(now.hyacinth_pct)}%` : "–";
  const radarLast = current.radar?.series.at(-1);
  $("t-pct-note").textContent = reg ? t("n_pct_region", { area: regionName(reg) })
    : radarLast && radarLast.date > last.date
      ? t("n_pct_radar", { date: fmtDate(last.date), rdate: fmtDate(radarLast.date), rpct: nf(radarLast.hyacinth_pct) })
      : now?.range ? t("n_pct_range", { lo: nf(now.range[0]), hi: nf(now.range[1]) }) : t("n_pct");
  // Area covered: for a selected area, the latest reading of each of its stretches.
  const mine = current.segments.features.map((f) => f.properties).filter((p) => p.measurable && inRegion(p));
  const regKm2 = mine.reduce((s, p) => s + (p.hyacinth_pct / 100) * p.area_ha, 0) / 100;
  $("t-area").textContent = nf(reg ? regKm2 : last.hyacinth_km2, 2);
  $("t-area-note").textContent = reg ? t("n_area_region", { area: regionName(reg) }) : t("n_area");

  // Same time last year (closest clear date within 3 weeks of a year before).
  const yr = now && closestTo(rows, toT(now.date) - 365 * dayMs, 21);
  setDelta($("t-year"), yr ? +(now.hyacinth_pct - yr.hyacinth_pct).toFixed(1) : null);
  $("t-year-note").textContent = yr ? t("n_year", { pct: nf(yr.hyacinth_pct), date: fmtDate(yr.date) }) : t("n_year_none");

  // ~30 days: the latest observation 30-60 days earlier.
  const cutoff = now ? toT(now.date) - 30 * dayMs : 0;
  const ref = now && [...rows].reverse().find((p) => toT(p.date) <= cutoff && toT(p.date) >= cutoff - 30 * dayMs);
  setDelta($("t-change"), ref ? +(now.hyacinth_pct - ref.hyacinth_pct).toFixed(1) : null);
  $("t-change-note").textContent = ref ? t("n_change", { date: fmtDate(ref.date) }) : t("n_change_none");

  const look = current.outlook;
  const lo = look?.river;
  // For a selected area: its stretches' outlooks, weighted by their river area.
  const withLook = mine.filter((p) => p.outlook);
  const w = withLook.reduce((s, p) => s + p.area_ha, 0);
  const regLook = reg && w ? withLook.reduce((s, p) => s + p.outlook.pct * p.area_ha, 0) / w : null;
  $("t-outlook").textContent = reg ? (regLook == null ? "–" : `~${nf(regLook)}%`) : lo ? `~${nf(lo.pct)}%` : "–";
  $("t-outlook").className = "tile-value";
  $("t-outlook-note").textContent = !look ? t("n_outlook_none")
    : look.monsoon ? t("n_outlook_monsoon", { date: fmtDate(look.target_date) })
      : !lo ? t("n_outlook_none")
        : reg ? t("n_outlook_area", { area: regionName(reg), date: fmtDate(look.target_date), e: nf(look.mae_segment) })
          : t("n_outlook", { lo: nf(lo.low), hi: nf(lo.high), date: fmtDate(look.target_date) });

  // Muddy water: the selected area's own figure on its latest date, else the whole river.
  const mud = reg ? now?.turbid_pct : last.turbid_pct;
  $("t-turbid").textContent = mud == null ? "–" : `${nf(mud)}%`;
  $("t-turbid-note").textContent = mud == null ? t("n_turbid_none")
    : reg ? t("n_turbid_area", { area: regionName(reg) }) : t("n_turbid", { w: nf(last.open_water_pct, 0) });
}

// ---------- regions table ----------
function drawRegions() {
  const feats = current.segments.features.map((f) => f.properties);
  $("regions").querySelector("tbody").innerHTML = current.regions.map((r) => {
    const rows = r.series, now = rows[rows.length - 1];
    const yr = now && closestTo(rows, toT(now.date) - 365 * dayMs, 21);
    const mine = feats.filter((p) => p.measurable && p.confidence !== "low" && !p.unconfirmed && (r.id === "other" ? !p.regions.length : p.regions.includes(r.id)));
    const worst = mine.reduce((a, p) => (!a || p.hyacinth_pct > a.hyacinth_pct ? p : a), null);
    const name = r.id === "other" ? t("outside") : regionName(r);
    return `<tr class="${state.region === r.id ? "on" : ""}" data-region="${r.id}">
      <td><button type="button" class="linkish">${name}</button></td>
      <td>${t("km_val", { km: nf(r.km) })}</td>
      <td><strong>${now ? nf(now.hyacinth_pct) + "%" : "–"}</strong></td>
      <td>${yr ? nf(yr.hyacinth_pct) + "%" : "–"}</td>
      <td>${worst ? `<button type="button" class="linkish" data-seg="${worst.id}">${segName(worst)}</button> ${nf(worst.hyacinth_pct)}%` : "–"}</td></tr>`;
  }).join("");
  $("regions").querySelectorAll("tr[data-region] td:first-child button").forEach((b) =>
    b.addEventListener("click", () => setRegion(b.closest("tr").dataset.region)));
  $("regions").querySelectorAll("button[data-seg]").forEach((b) =>
    b.addEventListener("click", () => selectSegment(+b.dataset.seg, true)));
}

// ---------- charts + table ----------
function drawCharts() {
  if (!current) return;
  const optical = (key) => ({ name: t("l_optical"), cls: "opt", rows: current.series, key,
    tip: (s) => `<div class="tt-sub">${t("tt_cloudfree", { p: nf(s.valid_fraction * 100, 0) })}</div>` });
  const hy = [{ ...optical("hyacinth_pct"), band: "range" }];
  const reg = selectedRegion();
  if (reg) {
    hy[0].name = t("l_whole");
    hy.push({ name: t("l_region", { area: reg.id === "other" ? t("outside") : regionName(reg) }), cls: "reg", rows: reg.series, key: "hyacinth_pct" });
  }
  if (current.radar) {
    hy.push({ name: t("l_radar", { e: nf(current.radar.mae_pct) }), cls: "rad", rows: current.radar.series, key: "hyacinth_pct", gap: 30 });
  }
  lineChart($("c-hy"), hy, t("c_hy"));
  const tu = [optical("turbid_pct")];
  if (reg) {
    tu[0].name = t("l_whole");
    tu.push({ name: t("l_region", { area: reg.id === "other" ? t("outside") : regionName(reg) }), cls: "reg", rows: reg.series, key: "turbid_pct" });
  }
  lineChart($("c-tu"), tu, t("c_tu"));
  if (selected != null) drawDetail();
}

function drawTable() {
  const rows = {};
  current.series.forEach((s) => (rows[s.date] = { ...s }));
  (current.rejected_dates || []).forEach((r) => (rows[r.date] = { ...r, rejected: true }));
  (current.radar?.series || []).forEach((r) => (rows[r.date] = { ...rows[r.date], date: r.date, radar_pct: r.hyacinth_pct }));
  const cell = (v, d = 1) => `<td>${v == null ? "" : nf(v, d)}</td>`;
  document.querySelector("#table tbody").innerHTML = Object.values(rows)
    .sort((a, b) => (a.date < b.date ? 1 : -1))
    .map((s) => s.rejected ? `<tr class="rejected"><td>${fmtDate(s.date)}</td><td colspan="6">${t(`rejected_${s.reason}`,
        { aot: nf(s.aot, 2), c: nf((s.cloud_share || 0) * 100, 0), pct: nf(s.hyacinth_pct) })}</td>` +
        `<td><a href="${photoUrl(s.scene, riverFeature(), 0.005)}" target="_blank" rel="noopener">${t("photo_link")}</a></td></tr>`
      : `<tr><td>${fmtDate(s.date)}</td>${cell(s.hyacinth_pct)}${cell(s.radar_pct)}${cell(s.hyacinth_km2, 3)}${cell(s.open_water_pct)}${cell(s.turbid_pct)}${cell(s.valid_fraction == null ? null : s.valid_fraction * 100, 0)}<td>${s.scene ? `<a href="${photoUrl(s.scene, riverFeature(), 0.005)}" target="_blank" rel="noopener">${t("photo_link")}</a>` : ""}</td></tr>`)
    .join("");
}

function drawAbout() {
  const th = current.thresholds, look = current.outlook;
  const vars = { ndvi: nf(th.hyacinth_ndvi, 2), ndti: nf(th.turbid_ndti, 2), a: nf(look?.alert_pct ?? 30, 0),
    mae: nf(look?.mae_river), maes: nf(look?.mae_segment) };
  const ft = current.flood_test?.flood_test?.at(-1);
  if (ft) Object.assign(vars, { fy: ft.year, fn: nf(ft.dry_green_px, 0), fc: nf(ft.cleared_px, 0), fs: nf(ft.stayed_px, 0),
    fch: nf(ft.channel_cleared_share * 100, 0), fgc: nf(ft.green_channel_stayed_share * 100, 0) });
  const lc = current.landsat_check;
  vars.landsat = lc ? t("about_landsat", { n: nf(lc.pairs, 0), mae: nf(lc.river_mae_pts), bias: nf(lc.river_bias_pts),
    r: nf(lc.river_correlation, 2), sr: nf(lc.stretch_correlation, 2), smae: nf(lc.stretch_mae_pts) }) : "";
  const u = current.uncertainty;
  if (u) Object.assign(vars, { rn: nf(u.river_noise_pts), rp: nf(u.river_pairs, 0), sh: nf(u.segment_noise_pts.high),
    nlo: nf(u.range_ndvi[0], 2), nhi: nf(u.range_ndvi[1], 2), nrej: nf((current.rejected_dates || []).length, 0),
    nj: nf(u.one_off_jumps, 0), nr: nf(u.readings, 0) });
  document.querySelectorAll("[data-i18n-html]").forEach((el) => (el.innerHTML = t(el.dataset.i18nHtml, vars)));
  const el = $("radar-about");
  el.hidden = !current.radar;
  if (current.radar) el.innerHTML = t("radar_about", { pairs: nf(current.radar.pairs, 0), mae: nf(current.radar.mae_pct) });
}

// ---------- downloads ----------
function download(name, header, rows) {
  const esc = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v);
  const text = [header, ...rows].map((r) => r.map(esc).join(",")).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function initDownloads() {
  $("dl-dates").addEventListener("click", () => {
    const radar = Object.fromEntries((current.radar?.series || []).map((r) => [r.date, r.hyacinth_pct]));
    download(`${current.river.id}-by-date.csv`,
      ["date", "hyacinth_pct", "hyacinth_km2", "open_water_pct", "muddy_pct_of_open_water", "cloud_free_pct", "radar_hyacinth_pct",
        ...current.regions.map((r) => `hyacinth_pct_${r.id}`)],
      current.series.map((s) => [s.date, s.hyacinth_pct, s.hyacinth_km2, s.open_water_pct, s.turbid_pct,
        Math.round(s.valid_fraction * 100), radar[s.date],
        ...current.regions.map((r) => r.series.find((x) => x.date === s.date)?.hyacinth_pct)]));
  });
  $("dl-segs").addEventListener("click", () => {
    download(`${current.river.id}-by-stretch.csv`,
      ["stretch", "km_from_start", "place", "area", "wards", "hyacinth_pct", "last_clear_date", "change_30d_pts",
        "outlook_14d_pct", "chance_above_alert", "confidence", "confidence_reasons", "river_area_ha", "green_channel_ha", "lat", "lon"],
      current.segments.features.map((f) => {
        const p = f.properties, c = f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)];
        return [p.id + 1, p.km, p.place, p.regions.join("+") || "outside", p.wards.join(" / "), p.hyacinth_pct, p.date,
          p.change_pct, p.outlook?.pct, p.outlook?.p_alert, p.confidence, (p.confidence_why || []).join(" ") || p.why, p.area_ha, p.green_channel_ha, c[1], c[0]];
      }));
  });
}

// ---------- state ----------
function syncUrl() {
  const r = new URLSearchParams({ river: $("river").value });
  if (state.region !== "all") r.set("area", state.region);
  if (state.lang !== "en") r.set("lang", state.lang);
  $("report-link").href = `report.html?${r}`;
  const q = new URLSearchParams();
  q.set("river", $("river").value);
  if (state.region !== "all") q.set("area", state.region);
  if (state.lang !== "en") q.set("lang", state.lang);
  if (selected != null) q.set("stretch", selected + 1);
  history.replaceState(null, "", `?${q}`);
}

function fillRiverSelect() {
  const sel = $("river"), keep = sel.value;
  sel.innerHTML = riverIndex.rivers
    .map((r) => `<option value="${r.id}">${state.lang === "mr" ? r.name_mr || r.name : r.name}</option>`).join("");
  if (keep) sel.value = keep;
}

function fillRegionSelect() {
  const sel = $("region");
  sel.innerHTML = `<option value="all">${t("all_areas")}</option>` + current.regions
    .map((r) => `<option value="${r.id}">${r.id === "other" ? t("outside") : regionName(r)}</option>`).join("");
  sel.value = current.regions.some((r) => r.id === state.region) ? state.region : "all";
  state.region = sel.value;
}

function setRegion(id) {
  state.region = id;
  $("region").value = id;
  syncUrl();
  renderAll(true);
}

function setLang(lang) {
  state.lang = lang;
  try { localStorage.setItem("lang", lang); } catch { /* private mode */ }
  syncUrl();
  applyStaticText();
  if (current) { fillRiverSelect(); fillRegionSelect(); renderAll(false); }
}

function renderAll(fit) {
  $("meta").textContent = t("meta", { date: fmtDate(current.latest_date), n: nf(current.series.length, 0) });
  drawLegend();
  drawTiles();
  drawSegments(fit);
  setIdx(idx);
  if (playTimer) $("play").textContent = t("pause");
  document.querySelectorAll("[data-list]").forEach((b) => b.classList.toggle("on", b.dataset.list === listMode));
  drawList();
  drawRegions();
  drawCharts();
  drawTable();
  drawAbout();
  drawDetail();
}

// ---------- load ----------
async function loadRiver(id) {
  stopPlay();
  current = await fetch(`data/${id}.json`, { cache: "no-cache" }).then((r) => r.json());
  current.regions ??= [];
  idx = current.segment_history.dates.length - 1;
  $("slider").max = idx;
  fillRegionSelect();
  if (selected != null && !current.segments.features.some((f) => f.properties.id === selected)) selected = null;
  renderAll(true);
}

async function main() {
  const q = new URLSearchParams(location.search);
  let saved = null;
  try { saved = localStorage.getItem("lang"); } catch { /* private mode */ }
  state.lang = q.get("lang") || saved || "en";
  if (!STRINGS[state.lang]) state.lang = "en";
  state.region = q.get("area") || "all";
  const st = parseInt(q.get("stretch"), 10);
  if (st > 0) selected = st - 1;
  applyStaticText();

  initMap();
  initSlider();
  initDownloads();
  document.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => setLang(b.dataset.lang)));
  document.querySelectorAll("[data-list]").forEach((b) => b.addEventListener("click", () => { listMode = b.dataset.list; renderAll(false); }));
  $("region").addEventListener("change", (e) => setRegion(e.target.value));
  $("d-close").addEventListener("click", () => { selected = null; syncUrl(); drawDetail(); });
  $("d-link").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(location.href); $("d-link").textContent = t("copied"); }
    catch { /* clipboard blocked: the URL bar already has the link */ }
    setTimeout(() => ($("d-link").textContent = t("copy_link")), 1500);
  });

  riverIndex = await fetch("data/index.json", { cache: "no-cache" }).then((r) => r.json());
  const sel = $("river");
  fillRiverSelect();
  const wanted = q.get("river");
  if (wanted && riverIndex.rivers.some((r) => r.id === wanted)) sel.value = wanted;
  sel.addEventListener("change", () => { selected = null; state.region = "all"; syncUrl(); loadRiver(sel.value); });
  await loadRiver(sel.value);
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawCharts, 150); });
}

main().catch((e) => {
  $("meta").textContent = t("load_error") + e.message;
});
