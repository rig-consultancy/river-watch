// River Watch dashboard. Reads data/index.json and data/<river>.json written by the pipeline.

const BINS = [5, 15, 30, 50]; // hyacinth % thresholds; below the first is "clear"
const GAP_DAYS = 45; // longer gaps between clear images break the chart line
const tip = document.getElementById("tip");
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtDate = (d) => new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
const fmtShort = (d) => new Date(d + "T00:00:00").toLocaleDateString("en-IN", { month: "short", year: "2-digit" });
// Local YYYY-MM-DD for a timestamp (toISOString would shift to UTC and change the day in India).
const isoLocal = (t) => {
  const d = new Date(t);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

let map, segLayer, baseLayers = {}, current;

function binColor(pct) {
  if (pct == null || pct < BINS[0]) return css("--clear");
  if (pct < BINS[1]) return css("--ramp-1");
  if (pct < BINS[2]) return css("--ramp-2");
  if (pct < BINS[3]) return css("--ramp-3");
  return css("--ramp-4");
}

function segName(p) {
  return p.place ? `Near ${p.place}` : `Stretch ${p.id + 1}`;
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

  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".seg button").forEach((o) => o.classList.toggle("on", o === b));
    Object.values(baseLayers).forEach((l) => map.removeLayer(l));
    baseLayers[b.dataset.base].addTo(map);
  }));

  const legend = document.getElementById("legend");
  const items = [[css("--clear"), `Under ${BINS[0]}%`]]
    .concat(BINS.map((b, i) => [css(`--ramp-${i + 1}`), i < BINS.length - 1 ? `${b}–${BINS[i + 1]}%` : `${b}%+`]));
  legend.innerHTML = `<span>Hyacinth cover:</span>` +
    items.map(([c, t]) => `<span><i class="sw" style="background:${c}"></i>${t}</span>`).join("");
}

function drawSegments(data) {
  if (segLayer) map.removeLayer(segLayer);
  segLayer = L.geoJSON(data.segments, {
    style: (f) => {
      const p = f.properties.hyacinth_pct;
      return { color: binColor(p), weight: p >= BINS[0] ? 6 : 3, opacity: 0.95, lineCap: "round" };
    },
    onEachFeature: (f, layer) => {
      const p = f.properties;
      const change = p.change_pct == null ? "" :
        `<div>Change ~30 days: <span class="tt-val">${p.change_pct > 0 ? "+" : ""}${p.change_pct} pts</span></div>`;
      const html = `<div class="tt-title">${segName(p)}</div>
        <div>Hyacinth: <span class="tt-val">${p.hyacinth_pct}%</span> of water</div>${change}
        <div>Seen: ${fmtDate(p.date)}</div>`;
      layer.on("mousemove", (e) => showTip(html, e.originalEvent.clientX, e.originalEvent.clientY));
      layer.on("mouseout", hideTip);
      layer.on("click", (e) => showTip(html, e.originalEvent.clientX, e.originalEvent.clientY));
    },
  }).addTo(map);
  map.invalidateSize();
  map.fitBounds(segLayer.getBounds(), { padding: [16, 16] });
}

function drawHotspots(data) {
  const list = document.getElementById("hotspots");
  const feats = data.segments.features
    .filter((f) => f.properties.hyacinth_pct >= BINS[0])
    .sort((a, b) => b.properties.hyacinth_pct - a.properties.hyacinth_pct)
    .slice(0, 15);
  if (!feats.length) {
    list.innerHTML = `<li class="hint">No stretch is above ${BINS[0]}% right now.</li>`;
    return;
  }
  list.innerHTML = "";
  feats.forEach((f, i) => {
    const p = f.properties;
    const li = document.createElement("li");
    li.innerHTML = `<button type="button"><span class="rank">${i + 1}</span>
      <span class="hs-name">${segName(p)}</span><span class="hs-val">${p.hyacinth_pct}%</span>
      <span class="hs-bar"><span style="width:${Math.min(100, p.hyacinth_pct)}%;background:${binColor(p.hyacinth_pct)}"></span></span></button>`;
    li.querySelector("button").addEventListener("click", () => {
      const layer = L.geoJSON(f);
      map.fitBounds(layer.getBounds(), { maxZoom: 16, padding: [40, 40] });
      document.getElementById("map").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    list.appendChild(li);
  });
}

// ---------- line chart ----------
// layers: [{name, cls, rows, key, tip}] sharing one % axis; the first layer is the primary
// series (area fill). A legend is shown when there is more than one layer.
function lineChart(el, layers, label) {
  const toPts = (l) => l.rows.map((s) => ({ t: new Date(s.date + "T00:00:00").getTime(), v: s[l.key], d: s }));
  const L = layers.map((l) => ({ ...l, pts: toPts(l) })).filter((l) => l.pts.length);
  const legend = L.length > 1
    ? `<div class="chart-legend">${L.map((l) => `<span><i class="key ${l.cls}"></i>${l.name}</span>`).join("")}</div>` : "";
  const W = el.clientWidth, H = el.clientHeight - (legend ? 24 : 0);
  const m = { l: 36, r: 12, t: 10, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  if (!L.length || L[0].pts.length < 2) {
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}"><text class="empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">Collecting data — needs at least two clear dates</text></svg>`;
    return;
  }
  const all = L.flatMap((l) => l.pts);
  const t0 = Math.min(...all.map((p) => p.t)), t1 = Math.max(...all.map((p) => p.t));
  const vmax = niceMax(Math.max(...all.map((p) => p.v)));
  const x = (t) => m.l + ((t - t0) / (t1 - t0)) * iw;
  const y = (v) => m.t + ih - (v / vmax) * ih;

  const ticksY = [0, vmax / 2, vmax];
  const ticksX = monthTicks(t0, t1, Math.max(2, Math.floor(iw / 70)));
  const seg = (run) => run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const marks = L.map((l, li) => {
    // Break the line across long gaps (monsoon clouds) instead of drawing data that doesn't exist.
    const runs = [[l.pts[0]]];
    for (let i = 1; i < l.pts.length; i++) {
      if (l.pts[i].t - l.pts[i - 1].t > GAP_DAYS * 864e5) runs.push([]);
      runs[runs.length - 1].push(l.pts[i]);
    }
    const area = li ? "" : `<path class="area" d="${runs.filter((r) => r.length > 1)
      .map((r) => `${seg(r)}L${x(r[r.length - 1].t)},${y(0)}L${x(r[0].t)},${y(0)}Z`).join("")}"/>`;
    // Primary: dots only where a point stands alone. Others: a marker on every (sparser) estimate.
    const dots = li ? l.pts : runs.filter((r) => r.length === 1).map((r) => r[0]);
    const last = l.pts[l.pts.length - 1];
    return `${area}<path class="line ${l.cls}" d="${runs.map(seg).join("")}"/>
      ${dots.map((p) => `<circle class="dot ${l.cls}" cx="${x(p.t)}" cy="${y(p.v)}" r="${li ? 4 : 3}"/>`).join("")}
      ${li ? "" : `<circle class="dot ${l.cls}" cx="${x(last.t)}" cy="${y(last.v)}" r="4"/>`}`;
  }).join("");

  el.innerHTML = `${legend}<svg viewBox="0 0 ${W} ${H}" style="height:${H}px" role="img" aria-label="${label}">
    <g class="axis">${ticksY.map((v) => `<line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>
      <text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${+v.toFixed(1)}%</text>`).join("")}
      ${ticksX.map((t) => `<text x="${x(t)}" y="${H - 6}" text-anchor="middle">${fmtShort(isoLocal(t))}</text>`).join("")}
    </g>
    <line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}"/>
    ${marks}
    <g class="hover" style="display:none"><line class="cross" y1="${m.t}" y2="${m.t + ih}"/>
      ${L.map((l) => `<circle class="dot ${l.cls}" r="5"/>`).join("")}</g>
    <rect x="${m.l}" y="0" width="${iw}" height="${H}" fill="transparent"/>
  </svg>`;

  const svg = el.querySelector("svg"), hover = svg.querySelector(".hover");
  const rect = svg.querySelector("rect"), rings = hover.querySelectorAll("circle");
  const nearest = (pts, t) => pts.reduce((a, p) => (Math.abs(p.t - t) < Math.abs(a.t - t) ? p : a));
  const move = (ev) => {
    const b = svg.getBoundingClientRect();
    const t = t0 + ((((ev.clientX - b.left) / b.width) * W - m.l) / iw) * (t1 - t0);
    const at = nearest(all, t).t;
    hover.style.display = "";
    hover.querySelector("line").setAttribute("x1", x(at));
    hover.querySelector("line").setAttribute("x2", x(at));
    const rowsHtml = [];
    L.forEach((l, i) => {
      const p = nearest(l.pts, at);
      const show = Math.abs(p.t - at) <= 6 * 864e5;
      rings[i].style.display = show ? "" : "none";
      if (!show) return;
      rings[i].setAttribute("cx", x(p.t));
      rings[i].setAttribute("cy", y(p.v));
      const name = L.length > 1 ? `<i class="key ${l.cls}"></i>${l.name}` : label;
      const when = p.t !== at ? ` (${fmtDate(p.d.date)})` : "";
      rowsHtml.push(`<div>${name}: <span class="tt-val">${p.v}%</span>${when}</div>${l.tip ? l.tip(p.d) : ""}`);
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

// ---------- tiles + table ----------
function drawTiles(data) {
  const s = data.series, last = s[s.length - 1];
  document.getElementById("t-pct").textContent = `${last.hyacinth_pct}%`;
  const radarLast = data.radar?.series.at(-1);
  document.getElementById("t-pct-note").textContent = radarLast && radarLast.date > last.date
    ? `optical, ${fmtDate(last.date)} · radar ${fmtDate(radarLast.date)}: ~${radarLast.hyacinth_pct}%`
    : "of river surface";
  document.getElementById("t-area").textContent = last.hyacinth_km2.toFixed(2);
  document.getElementById("t-turbid").textContent = `${last.turbid_pct}%`;

  const cutoff = new Date(last.date + "T00:00:00").getTime() - 30 * 864e5;
  const ref = [...s].reverse().find((p) => {
    const t = new Date(p.date + "T00:00:00").getTime();
    return t <= cutoff && t >= cutoff - 30 * 864e5;
  });
  const el = document.getElementById("t-change"), note = document.getElementById("t-change-note");
  if (!ref) { el.textContent = "–"; note.textContent = "needs 30 days of history"; return; }
  const d = +(last.hyacinth_pct - ref.hyacinth_pct).toFixed(1);
  el.textContent = `${d > 0 ? "▲ +" : d < 0 ? "▼ " : ""}${d}`;
  el.className = "tile-value " + (d > 0 ? "delta-up" : d < 0 ? "delta-down" : "");
  note.textContent = `points since ${fmtDate(ref.date)}`;
}

function drawTable(data) {
  const rows = {};
  data.series.forEach((s) => (rows[s.date] = { ...s }));
  (data.radar?.series || []).forEach((r) => (rows[r.date] = { ...rows[r.date], date: r.date, radar_pct: r.hyacinth_pct }));
  const cell = (v) => `<td>${v ?? ""}</td>`;
  document.querySelector("#table tbody").innerHTML = Object.values(rows)
    .sort((a, b) => (a.date < b.date ? 1 : -1))
    .map((s) => `<tr><td>${fmtDate(s.date)}</td>${cell(s.hyacinth_pct)}${cell(s.radar_pct)}${cell(s.hyacinth_km2)}${cell(s.turbid_pct)}${cell(s.valid_fraction == null ? null : Math.round(s.valid_fraction * 100))}</tr>`)
    .join("");
}

function drawCharts() {
  if (!current) return;
  const optical = (key) => ({ name: "Optical", cls: "opt", rows: current.series, key,
    tip: (s) => `<div class="tt-sub">Cloud-free: ${Math.round(s.valid_fraction * 100)}% of river</div>` });
  const hy = [optical("hyacinth_pct")];
  if (current.radar) {
    hy.push({ name: `Radar estimate (±${current.radar.mae_pct} pts)`, cls: "rad", rows: current.radar.series, key: "hyacinth_pct" });
  }
  lineChart(document.getElementById("c-hy"), hy, "Hyacinth cover");
  lineChart(document.getElementById("c-tu"), [optical("turbid_pct")], "Muddy water");
}

function drawAbout(data) {
  const el = document.getElementById("radar-about");
  if (!data.radar) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `<strong>Through clouds</strong>: the Sentinel-1 radar satellite passes about every 12 days and sees
    through monsoon clouds. Open water looks dark to radar; floating plants look brighter. The radar line is
    calibrated automatically against ${data.radar.pairs} days when both satellites passed, and on average it lands within
    <strong>±${data.radar.mae_pct} percentage points</strong> of the optical figure. It is too coarse to rank individual
    stretches, so the map and hotspot list use optical images only.`;
}

// ---------- load ----------
async function loadRiver(id) {
  const data = await fetch(`data/${id}.json`, { cache: "no-cache" }).then((r) => r.json());
  current = data;
  document.getElementById("meta").textContent =
    `Latest satellite image: ${fmtDate(data.latest_date)} · ${data.series.length} clear dates on record · updated automatically`;
  document.getElementById("th-ndvi").textContent = data.thresholds.hyacinth_ndvi;
  document.getElementById("th-ndti").textContent = data.thresholds.turbid_ndti;
  drawTiles(data);
  drawSegments(data);
  drawHotspots(data);
  drawCharts();
  drawTable(data);
  drawAbout(data);
}

async function main() {
  initMap();
  const index = await fetch("data/index.json", { cache: "no-cache" }).then((r) => r.json());
  const sel = document.getElementById("river");
  sel.innerHTML = index.rivers.map((r) => `<option value="${r.id}">${r.name}</option>`).join("");
  const wanted = new URLSearchParams(location.search).get("river");
  if (wanted && index.rivers.some((r) => r.id === wanted)) sel.value = wanted;
  sel.addEventListener("change", () => {
    history.replaceState(null, "", `?river=${sel.value}`);
    loadRiver(sel.value);
  });
  await loadRiver(sel.value);
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawCharts, 150); });
}

main().catch((e) => {
  document.getElementById("meta").textContent = "Could not load data: " + e.message;
});
