/* app.js — co-citation network dashboard frontend
 *
 * Talks to the local Flask backend:
 *   GET /api/meta            year range + journal list
 *   GET /api/graph?...       filtered co-citation graph
 *
 * Two filters, two objects:
 *   min_strength  — edge filter: how many articles co-cite a pair
 *   min_citations — node filter: how many times a reference is cited
 *
 * Bridging mode fades each co-citation community and highlights the
 * references whose links cross between communities.
 */
"use strict";

// ---- decade / era palette ----------------------------------------------
const ERA = [
  { max: 1969, c: "#7c6a55", label: "\u22641969" },
  { max: 1989, c: "#1e3a5f", label: "1970s\u201380s" },
  { max: 1999, c: "#2f7d6b", label: "1990s" },
  { max: 2009, c: "#b8860b", label: "2000s" },
  { max: 9999, c: "#9a3412", label: "2010s+" },
];
function eraColor(y) {
  if (y == null) return "#b9b3a4";
  for (const e of ERA) if (y <= e.max) return e.c;
  return "#9a3412";
}

// ---- cluster palette (used by "color by cluster" + bridging mode) ------
const CLUSTER_COLORS = [
  "#9a3412", "#1e3a5f", "#2f7d6b", "#b8860b",
  "#6d28d9", "#be123c", "#0f766e", "#a16207",
];
function clusterColor(c) {
  return CLUSTER_COLORS[c % CLUSTER_COLORS.length];
}

const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
const tip = document.getElementById("tip");
const overlay = document.getElementById("overlay");

// ---- view + layout state -----------------------------------------------
let view = { x: 0, y: 0, k: 1 };
let layout = [];
let links = [];
let nodeById = {};
let hoverNode = null, dragNode = null, panning = false;
let lastM = { x: 0, y: 0 };
let META = null;
let lastGraph = null;          // most recent /api/graph response
let bridgeMode = false;        // whether bridging mode is active

// ---- canvas sizing ------------------------------------------------------
function fit() {
  cv.width = cv.clientWidth * devicePixelRatio;
  cv.height = cv.clientHeight * devicePixelRatio;
}
window.addEventListener("resize", () => { fit(); draw(); });

// ---- legend (rebuilt depending on color mode) --------------------------
function updateLegend() {
  const el = document.getElementById("legend");
  const mode = document.getElementById("colorMode").value;
  if (bridgeMode) {
    el.innerHTML =
      `<div class="row"><span class="dot" style="background:#9a3412"></span>bridging work</div>` +
      `<div class="row"><span class="dot" style="background:#c9c3b4"></span>within a cluster</div>` +
      `<div class="row" style="margin-top:4px;color:#9a3412;">red links cross clusters</div>`;
    return;
  }
  if (mode === "cluster" && lastGraph && lastGraph.clusters) {
    const n = Math.min(lastGraph.clusters.length, CLUSTER_COLORS.length);
    let rows = "";
    for (let i = 0; i < n; i++) {
      rows += `<div class="row"><span class="dot" style="background:${clusterColor(i)}"></span>` +
              `cluster ${i + 1} (${lastGraph.clusters[i]})</div>`;
    }
    el.innerHTML = rows;
    return;
  }
  el.innerHTML =
    ERA.map(e => `<div class="row"><span class="dot" style="background:${e.c}"></span>${e.label}</div>`).join("") +
    `<div class="row"><span class="dot" style="background:#b9b3a4"></span>no year</div>`;
}

// ---- meta application (used by init, upload, and revert) ---------------
// Applies a /api/meta response to the UI. Called any time the active
// dataset changes, so it must be safe to re-run -- which means clearing
// the journals dropdown each time before repopulating.
function applyMeta(m) {
  META = m;
  document.getElementById("yFrom").value = m.year_min;
  document.getElementById("yTo").value = m.year_max;
  document.getElementById("corpusline").textContent =
    `${m.n_articles} citing articles \u00b7 ${m.n_refs.toLocaleString()} references \u00b7 ` +
    `${m.n_pairs.toLocaleString()} co-citation pairs indexed`;
  const sel = document.getElementById("jrnl");
  // first option is the static "All journals" placeholder; drop the rest
  while (sel.options.length > 1) sel.remove(1);
  m.journals.forEach(j => {
    const o = document.createElement("option");
    o.value = j;
    o.textContent = j.length > 38 ? j.slice(0, 36) + "\u2026" : j;
    sel.appendChild(o);
  });
  setCorpusTag(m.source || "bundled");
  updateLegend();
}

function setCorpusTag(source) {
  const tag = document.getElementById("corpusTag");
  const revertBtn = document.getElementById("resetCorpusBtn");
  if (source === "uploaded") {
    tag.textContent = "uploaded \u2014 in-memory only";
    tag.classList.add("uploaded");
    revertBtn.style.display = "";
  } else {
    tag.textContent = "bundled";
    tag.classList.remove("uploaded");
    revertBtn.style.display = "none";
  }
}

// ---- init: fetch metadata, populate controls ---------------------------
async function init() {
  fit();
  try {
    const m = await fetch("/api/meta").then(r => r.json());
    applyMeta(m);
  } catch (e) {
    document.getElementById("corpusline").textContent =
      "backend not reachable \u2014 is app.py running?";
    overlay.textContent = "Cannot reach the backend.\nStart it with:  python app.py";
    return;
  }
  rebuild();
}

// ---- fetch + rebuild ----------------------------------------------------
async function rebuild() {
  const btn = document.getElementById("apply");
  btn.disabled = true;
  overlay.classList.remove("hidden");
  overlay.textContent = "computing co-citation network\u2026";

  bridgeMode = document.getElementById("bridgeMode").checked;

  const p = new URLSearchParams({
    year_from: document.getElementById("yFrom").value || META.year_min,
    year_to: document.getElementById("yTo").value || META.year_max,
    min_strength: document.getElementById("minStr").value,
    min_citations: document.getElementById("minCit").value,
    max_nodes: document.getElementById("maxN").value,
    journal: document.getElementById("jrnl").value,
    bridging: bridgeMode ? "1" : "0",
  });

  let g;
  try {
    g = await fetch("/api/graph?" + p).then(r => r.json());
  } catch (e) {
    overlay.textContent = "Request failed.";
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  lastGraph = g;

  if (!g.nodes.length) {
    layout = []; links = [];
    overlay.classList.remove("hidden");
    overlay.textContent =
      `No co-citation links match these filters.\n` +
      `${g.selected_articles} articles selected \u2014 lower the minimum ` +
      `co-citation strength, lower the citation strength, or widen the years.`;
    draw();
    updateStats(g);
    document.getElementById("pairsList").innerHTML = "";
    return;
  }
  overlay.classList.add("hidden");

  nodeById = {};
  layout = g.nodes.map(n => {
    const o = {
      id: n.id, label: n.label, year: n.year, total: n.total,
      local: n.local, degree: n.degree,
      cluster: n.cluster, cross: n.cross, intra: n.intra,
      reach: n.reach, bridge_ratio: n.bridge_ratio, bridge: n.bridge,
      x: (Math.random() - 0.5) * 700, vx: 0,
      yy: (Math.random() - 0.5) * 700, vy: 0,
    };
    nodeById[n.id] = o;
    return o;
  });
  links = g.edges.map(e => ({
    s: e.s, t: e.t, w: e.w, cross: e.cross,
    A: nodeById[e.s], B: nodeById[e.t],
  }));

  fit();
  runSim();
  centerView();
  updateStats(g);
  updateLegend();
  if (bridgeMode) buildBridges(g);
  else buildPairs(g);
}

// ---- force simulation (fixed iterations, synchronous) ------------------
function runSim() {
  const N = layout.length;
  if (!N) { draw(); return; }
  const maxW = Math.max(...links.map(e => e.w), 1);

  // Group nodes by their backend-assigned cluster. The membership list is
  // fixed for this run; only positions change, so we can re-mean each step
  // without re-bucketing. Nodes without a cluster (shouldn't happen, but
  // guarded) are excluded from cluster-level forces only.
  const clusterMembers = {};
  const clusterIds = [];
  for (const n of layout) {
    if (n.cluster == null) continue;
    if (!clusterMembers[n.cluster]) {
      clusterMembers[n.cluster] = [];
      clusterIds.push(n.cluster);
    }
    clusterMembers[n.cluster].push(n);
  }

  // Cluster-separation tuning. The default view (CPA + PCSR) has ~2-6
  // clusters of ~10-60 nodes each; these constants are picked so centroids
  // end up visibly separated without overpowering the edge springs that
  // give each cluster its shape.
  //   CLUSTER_REPEL    — strength of centroid<->centroid repulsion. Uses
  //   a linear (1/d) falloff with a softened minimum distance so it stays
  //   useful at long range (otherwise 1/d^2 fades before clusters separate)
  //   without spiking when two centroids briefly coincide early on.
  //   CLUSTER_COHESION — weak per-node pull toward own centroid. Edge
  //   springs already do most of the work holding a cluster together; this
  //   just prevents loosely-tied members from being stripped off as the
  //   whole cluster is shoved by the centroid repulsion.
  const CLUSTER_REPEL = 1100;
  const CLUSTER_COHESION = 0.004;

  for (let it = 0; it < 280; it++) {
    // ---- centroids (mean x, mean yy) of each cluster, recomputed each
    // step so the forces follow nodes as they move ---------------------
    const cx = {}, cy = {};
    for (const c of clusterIds) {
      let sx = 0, sy = 0;
      const mem = clusterMembers[c];
      for (const n of mem) { sx += n.x; sy += n.yy; }
      cx[c] = sx / mem.length;
      cy[c] = sy / mem.length;
    }

    // pairwise node repulsion (unchanged)
    for (let i = 0; i < N; i++) {
      const a = layout[i];
      for (let j = i + 1; j < N; j++) {
        const b = layout[j];
        let dx = a.x - b.x, dy = a.yy - b.yy;
        let d2 = dx * dx + dy * dy + 0.01;
        const f = 2600 / d2;
        const d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.vx += dx * f; a.vy += dy * f;
        b.vx -= dx * f; b.vy -= dy * f;
      }
    }
    // edge attraction (unchanged) — includes cross-cluster edges, which
    // still pull clusters toward each other; the new cluster repulsion
    // below has to be strong enough to win against that without erasing it
    for (const e of links) {
      const a = e.A, b = e.B;
      let dx = b.x - a.x, dy = b.yy - a.yy;
      const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const w = e.w / maxW;
      const f = (d - 72) * 0.012 * (0.4 + w);
      dx /= d; dy /= d;
      a.vx += dx * f; a.vy += dy * f;
      b.vx -= dx * f; b.vy -= dy * f;
    }

    // ---- NEW: cluster centroid mutual repulsion ----------------------
    // For each pair of clusters, push every member of one away from the
    // other along the centroid-to-centroid line. Force is applied per
    // node (not divided by cluster size), so two clusters of any size
    // get the same centroid acceleration — small clusters don't get
    // flung while large ones sit still.
    for (let i = 0; i < clusterIds.length; i++) {
      for (let j = i + 1; j < clusterIds.length; j++) {
        const ca = clusterIds[i], cb = clusterIds[j];
        let dx = cx[ca] - cx[cb], dy = cy[ca] - cy[cb];
        const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
        // softened denominator: linear falloff at range, but capped near
        // zero so early iterations with overlapping centroids don't spike
        const f = CLUSTER_REPEL / Math.max(d, 80);
        dx /= d; dy /= d;
        const ma = clusterMembers[ca], mb = clusterMembers[cb];
        for (const n of ma) { n.vx += dx * f; n.vy += dy * f; }
        for (const n of mb) { n.vx -= dx * f; n.vy -= dy * f; }
      }
    }

    // ---- NEW: weak per-node attraction to own cluster centroid -------
    // Keeps each cluster internally cohesive as it's being shoved around
    // by the centroid repulsion above. Deliberately weak — edge springs
    // are still the primary organizing force inside a cluster.
    for (const n of layout) {
      if (n.cluster == null) continue;
      n.vx += (cx[n.cluster] - n.x) * CLUSTER_COHESION;
      n.vy += (cy[n.cluster] - n.yy) * CLUSTER_COHESION;
    }

    const damp = 0.86;
    for (let i = 0; i < N; i++) {
      const n = layout[i];
      n.vx += -n.x * 0.0016;
      n.vy += -n.yy * 0.0016;
      n.vx *= damp; n.vy *= damp;
      n.x += n.vx; n.yy += n.vy;
    }
  }
  draw();
}

function centerView() {
  if (!layout.length) {
    view = { x: cv.clientWidth / 2, y: cv.clientHeight / 2, k: 1 };
    return;
  }
  let minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9;
  for (const n of layout) {
    minx = Math.min(minx, n.x); maxx = Math.max(maxx, n.x);
    miny = Math.min(miny, n.yy); maxy = Math.max(maxy, n.yy);
  }
  const w = cv.clientWidth, h = cv.clientHeight;
  const k = Math.min(w / (maxx - minx + 140), h / (maxy - miny + 140), 2.2);
  view.k = k;
  view.x = w / 2 - ((minx + maxx) / 2) * k;
  view.y = h / 2 - ((miny + maxy) / 2) * k;
}

// ---- node color resolution ---------------------------------------------
function nodeColor(n) {
  if (bridgeMode) {
    // bridges pop in their cluster color; non-bridges fade to grey
    return n.bridge ? clusterColor(n.cluster) : "#c9c3b4";
  }
  const mode = document.getElementById("colorMode").value;
  if (mode === "cluster") return clusterColor(n.cluster);
  return eraColor(n.year);
}

// ---- render -------------------------------------------------------------
function draw() {
  const dpr = devicePixelRatio;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cv.clientWidth, cv.clientHeight);
  ctx.save();
  ctx.translate(view.x, view.y);
  ctx.scale(view.k, view.k);

  if (!layout.length) { ctx.restore(); return; }

  const term = document.getElementById("search").value.trim().toLowerCase();
  const maxW = Math.max(...links.map(e => e.w), 1);
  const maxT = Math.max(...layout.map(n => n.local), 1);

  // edges
  for (const e of links) {
    const w = e.w / maxW;
    if (bridgeMode) {
      if (e.cross) {
        // cross-cluster links: the bridging signal — draw them red & bold
        ctx.strokeStyle = "rgba(154,52,18," + (0.35 + w * 0.5) + ")";
        ctx.lineWidth = (0.8 + w * 3.0) / view.k;
      } else {
        // intra-cluster links: faded to background
        ctx.strokeStyle = "rgba(60,54,42,0.06)";
        ctx.lineWidth = (0.3 + w * 1.4) / view.k;
      }
    } else {
      ctx.strokeStyle = "rgba(60,54,42," + (0.07 + w * 0.34) + ")";
      ctx.lineWidth = (0.3 + w * 2.6) / view.k;
    }
    ctx.beginPath();
    ctx.moveTo(e.A.x, e.A.yy);
    ctx.lineTo(e.B.x, e.B.yy);
    ctx.stroke();
  }
  // nodes
  for (const n of layout) {
    const r = 4 + Math.sqrt(n.local / maxT) * 17;
    const hit = term && n.label.toLowerCase().includes(term);
    ctx.beginPath();
    ctx.arc(n.x, n.yy, r, 0, 7);
    ctx.fillStyle = nodeColor(n);
    let alpha = 1;
    if (term && !hit) alpha = 0.16;
    else if (bridgeMode && !n.bridge) alpha = 0.4;
    ctx.globalAlpha = alpha;
    ctx.fill();
    if (hit || n === hoverNode || (bridgeMode && n.bridge)) {
      ctx.lineWidth = (bridgeMode && n.bridge ? 1.6 : 2.4) / view.k;
      ctx.strokeStyle = "#1a1916";
      ctx.globalAlpha = (term && !hit) ? 0.16 : 1;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  // labels
  for (const n of layout) {
    const r = 4 + Math.sqrt(n.local / maxT) * 17;
    // in bridging mode, only label the bridges (keeps the view legible)
    if (bridgeMode && !n.bridge) continue;
    if (view.k * r > 9 || (bridgeMode && n.bridge && view.k * r > 5)) {
      const fs = Math.min(13, 9 + r * 0.2) / view.k;
      ctx.font = fs + "px ui-monospace,Menlo,monospace";
      ctx.fillStyle = "rgba(26,25,22,.82)";
      ctx.fillText(n.label.split(",")[0], n.x + r + 2 / view.k,
                   n.yy + 3 / view.k);
    }
  }
  ctx.restore();
}

// ---- stats + side panels -----------------------------------------------
function esc(s) {
  return s.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function shortLabel(s) {
  return esc(s.split(",").slice(0, 2).join(", "));
}
function updateStats(g) {
  const nClusters = g.clusters ? g.clusters.length : 0;
  let html =
    `<b>${g.selected_articles}</b> articles selected<br>` +
    `<b>${layout.length}</b> nodes \u00b7 <b>${links.length}</b> links shown<br>` +
    `<b>${g.strong_pairs}</b> pairs meet the strength threshold<br>` +
    `<b>${nClusters}</b> co-citation clusters detected<br>`;
  if (bridgeMode) {
    const bridges = layout.filter(n => n.bridge);
    const crossEdges = links.filter(e => e.cross).length;
    html += `<b>${bridges.length}</b> bridging works \u00b7 ` +
            `<b>${crossEdges}</b> cross-cluster links`;
  } else {
    const top = layout.slice().sort((a, b) => b.degree - a.degree)[0];
    if (top) html += `most-connected:<br><b>${shortLabel(top.label)}</b> ` +
                     `(${top.degree} links)`;
  }
  document.getElementById("stats").innerHTML = html;
}
function buildPairs(g) {
  document.getElementById("panelHead").textContent = "Strongest co-cited pairs";
  document.getElementById("togPairs").dataset.kind = "pairs";
  const sorted = g.edges.slice().sort((a, b) => b.w - a.w).slice(0, 40);
  document.getElementById("pairsList").innerHTML = sorted.map(e => {
    const a = nodeById[e.s], b = nodeById[e.t];
    if (!a || !b) return "";
    return `<div class="pair"><span class="s">${e.w}\u00d7</span>` +
      (e.cross ? ` <span class="s" style="background:#1e3a5f">cross</span>` : "") +
      `<br><span class="a">${shortLabel(a.label)}</span><br>` +
      `<span class="b">\u2194 ${shortLabel(b.label)}</span></div>`;
  }).join("") || '<div class="pair">No pairs.</div>';
}
function buildBridges(g) {
  document.getElementById("panelHead").textContent = "Bridging works";
  document.getElementById("togPairs").dataset.kind = "bridges";
  // rank bridges: first by # of clusters reached, then by bridging ratio,
  // then by strength-weighted cross-cluster co-citation
  const bridges = g.nodes.filter(n => n.bridge).sort((a, b) =>
    (b.reach - a.reach) ||
    (b.bridge_ratio - a.bridge_ratio) ||
    (b.cross - a.cross));
  const list = document.getElementById("pairsList");
  if (!bridges.length) {
    list.innerHTML = '<div class="pair">No bridging works at these ' +
      'filters. Try a lower co-citation strength so weak cross-cluster ' +
      'links survive, and a higher citation strength to keep the view clean.</div>';
    return;
  }
  list.innerHTML = bridges.slice(0, 40).map(n => {
    const pct = Math.round(n.bridge_ratio * 100);
    return `<div class="pair">` +
      `<span class="s">${pct}% cross</span> ` +
      `<span class="s" style="background:#1e3a5f">${n.reach} clusters</span><br>` +
      `<span class="a">${shortLabel(n.label)}</span><br>` +
      `<span class="b">${n.cross} cross-cluster \u00b7 ${n.intra} within ` +
      `\u00b7 cited ${n.local}\u00d7</span></div>`;
  }).join("");
}

// ---- interaction --------------------------------------------------------
function toWorld(mx, my) {
  return { x: (mx - view.x) / view.k, y: (my - view.y) / view.k };
}
function pick(mx, my) {
  const w = toWorld(mx, my);
  const maxT = Math.max(...layout.map(n => n.local), 1);
  for (let i = layout.length - 1; i >= 0; i--) {
    const n = layout[i];
    const r = 4 + Math.sqrt(n.local / maxT) * 17;
    if ((n.x - w.x) ** 2 + (n.yy - w.y) ** 2 < (r + 3) ** 2) return n;
  }
  return null;
}
cv.addEventListener("mousedown", e => {
  const r = cv.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  dragNode = pick(mx, my);
  if (!dragNode) panning = true;
  lastM = { x: mx, y: my };
});
cv.addEventListener("mousemove", e => {
  const r = cv.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  if (dragNode) {
    const w = toWorld(mx, my);
    dragNode.x = w.x; dragNode.yy = w.y;
    draw();
  } else if (panning) {
    view.x += mx - lastM.x;
    view.y += my - lastM.y;
    draw();
  } else {
    const n = pick(mx, my);
    if (n !== hoverNode) { hoverNode = n; draw(); }
    if (n) {
      tip.style.display = "block";
      tip.style.left = Math.min(mx + 14, cv.clientWidth - 300) + "px";
      tip.style.top = (my + 14) + "px";
      let body = `year ${n.year ?? "\u2014"} \u00b7 ${n.local} citations in ` +
        `selection (${n.total} corpus-wide) \u00b7 ${n.degree} co-cite links`;
      body += `<br>cluster ${n.cluster + 1}`;
      if (n.cross > 0) {
        body += ` \u00b7 ${n.cross} cross-cluster link${n.cross === 1 ? "" : "s"} ` +
          `into ${n.reach} other cluster${n.reach === 1 ? "" : "s"}` +
          (n.bridge ? " \u2014 bridging work" : "");
      }
      tip.innerHTML = `<div class="ttl">${esc(n.label)}</div>${body}`;
    } else {
      tip.style.display = "none";
    }
  }
  lastM = { x: mx, y: my };
});
window.addEventListener("mouseup", () => { dragNode = null; panning = false; });
cv.addEventListener("wheel", e => {
  e.preventDefault();
  const r = cv.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const f = e.deltaY < 0 ? 1.12 : 0.89;
  const nk = Math.max(0.15, Math.min(6, view.k * f));
  view.x = mx - (mx - view.x) * (nk / view.k);
  view.y = my - (my - view.y) * (nk / view.k);
  view.k = nk;
  draw();
}, { passive: false });

// ---- controls -----------------------------------------------------------
function bindRange(id, vid) {
  const el = document.getElementById(id), v = document.getElementById(vid);
  el.addEventListener("input", () => { v.textContent = el.value; });
}
bindRange("minStr", "minStrV");
bindRange("minCit", "minCitV");
bindRange("maxN", "maxNV");
document.getElementById("apply").addEventListener("click", rebuild);
document.getElementById("search").addEventListener("input", draw);
document.getElementById("colorMode").addEventListener("change", () => {
  updateLegend();
  draw();
});
document.getElementById("togPairs").addEventListener("click", () => {
  const p = document.getElementById("pairsPanel");
  p.classList.toggle("open");
  const open = p.classList.contains("open");
  const kind = document.getElementById("togPairs").dataset.kind === "bridges"
    ? "Bridges" : "Top pairs";
  document.getElementById("togPairs").textContent =
    open ? kind + " \u25c2" : kind + " \u25b8";
});
document.getElementById("reset").addEventListener("click", () => {
  document.getElementById("yFrom").value = META.year_min;
  document.getElementById("yTo").value = META.year_max;
  document.getElementById("minStr").value = 5;
  document.getElementById("minStrV").textContent = 5;
  document.getElementById("minCit").value = 0;
  document.getElementById("minCitV").textContent = 0;
  document.getElementById("maxN").value = 150;
  document.getElementById("maxNV").textContent = 150;
  document.getElementById("jrnl").value = "";
  document.getElementById("search").value = "";
  document.getElementById("colorMode").value = "era";
  document.getElementById("bridgeMode").checked = false;
  rebuild();
});

// ---- corpus upload + revert --------------------------------------------
async function uploadCSV(file) {
  if (!file) return;
  overlay.classList.remove("hidden");
  overlay.textContent = "uploading and parsing CSV\u2026";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/load", { method: "POST", body: fd });
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).error || msg; } catch (_) {}
      overlay.textContent = "upload failed: " + msg;
      return;
    }
    applyMeta(await r.json());
  } catch (e) {
    overlay.textContent = "upload failed: " + e.message;
    return;
  }
  rebuild();
}

async function revertCorpus() {
  overlay.classList.remove("hidden");
  overlay.textContent = "reverting to bundled corpus\u2026";
  try {
    const r = await fetch("/api/reset", { method: "POST" });
    if (!r.ok) { overlay.textContent = "revert failed: " + r.statusText; return; }
    applyMeta(await r.json());
  } catch (e) {
    overlay.textContent = "revert failed: " + e.message;
    return;
  }
  rebuild();
}

// ---- zoom controls -----------------------------------------------------
// Scale `view.k` while holding a chosen screen point fixed in world space.
// For + / - we hold the canvas centre; for fit we just call the existing
// centerView() which recomputes both k and translation from the layout.
function zoomAround(sx, sy, factor) {
  const nk = Math.max(0.15, Math.min(6, view.k * factor));
  view.x = sx - (sx - view.x) * (nk / view.k);
  view.y = sy - (sy - view.y) * (nk / view.k);
  view.k = nk;
  draw();
}
function zoomIn()  { zoomAround(cv.clientWidth / 2, cv.clientHeight / 2, 1.25); }
function zoomOut() { zoomAround(cv.clientWidth / 2, cv.clientHeight / 2, 0.80); }
function zoomFit() { centerView(); draw(); }

// ---- CSV export of the current view ------------------------------------
// One row per visible reference. Columns include local + corpus-wide
// citations, cluster id (1-indexed for readability), and the sum of the
// node's incident edge weights, i.e. the total co-citation strength
// running through it in the current filtered view.
function csvCell(v) {
  if (v == null) return "";
  const s = String(v);
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function exportCSV() {
  if (!layout.length) {
    overlay.classList.remove("hidden");
    overlay.textContent = "Nothing to export \u2014 rebuild the network first.";
    setTimeout(() => overlay.classList.add("hidden"), 1800);
    return;
  }
  const totalStrength = {};
  for (const n of layout) totalStrength[n.id] = 0;
  for (const e of links) {
    totalStrength[e.s] = (totalStrength[e.s] || 0) + e.w;
    totalStrength[e.t] = (totalStrength[e.t] || 0) + e.w;
  }
  const cols = ["ref_id", "label", "year", "local_citations",
                "total_citations", "cluster", "degree",
                "co_citation_strength", "cross_cluster_links",
                "bridge_ratio", "is_bridge"];
  const rows = [cols.join(",")];
  for (const n of layout) {
    rows.push([
      n.id, n.label, n.year == null ? "" : n.year,
      n.local, n.total,
      n.cluster + 1,                            // 1-indexed for spreadsheets
      n.degree, totalStrength[n.id] || 0,
      n.cross, n.bridge_ratio, n.bridge ? "true" : "false",
    ].map(csvCell).join(","));
  }
  // BOM + CRLF so Excel opens it cleanly without import dialogs
  const body = "\ufeff" + rows.join("\r\n") + "\r\n";
  const blob = new Blob([body], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const yf = document.getElementById("yFrom").value;
  const yt = document.getElementById("yTo").value;
  const ms = document.getElementById("minStr").value;
  const mc = document.getElementById("minCit").value;
  const mn = document.getElementById("maxN").value;
  const fname = `cocitation_${yf}-${yt}_str${ms}_cit${mc}_n${mn}.csv`;
  const a = document.createElement("a");
  a.href = url; a.download = fname;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

// ---- wire up the new controls ------------------------------------------
document.getElementById("uploadBtn").addEventListener("click", () => {
  document.getElementById("csvFile").click();
});
document.getElementById("csvFile").addEventListener("change", e => {
  const f = e.target.files && e.target.files[0];
  uploadCSV(f);
  e.target.value = "";   // allow re-uploading the same filename
});
document.getElementById("resetCorpusBtn").addEventListener("click", revertCorpus);
document.getElementById("zoomIn").addEventListener("click", zoomIn);
document.getElementById("zoomOut").addEventListener("click", zoomOut);
document.getElementById("zoomFit").addEventListener("click", zoomFit);
document.getElementById("exportBtn").addEventListener("click", exportCSV);


init();
