// PCMC Land Watch. Reads data/city-index.json and data/city-<id>.json written by pipeline/city_land.py.

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const tip = $("tip");
const LIST_MAX = 60;

let map, wardLayer, patchLayer, index, data;
const state = { lang: "en", city: null, ward: null };

// ---------- language ----------
const locale = () => (state.lang === "mr" ? "mr-IN" : "en-IN");
function t(key, vars = {}) {
  const s = STRINGS[state.lang][key] ?? STRINGS.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
}
const nf = (v, d = 0) => (v == null ? "–" : new Intl.NumberFormat(locale(), { maximumFractionDigits: d }).format(v));
const fmtDate = (d) => new Date(d + "T00:00:00").toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric" });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const ha = (m2) => t("rs_ha", { n: nf(m2 / 1e4, 1) });

function applyStaticText() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll("[data-i18n-html]").forEach((el) => (el.innerHTML = t(el.dataset.i18nHtml)));
  document.querySelectorAll("[data-lang]").forEach((b) => b.classList.toggle("on", b.dataset.lang === state.lang));
  document.title = t("cw_title");
}

// Years with at least two post-monsoon images count as complete (October alone is too early to judge).
const fullYears = () => data.years.filter((y, i) => data.post_images[i] >= 2);
const greenAt = (row, y) => row.green_pct[data.years.indexOf(y)];

// ---------- tiles ----------
function drawTiles() {
  const big = data.patches[0];
  $("t-bare").textContent = ha(data.changed_m2_total);
  $("t-bare-note").textContent = t("cw_n_bare", { n: nf(data.patches_total), from: data.before_years[0], to: data.before_years[1] });
  $("t-big").textContent = big ? t("cw_m2", { n: nf(big.area_m2) }) : "–";
  $("t-big-note").textContent = big ? `${big.ward || ""} · ${t("cw_since", { y: big.first_bare_year ?? "–" })}` : "";
  const top = [...data.wards].sort((a, b) => b.changed_m2 / b.area_km2 - a.changed_m2 / a.area_km2)[0];
  $("t-ward").textContent = top ? top.name : "–";
  $("t-ward-note").textContent = top ? t("cw_n_ward", { ha: nf(top.changed_m2 / 1e4, 1), per: nf(top.changed_m2 / top.area_km2 / 1e4, 2) }) : "";
  const ys = fullYears(), ly = ys.at(-1), fy = ys[0];
  $("t-green").textContent = ly ? nf(greenAt(data, ly), 0) + "%" : "–";
  $("t-green-note").textContent = ly ? t("cw_n_green", { year: ly, then: nf(greenAt(data, fy), 0), first: fy }) : "";
}

// ---------- map ----------
function rampColor(v, max) {
  if (!v) return "transparent";
  const k = Math.min(3, Math.floor((v / max) * 4));
  return css(["--ramp-1", "--ramp-2", "--ramp-3", "--ramp-4"][k]);
}
const density = (w) => w.changed_m2 / w.area_km2;  // m² turned bare per km² of ward
const lastYear = () => data.years.at(-1);
const patchColor = (p) => (p.first_bare_year >= lastYear() - 1 ? css("--critical") : css("--series-3"));

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
function bindTip(l, html) {
  l.on("mouseover", (e) => showTip(html, e.originalEvent.clientX, e.originalEvent.clientY));
  l.on("mouseout", hideTip);
}
function patchTip(p) {
  return `<strong>${esc(t("cw_p_title", { n: nf(p.id), m2: nf(p.area_m2) }))}</strong><br>`
    + esc(t("rs_p_change", { before: nf(p.ndvi_before, 2), now: nf(p.ndvi_now, 2), year: p.first_bare_year ?? "–" }))
    + `<br>${esc(t("rs_ward", { w: p.ward || t("rs_no_ward") }))}`;
}

function drawMap(fit) {
  const max = Math.max(1, ...data.wards.map(density));
  if (wardLayer) map.removeLayer(wardLayer);
  wardLayer = L.featureGroup();
  for (const w of data.wards) {
    const on = state.ward === w.name;
    const g = L.geoJSON(w.geometry, { style: { color: on ? css("--accent") : css("--text-muted"), weight: on ? 3 : 1,
      fillColor: rampColor(density(w), max), fillOpacity: 0.45 } });
    g.eachLayer((l) => {
      bindTip(l, `<strong>${esc(w.name)}</strong><br>${esc(t("cw_tip_ward", { ha: nf(w.changed_m2 / 1e4, 1), n: nf(w.patches) }))}`);
      l.on("click", () => selectWard(state.ward === w.name ? null : w.name, false));
    });
    g.addTo(wardLayer);
  }
  wardLayer.addTo(map);
  if (patchLayer) map.removeLayer(patchLayer);
  patchLayer = L.featureGroup();
  for (const p of data.patches) {
    const g = L.geoJSON(p.geometry, { style: { color: patchColor(p), weight: 2, fillColor: patchColor(p), fillOpacity: 0.35 } });
    g.eachLayer((l) => bindTip(l, patchTip(p)));
    g.addTo(patchLayer);
  }
  patchLayer.addTo(map);
  map.invalidateSize();
  if (fit) map.fitBounds(state.ward ? L.geoJSON(data.wards.find((w) => w.name === state.ward).geometry).getBounds() : wardLayer.getBounds(), { padding: [20, 20] });
}

function drawLegend() {
  const ly = lastYear();
  $("legend").innerHTML = `<span>${esc(t("cw_legend_ward"))}</span>`
    + ["--ramp-1", "--ramp-2", "--ramp-3", "--ramp-4"].map((c) => `<span class="ramp-sw" style="background:${css(c)}"></span>`).join("")
    + `<span>${esc(t("cw_legend_more"))}</span>`
    + `<span><span class="patch-sw" style="border-color:${css("--critical")}"></span>${esc(t("cw_legend_new", { a: ly - 1, b: ly }))}</span>`
    + `<span><span class="patch-sw" style="border-color:${css("--series-3")}"></span>${esc(t("cw_legend_old", { y: ly - 1 }))}</span>`;
}

// ---------- list and table ----------
function drawList() {
  const rows = data.patches.filter((p) => !state.ward || p.ward === state.ward).slice(0, LIST_MAX);
  $("list-title").textContent = state.ward ? t("cw_list_ward", { w: state.ward }) : t("cw_list_all");
  $("spots").innerHTML = rows.length ? rows.map((p) => `<li>
      <span class="sp-dot" style="background:${patchColor(p)}"></span>
      <span class="sp-main"><span class="sp-ward">${esc(p.ward || t("rs_no_ward"))}</span>
      <span class="sp-line">${esc(t("rs_p_change", { before: nf(p.ndvi_before, 2), now: nf(p.ndvi_now, 2), year: p.first_bare_year ?? "–" }))}</span>
      <span class="sp-links"><a href="https://www.google.com/maps?q=${p.lat},${p.lon}" target="_blank" rel="noopener">${esc(t("rs_open_map"))}</a>
        · <button type="button" class="linkish" data-copy="${p.lat}, ${p.lon}">${esc(t("rs_copy"))}</button>
        · <button type="button" class="linkish" data-zoom="${p.id}">${esc(t("cw_zoom"))}</button></span></span>
      <span class="sp-val">${esc(t("cw_m2", { n: nf(p.area_m2) }))}</span></li>`).join("")
    : `<li class="empty">${esc(t("rs_empty"))}</li>`;
}

function drawWards() {
  const ys = fullYears(), fy = ys[0], ly = ys.at(-1);
  $("w-green-then").textContent = t("cw_w_green", { y: fy });
  $("w-green-now").textContent = t("cw_w_green", { y: ly });
  const rows = [...data.wards].sort((a, b) => b.changed_m2 - a.changed_m2);
  $("wards").querySelector("tbody").innerHTML = rows.map((w) => `<tr data-ward="${esc(w.name)}" class="${state.ward === w.name ? "on" : ""}">
      <td>${esc(w.name)}</td><td class="num">${esc(t("cw_km2", { n: nf(w.area_km2, 1) }))}</td>
      <td class="num">${esc(ha(w.changed_m2))}</td><td class="num">${nf(w.patches)}</td>
      <td class="num">${nf(greenAt(w, fy), 0)}%</td><td class="num">${nf(greenAt(w, ly), 0)}%</td></tr>`).join("");
}

function drawChart() {
  const el = $("c-green"), rows = data.years.map((y, i) => ({ y, v: data.green_pct[i], partial: data.post_images[i] < 2 }));
  const W = el.clientWidth || 600, H = el.clientHeight || 220, pad = { l: 34, r: 10, t: 10, b: 26 };
  const top = Math.max(10, Math.ceil(Math.max(...rows.map((r) => r.v)) / 10) * 10);
  const x = (i) => pad.l + (i + 0.5) * (W - pad.l - pad.r) / rows.length;
  const y = (v) => H - pad.b - v / top * (H - pad.t - pad.b);
  const bw = Math.min(36, (W - pad.l - pad.r) / rows.length * 0.6);
  const ticks = [0, top / 2, top];
  el.innerHTML = `<svg width="${W}" height="${H}" role="img" aria-label="${esc(t("cw_c_green"))}">`
    + ticks.map((v) => `<line class="grid" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/><text class="tick" x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${nf(v)}%</text>`).join("")
    + rows.map((r, i) => `<rect class="bar" x="${x(i) - bw / 2}" y="${y(r.v)}" width="${bw}" height="${H - pad.b - y(r.v)}" rx="2" style="fill:${css("--good")};opacity:${r.partial ? 0.45 : 1}"><title>${r.y}: ${nf(r.v, 1)}%${r.partial ? " " + esc(t("cw_partial")) : ""}</title></rect>`
      + `<text class="tick" x="${x(i)}" y="${H - 8}" text-anchor="middle">${r.y}${r.partial ? "*" : ""}</text>`).join("")
    + "</svg>";
}

// ---------- page ----------
function selectWard(name, fit) {
  state.ward = name;
  $("show-all").hidden = !name;
  syncUrl();
  drawMap(fit);
  drawList();
  drawWards();
}
function syncUrl() {
  const q = new URLSearchParams();
  if (index.length > 1) q.set("city", state.city);
  if (state.ward) q.set("ward", state.ward);
  if (state.lang !== "en") q.set("lang", state.lang);
  history.replaceState(null, "", q.toString() ? `?${q}` : location.pathname);
}
function renderAll(fit) {
  $("meta").textContent = t("cw_meta", { n: nf(data.images), from: fmtDate(data.first), to: fmtDate(data.last), km2: nf(data.area_km2, 0) });
  drawTiles();
  drawMap(fit);
  drawLegend();
  drawList();
  drawWards();
  drawChart();
}
function fillCitySelect() {
  $("city").innerHTML = index.map((c) => `<option value="${c.id}">${esc(state.lang === "mr" ? c.name_mr || c.name : c.name)}</option>`).join("");
  $("city").value = state.city;
}
async function loadCity(id) {
  state.city = id;
  data = await fetch(`data/city-${id}.json`, { cache: "no-cache" }).then((r) => r.json());
  if (state.ward && !data.wards.some((w) => w.name === state.ward)) state.ward = null;
  $("show-all").hidden = !state.ward;
  renderAll(true);
}

async function main() {
  const q = new URLSearchParams(location.search);
  let saved = null;
  try { saved = localStorage.getItem("lang"); } catch { /* private mode */ }
  state.lang = q.get("lang") || saved || "en";
  if (!STRINGS[state.lang]) state.lang = "en";
  state.ward = q.get("ward");
  applyStaticText();

  // A starting view first: fitting bounds on a map that has none yet can leave it fully zoomed in.
  map = L.map("map", { scrollWheelZoom: false }).setView([18.64, 73.82], 11);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, className: "osm-tiles", attribution: "© OpenStreetMap contributors" }).addTo(map);

  document.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => {
    state.lang = b.dataset.lang;
    try { localStorage.setItem("lang", state.lang); } catch { /* private mode */ }
    applyStaticText(); fillCitySelect(); syncUrl(); renderAll(false);
  }));
  $("city").addEventListener("change", (e) => { state.ward = null; loadCity(e.target.value).then(syncUrl); });
  $("show-all").addEventListener("click", () => selectWard(null, true));
  $("wards").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-ward]");
    if (tr) selectWard(state.ward === tr.dataset.ward ? null : tr.dataset.ward, true);
  });
  document.addEventListener("click", async (e) => {
    const z = e.target.closest("[data-zoom]");
    if (z) {
      const p = data.patches.find((x) => x.id === +z.dataset.zoom);
      map.fitBounds(L.geoJSON(p.geometry).getBounds(), { maxZoom: 17, padding: [60, 60] });
      $("map").scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = t("rs_copied"); }
    catch { b.textContent = b.dataset.copy; }
    setTimeout(() => (b.textContent = t("rs_copy")), 1500);
  });

  index = await fetch("data/city-index.json", { cache: "no-cache" }).then((r) => r.json());
  if (!index.length) throw new Error(t("cw_no_data"));
  state.city = index.some((c) => c.id === q.get("city")) ? q.get("city") : index[0].id;
  fillCitySelect();
  await loadCity(state.city);
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawChart, 150); });
}

main().catch((e) => { $("meta").textContent = t("load_error") + e.message; });
