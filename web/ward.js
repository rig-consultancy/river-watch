// Ward report: one printable page per municipal ward along a river, built from the same
// data/<river>.json as the dashboard (its "wards" rows and the stretches in each). ?river=&ward=&lang=
// select it; ward is "<corporation>:<ward name>", as in the dashboard's ward picker.

const $ = (id) => document.getElementById(id);
const state = { lang: "en", ward: null };
let data, index;

const locale = () => (state.lang === "mr" ? "mr-IN" : "en-IN");
function t(key, vars = {}) {
  const s = STRINGS[state.lang][key] ?? STRINGS.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
}
const nf = (v, d = 1) => (v == null || Number.isNaN(v) ? "–" : new Intl.NumberFormat(locale(), { maximumFractionDigits: d }).format(v));
const fmtDate = (d) => new Date(d + "T00:00:00").toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric" });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const pct = (v) => (v == null ? "–" : `${nf(v)}%`);
const keyOf = (w) => `${w.region}:${w.ward}`;
const regionName = (id) => { const r = data.regions.find((x) => x.id === id); return r ? (state.lang === "mr" ? r.name_mr || r.name : r.name) : id; };
const riverName = () => (state.lang === "mr" ? data.river.name_mr || data.river.name : data.river.name);
function segName(p) {
  const place = state.lang === "mr" ? p.place_mr || p.place : p.place;
  if (place) return t("near", { place });
  const n = p.nearest;
  if (n) return t("from_place", { km: nf(n.km), place: state.lang === "mr" ? n.name_mr || n.name : n.name });
  return t("stretch", { n: nf(p.id + 1, 0) });
}
const color = (v) => (v == null ? "#c9c9c4" : v < 5 ? "#4b9bc8" : v < 15 ? "#ec8a5e" : v < 30 ? "#e0602c" : v < 50 ? "#b8451a" : "#7f2c0e");

// The ward's cover on every image of the last 12 months, weighted by river area like the table.
function wardSeries(w, props) {
  const sh = data.segment_history, last = new Date(data.latest_date + "T00:00:00").getTime();
  const total = w.segments.reduce((s, k) => s + (props.get(k).area_ha || 0), 0);
  return sh.dates.map((d, i) => {
    if (last - new Date(d + "T00:00:00").getTime() > 365 * 864e5) return null;
    let sum = 0, seen = 0;
    w.segments.forEach((k) => { const v = sh.hyacinth_pct[i][k], a = props.get(k).area_ha || 0; if (v != null) { sum += v * a; seen += a; } });
    return total && seen >= 0.5 * total ? { date: d, v: sum / seen } : null;
  }).filter(Boolean);
}

function trendSvg(rows) {
  if (rows.length < 2) return `<p>${t("r_no_data")}</p>`;
  const W = 640, H = 180, l = 40, r = 10, top = 10, b = 24;
  const t0 = new Date(rows[0].date).getTime(), t1 = new Date(rows.at(-1).date).getTime();
  const vmax = Math.max(10, Math.ceil(Math.max(...rows.map((p) => p.v)) / 10) * 10);
  const x = (d) => l + ((new Date(d).getTime() - t0) / Math.max(t1 - t0, 1)) * (W - l - r), y = (v) => top + (1 - v / vmax) * (H - top - b);
  const path = rows.map((p, i) => `${i ? "L" : "M"}${x(p.date).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const ticks = [0, vmax / 2, vmax];
  return `<svg viewBox="0 0 ${W} ${H}" class="river-svg" role="img" aria-label="${esc(t("wr_trend"))}">
    ${ticks.map((v) => `<line x1="${l}" x2="${W - r}" y1="${y(v)}" y2="${y(v)}" style="stroke:var(--grid)"/><text x="${l - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="#777">${nf(v, 0)}%</text>`).join("")}
    <path d="${path}" fill="none" stroke="#2a78d6" stroke-width="2"/>
    ${rows.map((p) => `<circle cx="${x(p.date)}" cy="${y(p.v)}" r="2.5" fill="#2a78d6"><title>${fmtDate(p.date)}: ${nf(p.v)}%</title></circle>`).join("")}
    <text x="${l}" y="${H - 6}" font-size="11" fill="#777">${fmtDate(rows[0].date)}</text>
    <text x="${W - r}" y="${H - 6}" font-size="11" fill="#777" text-anchor="end">${fmtDate(rows.at(-1).date)}</text></svg>`;
}

// The whole river, the ward's stretches coloured by their latest reading, the rest grey.
function mapSvg(w) {
  const mine = new Set(w.segments), feats = data.segments.features;
  const pts = feats.flatMap((f) => f.geometry.coordinates);
  const [x0, x1] = [Math.min(...pts.map((c) => c[0])), Math.max(...pts.map((c) => c[0]))];
  const [y0, y1] = [Math.min(...pts.map((c) => c[1])), Math.max(...pts.map((c) => c[1]))];
  const W = 640, H = Math.round(W * (y1 - y0) / (x1 - x0) / Math.cos((y0 + y1) / 2 * Math.PI / 180)) || 300;
  const sx = (v) => ((v - x0) / (x1 - x0)) * (W - 20) + 10, sy = (v) => H - 10 - ((v - y0) / (y1 - y0)) * (H - 20);
  const lines = feats.map((f) => {
    const p = f.properties, own = mine.has(p.id);
    const d = f.geometry.coordinates.map((c, i) => `${i ? "L" : "M"}${sx(c[0]).toFixed(1)},${sy(c[1]).toFixed(1)}`).join("");
    // Other wards in the theme's quiet border colour (white on a dark screen would drown the ward).
    return `<path d="${d}" style="stroke:${own ? color(p.measurable ? p.hyacinth_pct : null) : "var(--border)"}" stroke-width="${own ? 6 : 2.5}" fill="none" stroke-linecap="round"/>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H + 10}" class="river-svg" role="img" aria-label="${esc(t("r_map"))}">${lines}</svg>
    <div class="legend">${[["#4b9bc8", "<5%"], ["#ec8a5e", "5–15%"], ["#e0602c", "15–30%"], ["#b8451a", "30–50%"], ["#7f2c0e", "50%+"], ["#c9c9c4", t("r_no_view")], ["var(--border)", t("wr_other_wards")]]
      .map(([c, lab]) => `<span><i class="sw" style="background:${c}"></i>${lab}</span>`).join("")}</div>`;
}

function render() {
  const w = data.wards.find((x) => keyOf(x) === state.ward);
  if (!w) { $("sheet").innerHTML = `<p>${t("r_no_data")}</p>`; return; }
  const props = new Map(data.segments.features.map((f) => [f.properties.id, f.properties]));
  const ranked = data.wards.filter((x) => x.ranked && x.now != null).sort((a, b) => b.now - a.now);
  const rank = ranked.findIndex((x) => keyOf(x) === state.ward);
  const change = w.now == null || w.last_year == null ? null : w.now - w.last_year;
  const yearAgo = new Date(new Date(data.latest_date).getTime() - 365 * 864e5).toISOString().slice(0, 10);
  const ops = new Map();
  (data.cleanup_events || []).filter((e) => e.cause === "removal" && w.segments.includes(e.segment) && e.to > yearAgo)
    .forEach((e) => { if (!ops.has(e.operation)) ops.set(e.operation, []); ops.get(e.operation).push(e); });
  const stretches = w.segments.map((k) => props.get(k));

  $("sheet").innerHTML = `
    <header class="r-head">
      <div><h1>${esc(t("wr_title", { ward: w.ward }))}</h1>
        <p class="sub">${esc(regionName(w.region))} · ${esc(riverName())} · ${t("km_val", { km: nf(w.km) })}</p></div>
      <p class="r-gen">${esc(t("r_generated", { date: fmtDate(data.latest_date) }))}</p>
    </header>
    ${w.ranked ? "" : `<p class="r-warn">${t("wr_low_conf", { p: nf(w.reliable * 100, 0) })}</p>`}
    <section class="tiles r-tiles">
      <div class="tile"><div class="tile-label">${t("w_now")}</div><div class="tile-value">${pct(w.now)}</div>
        <div class="tile-note">${rank >= 0 ? t("wr_rank", { r: nf(rank + 1, 0), n: nf(ranked.length, 0) }) : t("wr_not_ranked")}</div></div>
      <div class="tile"><div class="tile-label">${t("w_last_year")}</div><div class="tile-value">${pct(w.last_year)}</div>
        <div class="tile-note">${change == null ? "–" : `${t("w_change")}: ${change > 0 ? "+" : ""}${t("pts", { v: nf(change) })}`}</div></div>
      <div class="tile"><div class="tile-label">${t("w_avg")}</div><div class="tile-value">${pct(w.avg_12m)}</div>
        <div class="tile-note">${t("w_cleanups")}: ${nf(w.cleanups_12m, 0)}${w.confirmed_12m ? ` (${t("w_confirmed", { n: nf(w.confirmed_12m, 0) })})` : ""}</div></div>
    </section>

    <section class="r-block"><h2>${t("wr_map")}</h2>${mapSvg(w)}</section>
    <section class="r-block"><h2>${t("wr_trend")}</h2>${trendSvg(wardSeries(w, props))}</section>

    <section class="r-block"><h2>${t("wr_stretches")}</h2>
      <table><thead><tr><th>${t("r_stretch")}</th><th>${t("w_now")}</th><th>${t("r_latest")}</th><th>${t("f_conf")}</th><th>${t("wr_notes")}</th></tr></thead><tbody>
      ${stretches.map((p) => `<tr><td>${esc(segName(p))}</td>
        <td>${p.measurable ? `<strong>${pct(p.hyacinth_pct)}</strong> <span class="muted">${t("likely", { lo: nf(p.range[0]), hi: nf(p.range[1]) })}</span>` : "–"}</td>
        <td>${p.measurable ? fmtDate(p.date) : t(`why_${p.why}`)}</td><td>${p.measurable ? t(`conf_${p.confidence}`) : "–"}</td>
        <td>${[p.muddier ? t("muddier_flag") : "", p.flood_test ? t(`flood_${p.flood_test.verdict}`) : ""].filter(Boolean).map(esc).join("; ") || "–"}</td></tr>`).join("")}
      </tbody></table></section>

    <section class="r-block"><h2>${t("wr_events")}</h2>
      ${ops.size ? `<table><thead><tr><th>${t("r_when")}</th><th>${t("wr_where")}</th><th>${t("r_change")}</th><th>${t("r_regrowth")}</th></tr></thead><tbody>
      ${[...ops.values()].sort((a, b) => (a[0].to < b[0].to ? 1 : -1)).map((op) => {
        const e = op.reduce((a, x) => (x.before - x.after > a.before - a.after ? x : a));
        const regrow = op.map((x) => x.regrowth_days).filter((x) => x != null);
        return `<tr><td>${fmtDate(e.from)} → ${fmtDate(e.to)}</td><td>${esc(segName(props.get(e.segment)))}${op.length > 1 ? ` ${t("wr_and_more", { n: nf(op.length - 1, 0) })}` : ""}
          ${e.confirmed_cleanup ? `<br><strong>${t("confirmed_cleanup")}</strong>` : ""}</td>
          <td>${nf(e.before)}% → ${nf(e.after)}%</td><td>${regrow.length ? t("r_days", { n: nf(Math.min(...regrow), 0) }) : t("r_not_yet")}</td></tr>`;
      }).join("")}</tbody></table>` : `<p>${t("r_no_data")}</p>`}
      <p class="hint">${t("r_events_note")}</p></section>

    <section class="r-block"><h2>${t("r_quality")}</h2>
      <p class="hint">${t("w_note")}</p><p class="hint">${t("r_method")}</p>
      <p class="credits">${t("credits")}</p></section>`;
}

function fillControls() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll("[data-lang]").forEach((b) => b.classList.toggle("on", b.dataset.lang === state.lang));
  $("river").innerHTML = index.rivers.map((r) => `<option value="${r.id}">${esc(state.lang === "mr" ? r.name_mr || r.name : r.name)}</option>`).join("");
  $("river").value = data.river.id;
  const groups = [...new Set(data.wards.map((w) => w.region))];
  $("ward").innerHTML = groups.map((g) => `<optgroup label="${esc(regionName(g))}">${data.wards.filter((w) => w.region === g)
    .map((w) => `<option value="${esc(keyOf(w))}">${esc(w.ward)}</option>`).join("")}</optgroup>`).join("");
  $("ward").value = state.ward;
  const w = data.wards.find((x) => keyOf(x) === state.ward);
  document.title = w ? t("wr_title", { ward: w.ward }) : "River Watch";
}

function sync() {
  if (!data.wards.some((w) => keyOf(w) === state.ward)) {  // default: the worst ranked ward
    const top = data.wards.filter((w) => w.ranked && w.now != null).sort((a, b) => b.now - a.now)[0] || data.wards[0];
    state.ward = top ? keyOf(top) : null;
  }
  const q = new URLSearchParams({ river: data.river.id });
  if (state.ward) q.set("ward", state.ward);
  if (state.lang !== "en") q.set("lang", state.lang);
  history.replaceState(null, "", `?${q}`);
  fillControls();
  render();
}

async function load(id) {
  data = await fetch(`data/${id}.json`, { cache: "no-cache" }).then((r) => r.json());
  data.regions ??= [];
  data.wards ??= [];
}

async function main() {
  const q = new URLSearchParams(location.search);
  let saved = null;
  try { saved = localStorage.getItem("lang"); } catch { /* private mode */ }
  state.lang = STRINGS[q.get("lang") || saved] ? q.get("lang") || saved : "en";
  state.ward = q.get("ward");
  index = await fetch("data/index.json", { cache: "no-cache" }).then((r) => r.json());
  const wanted = q.get("river");
  await load(index.rivers.some((r) => r.id === wanted) ? wanted : index.rivers[0].id);
  $("river").addEventListener("change", async (e) => { await load(e.target.value); state.ward = null; sync(); });
  $("ward").addEventListener("change", (e) => { state.ward = e.target.value; sync(); });
  document.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => { state.lang = b.dataset.lang; sync(); }));
  $("print").addEventListener("click", () => print());
  sync();
}

main().catch((e) => { $("sheet").innerHTML = `<p class="meta">${esc(t("load_error"))}${esc(e.message)}</p>`; });
