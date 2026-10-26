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
  return place ? t("near", { place }) : t("stretch", { n: nf(p.id + 1, 0) });
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
  baseLayers.sat = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" });
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
    (state.region !== "all" ? `<span><i class="sw other"></i>${t("legend_other_area")}</span>` : "");
}

// Value of a segment on the date at slider position idx (null: clouds that day).
const valueAt = (id) => current.segment_history.hyacinth_pct[idx][id];

function segStyle(f) {
  const p = f.properties, v = valueAt(p.id), mine = inRegion(p);
  if (!mine) return { color: css("--clear"), weight: 2, opacity: 0.25, dashArray: null, lineCap: "round" };
  if (v == null) return { color: css("--clear"), weight: 3, opacity: 0.8, dashArray: "2 6", lineCap: "round" };
  return { color: binColor(v), weight: v >= BINS[0] ? 6 : 3, opacity: 0.95, dashArray: null, lineCap: "round" };
}

function segTip(p) {
  const v = valueAt(p.id), d = current.segment_history.dates[idx];
  const val = v == null ? t("legend_cloud") : `<span class="tt-val">${nf(v)}%</span>`;
  return `<div class="tt-title">${segName(p)} · ${segAreaLabel(p)}</div><div>${val}</div><div class="tt-sub">${fmtDate(d)}</div>`;
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
  const feats = current.segments.features.map((f) => f.properties).filter(inRegion);
  const look = current.outlook;
  $("list-hint").textContent = listMode === "risk" && look?.alert_pct
    ? t("hint_risk", { a: nf(look.alert_pct, 0) }) : t("hint_list");
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
      <span class="hs-name">${segName(p)}<small>${segAreaLabel(p)}</small></span><span class="hs-val">${val}</span>
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
  const fact = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  const change = p.change_pct == null ? "–" : `${p.change_pct > 0 ? "+" : ""}${t("pts", { v: nf(p.change_pct) })}`;
  $("d-facts").innerHTML = [
    fact(t("f_area"), segAreaLabel(p)),
    p.wards.length ? fact(t("f_ward"), p.wards.join(" / ")) : "",
    fact(t("f_now"), `<strong>${nf(p.hyacinth_pct)}%</strong>`),
    fact(t("f_seen"), fmtDate(p.date)),
    fact(t("f_change"), change),
    p.outlook ? fact(t("f_outlook"), `~${nf(p.outlook.pct)}%`) : "",
    p.outlook && look?.alert_pct ? fact(t("f_chance", { a: nf(look.alert_pct, 0) }), `${nf(p.outlook.p_alert * 100, 0)}%`) : "",
    fact(t("f_km"), t("km_val", { km: nf(p.km) })),
  ].join("");
  const sh = current.segment_history;
  const rows = sh.dates.map((d, i) => ({ date: d, v: sh.hyacinth_pct[i][p.id] })).filter((r) => r.v != null);
  lineChart($("c-seg"), [{ name: segName(p), cls: "opt", rows, key: "v" }], t("f_now"));
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
  const vmax = niceMax(Math.max(...all.map((p) => p.v)));
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
    const area = li ? "" : `<path class="area" d="${runs.filter((r) => r.length > 1)
      .map((r) => `${seg(r)}L${x(r[r.length - 1].t)},${y(0)}L${x(r[0].t)},${y(0)}Z`).join("")}"/>`;
    // Primary: dots only where a point stands alone. Radar: a marker on every (sparser) estimate.
    const dots = l.cls === "rad" ? l.pts : runs.filter((r) => r.length === 1).map((r) => r[0]);
    const last = l.pts[l.pts.length - 1];
    return `${area}<path class="line ${l.cls}" d="${runs.map(seg).join("")}"/>
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
      rowsHtml.push(`<div>${name}: <span class="tt-val">${nf(p.v)}%</span>${when}</div>${l.tip ? l.tip(p.d) : ""}`);
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
      : t("n_pct");
  $("t-area").textContent = nf(last.hyacinth_km2, 2);
  $("t-area-note").textContent = reg ? t("n_area_whole") : t("n_area");

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
  $("t-outlook").textContent = lo ? `~${nf(lo.pct)}%` : "–";
  $("t-outlook").className = "tile-value";
  $("t-outlook-note").textContent = !look ? t("n_outlook_none")
    : look.monsoon ? t("n_outlook_monsoon", { date: fmtDate(look.target_date) })
      : !lo ? t("n_outlook_none")
        : t(reg ? "n_outlook_region" : "n_outlook", { lo: nf(lo.low), hi: nf(lo.high), date: fmtDate(look.target_date) });

  $("t-turbid").textContent = last.turbid_pct == null ? "–" : `${nf(last.turbid_pct)}%`;
  $("t-turbid-note").textContent = last.turbid_pct == null ? t("n_turbid_none")
    : t("n_turbid", { w: nf(last.open_water_pct, 0) });
}

// ---------- regions table ----------
function drawRegions() {
  const feats = current.segments.features.map((f) => f.properties);
  $("regions").querySelector("tbody").innerHTML = current.regions.map((r) => {
    const rows = r.series, now = rows[rows.length - 1];
    const yr = now && closestTo(rows, toT(now.date) - 365 * dayMs, 21);
    const mine = feats.filter((p) => (r.id === "other" ? !p.regions.length : p.regions.includes(r.id)));
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
  const hy = [optical("hyacinth_pct")];
  const reg = selectedRegion();
  if (reg) {
    hy[0].name = t("l_whole");
    hy.push({ name: t("l_region", { area: reg.id === "other" ? t("outside") : regionName(reg) }), cls: "reg", rows: reg.series, key: "hyacinth_pct" });
  }
  if (current.radar) {
    hy.push({ name: t("l_radar", { e: nf(current.radar.mae_pct) }), cls: "rad", rows: current.radar.series, key: "hyacinth_pct", gap: 30 });
  }
  lineChart($("c-hy"), hy, t("c_hy"));
  lineChart($("c-tu"), [optical("turbid_pct")], t("c_tu"));
  if (selected != null) drawDetail();
}

function drawTable() {
  const rows = {};
  current.series.forEach((s) => (rows[s.date] = { ...s }));
  (current.radar?.series || []).forEach((r) => (rows[r.date] = { ...rows[r.date], date: r.date, radar_pct: r.hyacinth_pct }));
  const cell = (v, d = 1) => `<td>${v == null ? "" : nf(v, d)}</td>`;
  document.querySelector("#table tbody").innerHTML = Object.values(rows)
    .sort((a, b) => (a.date < b.date ? 1 : -1))
    .map((s) => `<tr><td>${fmtDate(s.date)}</td>${cell(s.hyacinth_pct)}${cell(s.radar_pct)}${cell(s.hyacinth_km2, 3)}${cell(s.open_water_pct)}${cell(s.turbid_pct)}${cell(s.valid_fraction == null ? null : s.valid_fraction * 100, 0)}</tr>`)
    .join("");
}

function drawAbout() {
  const th = current.thresholds, look = current.outlook;
  const vars = { ndvi: nf(th.hyacinth_ndvi, 2), ndti: nf(th.turbid_ndti, 2), a: nf(look?.alert_pct ?? 30, 0),
    mae: nf(look?.mae_river), maes: nf(look?.mae_segment) };
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
        "outlook_14d_pct", "chance_above_alert", "lat", "lon"],
      current.segments.features.map((f) => {
        const p = f.properties, c = f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)];
        return [p.id + 1, p.km, p.place, p.regions.join("+") || "outside", p.wards.join(" / "), p.hyacinth_pct, p.date,
          p.change_pct, p.outlook?.pct, p.outlook?.p_alert, c[1], c[0]];
      }));
  });
}

// ---------- state ----------
function syncUrl() {
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
