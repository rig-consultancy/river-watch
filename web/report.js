// Monthly report: one printable page per river, month and municipal area, built from the same
// data/<river>.json as the dashboard. ?river=&area=&month=YYYY-MM&lang= select it.

const $ = (id) => document.getElementById(id);
const state = { lang: "en", region: "all", month: null };
let data, index;

const locale = () => (state.lang === "mr" ? "mr-IN" : "en-IN");
function t(key, vars = {}) {
  const s = STRINGS[state.lang][key] ?? STRINGS.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
}
const nf = (v, d = 1) => (v == null || Number.isNaN(v) ? "–" : new Intl.NumberFormat(locale(), { maximumFractionDigits: d }).format(v));
const fmtDate = (d) => new Date(d + "T00:00:00").toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric" });
const fmtMonth = (m) => new Date(m + "-01T00:00:00").toLocaleDateString(locale(), { month: "long", year: "numeric" });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
const shiftMonth = (m, n) => { const d = new Date(m + "-01T00:00:00"); d.setMonth(d.getMonth() + n); return d.toISOString().slice(0, 7); };

const regionOf = (id) => data.regions.find((r) => r.id === id);
const regionName = (r) => (r.id === "other" ? t("outside") : state.lang === "mr" ? r.name_mr || r.name : r.name);
const inRegion = (p) => state.region === "all" || (state.region === "other" ? !p.regions.length : p.regions.includes(state.region));
function segName(p) {
  const place = state.lang === "mr" ? p.place_mr || p.place : p.place;
  if (place) return t("near", { place });
  const n = p.nearest;
  if (n) return t("from_place", { km: nf(n.km), place: state.lang === "mr" ? n.name_mr || n.name : n.name });
  return t("stretch", { n: nf(p.id + 1, 0) });
}

// River-wide (or area) rows for a month, and the month's average.
function monthRows(m) {
  const rows = state.region === "all" ? data.series : regionOf(state.region)?.series || [];
  return rows.filter((r) => r.date.startsWith(m));
}
const monthMean = (m) => mean(monthRows(m).map((r) => r.hyacinth_pct));

// Average of each stretch over the month's images (one-off jumps excluded).
function stretchMeans(m) {
  const sh = data.segment_history, jumps = new Set((sh.one_off || []).map(([i, k]) => `${i}:${k}`));
  const idx = sh.dates.map((d, i) => (d.startsWith(m) ? i : -1)).filter((i) => i >= 0);
  return data.segments.features.map((f) => f.properties).filter((p) => p.measurable && inRegion(p)).map((p) => {
    const v = idx.map((i) => (jumps.has(`${i}:${p.id}`) ? null : sh.hyacinth_pct[i][p.id])).filter((x) => x != null);
    return { p, v: mean(v), n: v.length };
  }).filter((s) => s.v != null);
}

// The river drawn as a line coloured by each stretch's monthly average (prints without map tiles).
function riverSvg(means) {
  const byId = new Map(means.map((s) => [s.p.id, s.v]));
  const feats = data.segments.features;
  const pts = feats.flatMap((f) => f.geometry.coordinates);
  const [x0, x1] = [Math.min(...pts.map((c) => c[0])), Math.max(...pts.map((c) => c[0]))];
  const [y0, y1] = [Math.min(...pts.map((c) => c[1])), Math.max(...pts.map((c) => c[1]))];
  const W = 640, H = Math.round(W * (y1 - y0) / (x1 - x0) / Math.cos((y0 + y1) / 2 * Math.PI / 180)) || 300;
  const sx = (x) => ((x - x0) / (x1 - x0)) * (W - 20) + 10, sy = (y) => H - 10 - ((y - y0) / (y1 - y0)) * (H - 20);
  const color = (v) => (v == null ? "#c9c9c4" : v < 5 ? "#8d9196" : v < 15 ? "#ec8a5e" : v < 30 ? "#e0602c" : v < 50 ? "#b8451a" : "#7f2c0e");
  const lines = feats.map((f) => {
    const v = byId.get(f.properties.id), mine = inRegion(f.properties);
    const d = f.geometry.coordinates.map((c, i) => `${i ? "L" : "M"}${sx(c[0]).toFixed(1)},${sy(c[1]).toFixed(1)}`).join("");
    return `<path d="${d}" stroke="${mine ? color(v) : "#e2e1dc"}" stroke-width="${v >= 5 && mine ? 5 : 3}" fill="none" stroke-linecap="round"/>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H + 10}" class="river-svg" role="img" aria-label="${esc(t("r_map"))}">${lines}</svg>
    <div class="legend">${[["#8d9196", "<5%"], ["#ec8a5e", "5–15%"], ["#e0602c", "15–30%"], ["#b8451a", "30–50%"], ["#7f2c0e", "50%+"], ["#c9c9c4", t("r_no_view")]]
      .map(([c, l]) => `<span><i class="sw" style="background:${c}"></i>${l}</span>`).join("")}</div>`;
}

function render() {
  const m = state.month;
  const area = state.region === "all" ? t("all_areas") : regionName(regionOf(state.region));
  const river = state.lang === "mr" ? data.river.name_mr || data.river.name : data.river.name;
  const rows = monthRows(m), now = monthMean(m), prev = monthMean(shiftMonth(m, -1)), ly = monthMean(shiftMonth(m, -12));
  const ranges = state.region === "all" ? rows.filter((r) => r.range) : [];
  const lo = ranges.length ? mean(ranges.map((r) => r.range[0])) : null, hi = ranges.length ? mean(ranges.map((r) => r.range[1])) : null;
  const means = stretchMeans(m);
  const top = means.filter((s) => s.p.confidence !== "low").sort((a, b) => b.v - a.v).slice(0, 10);
  const inMonth = (d) => d && d.startsWith(m);
  const ids = new Set(means.map((s) => s.p.id));
  const events = (data.cleanup_events || []).filter((e) => inMonth(e.to) && (state.region === "all" || ids.has(e.segment)));
  const segById = new Map(data.segments.features.map((f) => [f.properties.id, f.properties]));
  const rejected = (data.rejected_dates || []).filter((r) => inMonth(r.date));
  const delta = (a, b) => (a == null || b == null ? "–" : `${a - b > 0 ? "+" : ""}${nf(a - b)}`);
  const look = data.outlook;

  $("sheet").innerHTML = `
    <header class="r-head">
      <div><h1>${esc(t("r_title", { river }))}</h1><p class="sub">${esc(area)} · ${esc(fmtMonth(m))}</p></div>
      <p class="r-gen">${esc(t("r_generated", { date: fmtDate(data.latest_date) }))}</p>
    </header>
    ${rows.length ? "" : `<p class="r-warn">${esc(t("r_no_images"))}</p>`}
    <section class="tiles r-tiles">
      <div class="tile"><div class="tile-label">${t("r_avg")}</div><div class="tile-value">${now == null ? "–" : nf(now) + "%"}</div>
        <div class="tile-note">${lo == null ? t("n_pct") : t("likely", { lo: nf(lo), hi: nf(hi) })} · ${t("r_images", { n: nf(rows.length, 0) })}</div></div>
      <div class="tile"><div class="tile-label">${t("r_vs_prev")}</div><div class="tile-value">${delta(now, prev)}</div>
        <div class="tile-note">${prev == null ? t("r_no_data") : t("r_was", { pct: nf(prev) })}</div></div>
      <div class="tile"><div class="tile-label">${t("t_year")}</div><div class="tile-value">${delta(now, ly)}</div>
        <div class="tile-note">${ly == null ? t("r_no_data") : t("r_was", { pct: nf(ly) })}</div></div>
    </section>

    <section class="r-block"><h2>${t("r_map")}</h2>${riverSvg(means)}</section>

    <section class="r-block"><h2>${t("r_top")}</h2>
      ${top.length ? `<table><thead><tr><th>#</th><th>${t("r_stretch")}</th><th>${t("f_ward")}</th><th>${t("r_avg_short")}</th><th>${t("r_latest")}</th><th>${t("f_conf")}</th></tr></thead><tbody>
      ${top.map((s, i) => `<tr><td>${nf(i + 1, 0)}</td><td>${esc(segName(s.p))}</td><td>${esc(s.p.wards.join(" / "))}</td>
        <td>${nf(s.v)}%</td><td>${nf(s.p.hyacinth_pct)}% <span class="muted">${t("likely", { lo: nf(s.p.range[0]), hi: nf(s.p.range[1]) })}</span></td>
        <td>${t(`conf_${s.p.confidence}`)}</td></tr>`).join("")}</tbody></table>
        <p class="hint">${t("r_top_note")}</p>` : `<p>${t("r_no_data")}</p>`}
    </section>

    <section class="r-block"><h2>${t("r_events")}</h2>
      ${events.length ? `<table><thead><tr><th>${t("r_stretch")}</th><th>${t("r_when")}</th><th>${t("r_change")}</th><th>${t("r_cause")}</th><th>${t("r_regrowth")}</th></tr></thead><tbody>
      ${events.map((e) => `<tr><td>${esc(segName(segById.get(e.segment)))}</td><td>${fmtDate(e.from)} → ${fmtDate(e.to)}</td>
        <td>${nf(e.before)}% → ${nf(e.after)}%</td><td>${t(`cause_${e.cause}`)}${e.rain_mm != null ? ` <span class="muted">(${t("r_rain", { mm: nf(e.rain_mm, 0) })})</span>` : ""}</td>
        <td>${e.regrowth_days == null ? t("r_not_yet") : t("r_days", { n: nf(e.regrowth_days, 0) })}</td></tr>`).join("")}</tbody></table>`
        : `<p>${t("r_no_events")}</p>`}
      <p class="hint">${t("r_events_note")}</p>
    </section>

    ${look && !look.monsoon && look.river && m === data.latest_date.slice(0, 7) ? `<section class="r-block"><h2>${t("t_outlook")}</h2>
      <p>${t("n_outlook", { lo: nf(look.river.low), hi: nf(look.river.high), date: fmtDate(look.target_date) })} (${t("r_outlook_err", { e: nf(look.mae_river) })})</p></section>` : ""}

    <section class="r-block"><h2>${t("r_quality")}</h2>
      <p>${t("r_quality_text", { n: nf(rows.length, 0), rej: nf(rejected.length, 0) })}
        ${rejected.map((r) => `${fmtDate(r.date)}: ${t(`rejected_${r.reason}`, { aot: nf(r.aot, 2), c: nf((r.cloud_share || 0) * 100, 0), pct: nf(r.hyacinth_pct) })}`).join(" ")}</p>
      <p class="hint">${t("r_method")}</p>
      <p class="credits">${t("credits")}</p>
    </section>`;
}

function fillControls() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll("[data-lang]").forEach((b) => b.classList.toggle("on", b.dataset.lang === state.lang));
  $("river").innerHTML = index.rivers.map((r) => `<option value="${r.id}">${esc(state.lang === "mr" ? r.name_mr || r.name : r.name)}</option>`).join("");
  $("river").value = data.river.id;
  $("region").innerHTML = `<option value="all">${t("all_areas")}</option>` +
    data.regions.map((r) => `<option value="${r.id}">${esc(regionName(r))}</option>`).join("");
  $("region").value = state.region;
  const months = [...new Set(data.series.map((s) => s.date.slice(0, 7)))].sort().reverse();
  $("month").innerHTML = months.map((mm) => `<option value="${mm}">${esc(fmtMonth(mm))}</option>`).join("");
  if (!months.includes(state.month)) state.month = months[0];
  $("month").value = state.month;
  document.title = t("r_title", { river: state.lang === "mr" ? data.river.name_mr || data.river.name : data.river.name });
}

function sync() {
  const months = data.series.map((s) => s.date.slice(0, 7));
  if (!months.includes(state.month)) state.month = months[months.length - 1];  // before it goes in the URL
  const q = new URLSearchParams({ river: data.river.id, month: state.month });
  if (state.region !== "all") q.set("area", state.region);
  if (state.lang !== "en") q.set("lang", state.lang);
  history.replaceState(null, "", `?${q}`);
  fillControls();
  render();
}

async function load(id) {
  data = await fetch(`data/${id}.json`, { cache: "no-cache" }).then((r) => r.json());
  data.regions ??= [];
  if (!data.regions.some((r) => r.id === state.region)) state.region = "all";
}

async function main() {
  const q = new URLSearchParams(location.search);
  let saved = null;
  try { saved = localStorage.getItem("lang"); } catch { /* private mode */ }
  state.lang = STRINGS[q.get("lang") || saved] ? q.get("lang") || saved : "en";
  state.region = q.get("area") || "all";
  state.month = q.get("month");
  index = await fetch("data/index.json", { cache: "no-cache" }).then((r) => r.json());
  const wanted = q.get("river");
  await load(index.rivers.some((r) => r.id === wanted) ? wanted : index.rivers[0].id);
  $("river").addEventListener("change", async (e) => { await load(e.target.value); sync(); });
  $("region").addEventListener("change", (e) => { state.region = e.target.value; sync(); });
  $("month").addEventListener("change", (e) => { state.month = e.target.value; sync(); });
  document.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => { state.lang = b.dataset.lang; sync(); }));
  $("print").addEventListener("click", () => print());
  sync();
}

main().catch((e) => { $("sheet").innerHTML = `<p class="meta">${esc(t("load_error"))}${esc(e.message)}</p>`; });
