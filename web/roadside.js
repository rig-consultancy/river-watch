// Roadside Watch. Reads data/roadside-index.json and data/roadside-<site>.json written by
// pipeline/roadside.py. "All areas" puts every watched society's roads on one map and table.

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const tip = $("tip");
const DAY_BINS = [7, 14, 30]; // days uncleared: under a week, 1-2 weeks, 2 weeks to a month, longer

let map, layer, index, sites = {}, lands = {}, view;
const state = { lang: "en", site: "all", list: "open", show: "all" };

// ---------- language ----------
const locale = () => (state.lang === "mr" ? "mr-IN" : "en-IN");
function t(key, vars = {}) {
  const s = STRINGS[state.lang][key] ?? STRINGS.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
}
const nf = (v, d = 0) => (v == null ? "–" : new Intl.NumberFormat(locale(), { maximumFractionDigits: d }).format(v));
const fmtDate = (d) => new Date(d + "T00:00:00").toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric" });
const fmtShort = (d) => new Date(d + "T00:00:00").toLocaleDateString(locale(), { day: "numeric", month: "short" });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const median = (a) => {
  if (!a.length) return null;
  const s = [...a].sort((x, y) => x - y), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const siteName = (s) => (state.lang === "mr" ? s.name_mr || s.name : s.name);

function applyStaticText() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll("[data-i18n-html]").forEach((el) => (el.innerHTML = t(el.dataset.i18nHtml)));
  document.querySelectorAll("[data-lang]").forEach((b) => b.classList.toggle("on", b.dataset.lang === state.lang));
  document.title = t("rs_title");
}

// ---------- the data being shown: one site, or every site together ----------
function buildView() {
  const list = state.site === "all" ? Object.values(sites) : [sites[state.site]];
  const tag = (s) => (x) => ({ ...x, site: s });
  const spots = list.flatMap((s) => s.garbage.spots.map(tag(s)));
  const parked = list.flatMap((s) => s.vehicles.long_parked.map(tag(s)));
  // Sites can be imaged on different days; per-date figures are added up by date.
  const byDate = {};
  for (const s of list) for (const p of s.vehicles.per_date) {
    const o = (byDate[p.date] ??= { date: p.date, vehicles: 0, dumps: 0, kerb: 0, road: 0 });
    o.vehicles += p.vehicles; o.dumps += p.dumps; o.kerb += (p.kerb_used_pct ?? 0) * s.road_m; o.road += s.road_m;
  }
  const perDate = Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date))
    .map((o) => ({ ...o, kerb_used_pct: o.road ? o.kerb / o.road : null }));
  const open = spots.filter((s) => s.open);
  const days = (s) => s.episodes.at(-1).min_days;
  const cleared = spots.flatMap((s) => s.episodes.filter((e) => !e.open));
  return {
    list, spots, parked, perDate, open, cleared,
    land: list.map((s) => lands[s.site]).filter(Boolean),
    example: list.some((s) => s.example),
    latest: list.map((s) => s.latest).sort().at(-1),
    images: list.reduce((n, s) => n + s.images, 0),
    roadM: list.reduce((n, s) => n + s.road_m, 0),
    uncleared: open.map(days),
    clearDays: cleared.map((e) => e.min_days),
    cameBack: spots.filter((s) => s.episodes.length > 1).length,
    parkedNow: parked.filter((p) => p.open),
  };
}

// ---------- tiles ----------
function tile(id, value, note) { $(id).textContent = value; $(id + "-note").textContent = note; }
function drawTiles() {
  const v = view, last = v.perDate.at(-1);
  tile("t-open", nf(v.open.length), t("rs_n_open", { m2: nf(v.open.reduce((n, s) => n + (s.area_m2 || 0), 0)) }));
  tile("t-days", v.uncleared.length ? t("rs_days", { n: nf(median(v.uncleared)) }) : "–",
    v.uncleared.length ? t("rs_n_days", { max: nf(Math.max(...v.uncleared)) }) : t("rs_n_none_open"));
  tile("t-cleared", nf(v.cleared.length),
    v.clearDays.length ? t("rs_n_cleared", { n: nf(median(v.clearDays)) }) : t("rs_n_none_cleared"));
  tile("t-back", nf(v.cameBack), t("rs_n_back", { n: nf(v.spots.length) }));
  tile("t-parked", nf(v.parkedNow.length), t("rs_n_parked", { n: nf(v.parked.length) }));
  tile("t-kerb", last ? nf(last.kerb_used_pct, 1) + "%" : "–",
    last ? t("rs_n_kerb", { n: nf(last.vehicles), km: nf(v.roadM / 1000, 1) }) : "");
}

// ---------- map ----------
function dayColor(d) {
  if (d < DAY_BINS[0]) return css("--ramp-1");
  if (d < DAY_BINS[1]) return css("--ramp-2");
  if (d < DAY_BINS[2]) return css("--ramp-3");
  return css("--ramp-4");
}
function spotRange(e) {
  return e.max_days == null ? t("rs_range_open", { min: nf(e.min_days) }) : t("rs_range", { min: nf(e.min_days), max: nf(e.max_days) });
}
function spotTip(s) {
  const e = s.episodes.at(-1);
  const head = s.open ? t("rs_tip_open", { n: nf(e.min_days) }) : t("rs_tip_cleared");
  return `<strong>${esc(head)}</strong><br>${esc(t("rs_since", { from: fmtDate(e.first), to: fmtDate(e.last) }))}`
    + `<br>${esc(spotRange(e))} · ${esc(t("rs_m2", { n: nf(s.area_m2) }))}`
    + (s.episodes.length > 1 ? `<br>${esc(t("rs_tip_back", { n: nf(s.episodes.length) }))}` : "")
    + `<br>${esc(t("rs_ward", { w: s.ward || t("rs_no_ward") }))}`;
}
function parkedTip(p) {
  return `<strong>${esc(t(p.open ? "rs_tip_parked_now" : "rs_tip_parked_was", { n: nf(p.min_days) }))}</strong>`
    + `<br>${esc(t("rs_since", { from: fmtDate(p.first), to: fmtDate(p.last) }))}`
    + `<br>${esc(t("rs_ward", { w: p.ward || t("rs_no_ward") }))}`;
}
function bindTip(m, html) {
  m.on("mouseover", (e) => showTip(html, e.originalEvent.clientX, e.originalEvent.clientY));
  m.on("mouseout", hideTip);
  m.on("click", (e) => showTip(html, e.originalEvent.clientX, e.originalEvent.clientY));
}
function drawMap(fit) {
  if (layer) map.removeLayer(layer);
  layer = L.featureGroup();
  for (const s of view.list) {
    L.polyline(s.roads.map((ln) => ln.map(([x, y]) => [y, x])), { color: css("--series-1"), weight: 7, opacity: 0.3, interactive: false }).addTo(layer);
  }
  if (state.show === "all" || state.show === "land") {
    for (const ld of view.land) for (const p of ld.patches) {
      const g = L.geoJSON(p.geometry, { style: { color: css("--critical"), weight: 2, dashArray: "4 3", fillColor: css("--critical"), fillOpacity: 0.12 } });
      g.eachLayer((l) => bindTip(l, patchTip(p, ld)));
      g.addTo(layer);
    }
  }
  if (state.show === "all" || state.show === "garbage") {
    for (const s of view.spots) {
      const e = s.episodes.at(-1);
      const m = L.circleMarker([s.lat, s.lon], s.open
        ? { radius: 9, color: css("--surface-1"), weight: 2, fillColor: dayColor(e.min_days), fillOpacity: 1 }
        : { radius: 7, color: css("--clear"), weight: 2, fillOpacity: 0, dashArray: "3 3" });
      bindTip(m, spotTip(s));
      m.addTo(layer);
    }
  }
  if (state.show === "all" || state.show === "vehicles") {
    for (const p of view.parked) {
      const m = L.marker([p.lat, p.lon], { icon: L.divIcon({ className: "car-pin" + (p.open ? " now" : ""), iconSize: [14, 14] }), keyboard: false });
      bindTip(m, parkedTip(p));
      m.addTo(layer);
    }
  }
  layer.addTo(map);
  // The page lays out after the map is created; measure again so the whole road fits.
  map.invalidateSize();
  if (fit && layer.getLayers().length) map.fitBounds(layer.getBounds(), { padding: [30, 30], maxZoom: 17 });
}
function drawLegend() {
  const sw = (c) => `<span class="dot-sw" style="background:${c}"></span>`;
  const bins = [`< ${DAY_BINS[0]}`, `${DAY_BINS[0]}–${DAY_BINS[1]}`, `${DAY_BINS[1]}–${DAY_BINS[2]}`, `${DAY_BINS[2]}+`];
  const ramp = ["--ramp-1", "--ramp-2", "--ramp-3", "--ramp-4"];
  $("legend").innerHTML = `<span>${esc(t("rs_legend_days"))}</span>`
    + bins.map((b, i) => `<span>${sw(css(ramp[i]))}${esc(b)}</span>`).join("")
    + `<span><span class="dot-sw hollow"></span>${esc(t("rs_legend_cleared"))}</span>`
    + `<span><span class="car-sw"></span>${esc(t("rs_legend_parked"))}</span>`
    + (view.land.some((l) => l.patches.length) ? `<span><span class="land-sw"></span>${esc(t("rs_legend_land"))}</span>` : "");
}

// ---------- list ----------
function mapsLink(lat, lon) {
  return `<a href="https://www.google.com/maps?q=${lat},${lon}" target="_blank" rel="noopener">${esc(t("rs_open_map"))}</a>`;
}
function drawList() {
  let rows, hint;
  if (state.list === "open") {
    rows = [...view.open].sort((a, b) => b.episodes.at(-1).min_days - a.episodes.at(-1).min_days).map((s) => {
      const e = s.episodes.at(-1);
      return { lat: s.lat, lon: s.lon, val: t("rs_days", { n: nf(e.min_days) }), color: dayColor(e.min_days),
        line: t("rs_since_short", { d: fmtDate(e.first) }) + (s.episodes.length > 1 ? " · " + t("rs_back_short") : ""), s };
    });
    hint = t("rs_hint_open");
  } else if (state.list === "cleared") {
    rows = view.spots.flatMap((s) => s.episodes.filter((e) => !e.open).map((e) => ({ s, e })))
      .sort((a, b) => b.e.last.localeCompare(a.e.last)).map(({ s, e }) => ({
        lat: s.lat, lon: s.lon, val: t("rs_days", { n: nf(e.min_days) }), color: css("--clear"),
        line: t("rs_cleared_line", { from: fmtShort(e.first), to: fmtShort(e.last) }), s }));
    hint = t("rs_hint_cleared");
  } else {
    rows = [...view.parked].sort((a, b) => b.open - a.open || b.min_days - a.min_days).map((p) => ({
      lat: p.lat, lon: p.lon, val: t("rs_days", { n: nf(p.min_days) }), color: css("--series-3"),
      line: (p.open ? t("rs_still_there") + " · " : "") + t("rs_since_short", { d: fmtDate(p.first) }), s: p }));
    hint = t("rs_hint_parked", { n: nf(view.list[0].long_parked_days) });
  }
  $("list-hint").textContent = hint;
  $("spots").innerHTML = rows.length ? rows.map((r) => `<li>
      <span class="sp-dot" style="background:${r.color}"></span>
      <span class="sp-main"><span class="sp-ward">${esc(r.s.ward || t("rs_no_ward"))}${state.site === "all" && view.list.length > 1 ? " · " + esc(siteName(r.s.site)) : ""}</span>
      <span class="sp-line">${esc(r.line)}</span>
      <span class="sp-links">${mapsLink(r.lat, r.lon)} · <button type="button" class="linkish" data-copy="${r.lat}, ${r.lon}">${esc(t("rs_copy"))}</button></span></span>
      <span class="sp-val">${esc(r.val)}</span></li>`).join("")
    : `<li class="empty">${esc(t("rs_empty"))}</li>`;
}

// ---------- by ward ----------
function drawWards() {
  const w = {};
  const row = (name) => (w[name] ??= { open: 0, days: [], cleared: 0, back: 0, parked: 0 });
  for (const s of view.spots) {
    const r = row(s.ward || t("rs_no_ward"));
    if (s.open) { r.open++; r.days.push(s.episodes.at(-1).min_days); }
    r.cleared += s.episodes.filter((e) => !e.open).length;
    if (s.episodes.length > 1) r.back++;
  }
  for (const p of view.parkedNow) row(p.ward || t("rs_no_ward")).parked++;
  const rows = Object.entries(w).sort((a, b) => b[1].open - a[1].open || (median(b[1].days) ?? 0) - (median(a[1].days) ?? 0));
  $("wards").querySelector("tbody").innerHTML = rows.map(([name, r]) => `<tr><td>${esc(name)}</td>
    <td class="num">${nf(r.open)}</td><td class="num">${r.days.length ? esc(t("rs_days", { n: nf(median(r.days)) })) : "–"}</td>
    <td class="num">${r.days.length ? esc(t("rs_days", { n: nf(Math.max(...r.days)) })) : "–"}</td>
    <td class="num">${nf(r.cleared)}</td><td class="num">${nf(r.back)}</td><td class="num">${nf(r.parked)}</td></tr>`).join("")
    || `<tr><td colspan="7" class="muted">${esc(t("rs_empty"))}</td></tr>`;
}

// ---------- charts ----------
function barChart(el, rows, bars, line, xl = { long: (r) => fmtDate(r.date), short: (r) => fmtShort(r.date) }) {
  const W = el.clientWidth || 400, H = el.clientHeight || 220, pad = { l: 34, r: line ? 40 : 10, t: 10, b: 26 };
  if (!rows.length) { el.innerHTML = `<p class="muted">${esc(t("rs_empty"))}</p>`; return; }
  const max = Math.max(1, ...rows.map(bars.value));
  const step = Math.max(1, Math.ceil(max / 4)), top = step * Math.ceil(max / step);
  const x = (i) => pad.l + (i + 0.5) * (W - pad.l - pad.r) / rows.length;
  const y = (v) => H - pad.b - v / top * (H - pad.t - pad.b);
  const bw = Math.max(4, Math.min(28, (W - pad.l - pad.r) / rows.length * 0.6));
  const ticks = []; for (let v = 0; v <= top; v += step) ticks.push(v);
  const every = Math.ceil(rows.length / Math.max(1, Math.floor((W - pad.l - pad.r) / 60)));
  let svg = `<svg width="${W}" height="${H}" role="img" aria-label="${esc(bars.label)}">`
    + ticks.map((v) => `<line class="grid" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/><text class="tick" x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${nf(v)}</text>`).join("")
    + rows.map((r, i) => `<rect class="bar" x="${x(i) - bw / 2}" y="${y(bars.value(r))}" width="${bw}" height="${H - pad.b - y(bars.value(r))}" rx="2" style="fill:${bars.color}"><title>${esc(xl.long(r))}: ${nf(bars.value(r), 1)}</title></rect>`
      + (i % every === 0 ? `<text class="tick" x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(xl.short(r))}</text>` : "")).join("");
  if (line) {
    const lmax = Math.max(1, ...rows.map(line.value)), ltop = Math.ceil(lmax / 5) * 5;
    const ly = (v) => H - pad.b - v / ltop * (H - pad.t - pad.b);
    svg += `<polyline fill="none" stroke="${line.color}" stroke-width="2" points="${rows.map((r, i) => `${x(i)},${ly(line.value(r))}`).join(" ")}"/>`
      + rows.map((r, i) => `<circle cx="${x(i)}" cy="${ly(line.value(r))}" r="3" fill="${line.color}"><title>${esc(xl.long(r))}: ${nf(line.value(r), 1)}%</title></circle>`).join("")
      + `<text class="tick" x="${W - pad.r + 6}" y="${ly(ltop) + 4}">${nf(ltop)}%</text><text class="tick" x="${W - pad.r + 6}" y="${ly(0) + 4}">0%</text>`;
  }
  el.innerHTML = svg + "</svg>";
}
function drawCharts() {
  barChart($("c-dumps"), view.perDate, { value: (r) => r.dumps, color: css("--ramp-2"), label: t("rs_c_dumps") });
  barChart($("c-vehicles"), view.perDate, { value: (r) => r.vehicles, color: css("--series-1"), label: t("rs_c_vehicles") },
    { value: (r) => r.kerb_used_pct ?? 0, color: css("--series-3") });
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

// ---------- page ----------
function fillSiteSelect() {
  const sel = $("site");
  sel.innerHTML = (index.length > 1 ? `<option value="all">${esc(t("rs_all_sites"))}</option>` : "")
    + index.map((s) => `<option value="${s.id}">${esc(siteName(s))}</option>`).join("");
  if (!sites[state.site] && state.site !== "all") state.site = "all";
  if (index.length === 1) state.site = index[0].id;
  sel.value = state.site;
}
function syncUrl() {
  const q = new URLSearchParams();
  if (state.site !== "all") q.set("site", state.site);
  if (state.lang !== "en") q.set("lang", state.lang);
  history.replaceState(null, "", q.toString() ? `?${q}` : location.pathname);
}
// ---------- land change (real Sentinel-2 data, 10 m) ----------
function verdict(p) {
  return p.stayed_bare_in_monsoon ? t("rs_p_covered") : t("rs_p_bare");
}
function patchTip(p, ld) {
  return `<strong>${esc(t("rs_p_title", { n: nf(p.id), m2: nf(p.area_m2) }))}</strong>`
    + `<br>${esc(t("rs_p_change", { before: nf(p.ndvi_before, 2), now: nf(p.ndvi_now, 2), year: p.first_bare_year ?? "–" }))}`
    + `<br>${esc(verdict(p))}<br>${esc(t("rs_ward", { w: p.ward || t("rs_no_ward") }))} · ${esc(t("rs_p_road", { m: nf(p.road_m) }))}`;
}
function sparkline(series, dates) {
  const W = 220, H = 44, pts = series.map((v, i) => [i, v]).filter(([, v]) => v != null);
  if (pts.length < 2) return "";
  const x = (i) => 2 + i / (series.length - 1) * (W - 4), y = (v) => H - 4 - Math.max(0, Math.min(v, 0.9)) / 0.9 * (H - 8);
  // Shade each monsoon (Jul-Oct), when open land should turn green.
  const bands = [];
  dates.forEach((d, i) => { const m = +d.slice(5, 7); if (m >= 7 && m <= 10) bands.push(i); });
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
    + bands.map((i) => `<rect x="${x(i) - 1.5}" y="0" width="3" height="${H}" class="spark-monsoon"/>`).join("")
    + `<line x1="0" x2="${W}" y1="${y(0.35)}" y2="${y(0.35)}" class="spark-green"/>`
    + `<polyline points="${pts.map(([i, v]) => `${x(i)},${y(v)}`).join(" ")}" class="spark-line"/></svg>`;
}
function drawLand() {
  const L_ = view.land;
  $("land").hidden = !L_.length;
  if (!L_.length) return;
  const patches = L_.flatMap((ld) => ld.patches.map((p) => ({ ...p, ld })));
  const ha = L_.reduce((n, l) => n + l.near_roads_ha, 0);
  const first = L_.map((l) => l.first).sort()[0], last = L_.map((l) => l.last).sort().at(-1);
  const m = L_[0].near_road_m;
  $("land-intro").textContent = t("rs_land_intro", { n: nf(L_.reduce((n, l) => n + l.images, 0)), from: fmtDate(first), to: fmtDate(last) });
  $("lf-patches").textContent = nf(patches.length);
  $("lf-patches-note").textContent = t("rs_lf_patches_note", { m2: nf(patches.reduce((n, p) => n + p.area_m2, 0)), road: nf(L_[0].patch_road_m) });
  // Latest season with at least two post-monsoon images (October alone is too early to judge).
  const years = [...new Set(L_.flatMap((l) => l.trend.filter((r) => r.images >= 2).map((r) => r.year)))].sort();
  const pct = (y) => {
    const rows = L_.map((l) => [l.trend.find((r) => r.year === y), l.near_roads_ha]).filter(([r]) => r);
    const w = rows.reduce((n, [, h]) => n + h, 0);
    return w ? rows.reduce((n, [r, h]) => n + r.green_pct * h, 0) / w : null;
  };
  const ly = years.at(-1), fy = years[0];
  $("lf-green").textContent = ly ? nf(pct(ly), 0) + "%" : "–";
  $("lf-green-note").textContent = ly ? t("rs_lf_green_note", { year: ly, then: nf(pct(fy), 0), first: fy }) : "";
  $("lf-bare").textContent = t("rs_ha", { n: nf(L_.reduce((n, l) => n + l.bare_near_roads_dry_ha, 0), 1) });
  $("lf-bare-note").textContent = t("rs_lf_bare_note", { year: L_[0].dry_year, ha: nf(ha, 1), m: nf(m) });
  $("lf-trend-label").textContent = t("rs_lf_trend", { m: nf(m) });
  $("patches").innerHTML = patches.length ? patches.map((p) => `<li>
      <div class="p-head"><strong>${esc(t("rs_p_title", { n: nf(p.id), m2: nf(p.area_m2) }))}</strong>
        <span class="p-verdict ${p.stayed_bare_in_monsoon ? "covered" : ""}">${esc(verdict(p))}</span></div>
      <div class="sp-line">${esc(t("rs_p_change", { before: nf(p.ndvi_before, 2), now: nf(p.ndvi_now, 2), year: p.first_bare_year ?? "–" }))}</div>
      ${sparkline(p.series, p.ld.dates)}
      <div class="sp-links">${esc(p.ward || t("rs_no_ward"))} · ${esc(t("rs_p_road", { m: nf(p.road_m) }))} · ${mapsLink(p.lat, p.lon)}
        · <button type="button" class="linkish" data-copy="${p.lat}, ${p.lon}">${esc(t("rs_copy"))}</button></div></li>`).join("")
    : `<li class="empty">${esc(t("rs_p_none"))}</li>`;
  const rows = [...new Set(L_.flatMap((l) => l.trend.map((r) => r.year)))].sort().map((y) => ({ year: y, pct: pct(y), partial: !years.includes(y) }));
  barChart($("c-green"), rows, { value: (r) => r.pct ?? 0, color: css("--good"), label: t("rs_lf_trend", { m: nf(m) }) }, null,
    { long: (r) => r.year + (r.partial ? " " + t("rs_partial") : ""), short: (r) => String(r.year) + (r.partial ? "*" : "") });
}

function renderAll(fit) {
  view = buildView();
  $("example").hidden = !view.example;
  $("meta").textContent = t("rs_meta", { date: fmtDate(view.latest), n: nf(view.images), km: nf(view.roadM / 1000, 1) });
  document.querySelectorAll("[data-list]").forEach((b) => b.classList.toggle("on", b.dataset.list === state.list));
  document.querySelectorAll("[data-show]").forEach((b) => b.classList.toggle("on", b.dataset.show === state.show));
  drawTiles();
  drawMap(fit);
  drawLegend();
  drawList();
  drawWards();
  drawCharts();
  drawLand();
}
function setLang(lang) {
  state.lang = lang;
  try { localStorage.setItem("lang", lang); } catch { /* private mode */ }
  syncUrl();
  applyStaticText();
  fillSiteSelect();
  renderAll(false);
}

async function main() {
  const q = new URLSearchParams(location.search);
  let saved = null;
  try { saved = localStorage.getItem("lang"); } catch { /* private mode */ }
  state.lang = q.get("lang") || saved || "en";
  if (!STRINGS[state.lang]) state.lang = "en";
  state.site = q.get("site") || "all";
  applyStaticText();

  map = L.map("map", { scrollWheelZoom: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, className: "osm-tiles", attribution: "© OpenStreetMap contributors" }).addTo(map);

  document.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => setLang(b.dataset.lang)));
  document.querySelectorAll("[data-list]").forEach((b) => b.addEventListener("click", () => { state.list = b.dataset.list; renderAll(false); }));
  document.querySelectorAll("[data-show]").forEach((b) => b.addEventListener("click", () => { state.show = b.dataset.show; renderAll(false); }));
  $("site").addEventListener("change", (e) => { state.site = e.target.value; syncUrl(); renderAll(true); });
  document.addEventListener("click", async (e) => {  // copy buttons in both lists
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = t("rs_copied"); }
    catch { b.textContent = b.dataset.copy; }
    setTimeout(() => (b.textContent = t("rs_copy")), 1500);
  });

  index = await fetch("data/roadside-index.json", { cache: "no-cache" }).then((r) => r.json());
  const loaded = await Promise.all(index.map((s) => fetch(`data/roadside-${s.id}.json`, { cache: "no-cache" })
    .then((r) => (r.ok ? r.json() : null)).catch(() => null)));
  loaded.forEach((d, i) => { if (d) sites[index[i].id] = d; });
  // Land change is optional per site (it needs a few years of free satellite images).
  const landData = await Promise.all(index.map((s) => fetch(`data/roadside-${s.id}-land.json`, { cache: "no-cache" })
    .then((r) => (r.ok ? r.json() : null)).catch(() => null)));
  landData.forEach((d, i) => { if (d) lands[index[i].id] = d; });
  index = index.filter((s) => sites[s.id]);
  if (!index.length) throw new Error(t("rs_no_data"));
  fillSiteSelect();
  renderAll(true);
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { drawCharts(); drawLand(); }, 150); });
}

main().catch((e) => { $("meta").textContent = t("load_error") + e.message; });
