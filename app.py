#!/usr/bin/env python3
"""
app.py — Local dashboard backend for the co-citation network.

Loads a precomputed co-citation pair index and recomputes edge
strengths on demand for any subset of citing articles, filtered by
citing-article year range and/or journal.

Each pair in the index stores the list of citing articles that produced
it. Filtering therefore reduces to: for each pair, count how many of its
contributing articles pass the current filter. This keeps every request
in the millisecond range even though the underlying corpus has ~2.4M
raw reference-pair combinations.

Run:  python app.py
Then open  http://127.0.0.1:5000
"""
import csv
import io
import json
import os
from collections import defaultdict

from flask import Flask, jsonify, request, send_from_directory

from preprocess import build_from_csv

BASE = os.path.dirname(os.path.abspath(__file__))
CORPUS_PATH = os.path.join(BASE, "data", "corpus.json")
PAIRS_PATH = os.path.join(BASE, "data", "pairs.json")

# Hard ceiling on uploaded CSV size. Vercel Hobby caps request bodies at
# ~4.5 MB; we reject larger payloads explicitly so the user gets a clear
# message instead of a generic 413 from the platform.
MAX_UPLOAD_BYTES = 4_500_000

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 100_000  # small headroom

# ---------------------------------------------------------------------------
# Active dataset (module-level globals). Loaded once from the bundled JSON
# at import time and then mutated in-place when the user uploads a custom
# CSV via /api/load. Subsequent /api/meta and /api/graph calls read from
# these globals, so the swap is transparent to the rest of the app.
#
# Because Vercel functions are stateless, an uploaded dataset only lives
# as long as the warm container does -- once it goes cold and a fresh
# instance starts, the bundled corpus is reloaded and the user has to
# re-upload. For the single-user research workflow this dashboard is
# built for, that's an acceptable tradeoff vs. introducing a blob store.
# ---------------------------------------------------------------------------
CORPUS = None
PAIRS = None
REFS = None
ARTICLES = None
JOURNALS = None
REF_BY_I = None
PX = None
PY = None
PARTS = None
N_PAIRS = 0
ACTIVE_SOURCE = "bundled"   # "bundled" or "uploaded" -- exposed via /api/meta


def _set_active(corpus, pairs, source):
    """Install corpus + pairs as the active dataset for graph queries.

    Updates every module-level handle in one shot so the in-flight
    request finishes against a consistent dataset. Derived structures
    (REF_BY_I lookup, the unzipped pair arrays) are rebuilt here too.
    """
    global CORPUS, PAIRS, REFS, ARTICLES, JOURNALS, REF_BY_I
    global PX, PY, PARTS, N_PAIRS, ACTIVE_SOURCE
    CORPUS = corpus
    PAIRS = pairs
    REFS = corpus["refs"]
    ARTICLES = corpus["articles"]
    JOURNALS = corpus["journals"]
    REF_BY_I = {r["i"]: r for r in REFS}
    PX = pairs["x"]
    PY = pairs["y"]
    PARTS = pairs["arts"]
    N_PAIRS = len(PX)
    ACTIVE_SOURCE = source


# Load the bundled default at import time.
for p in (CORPUS_PATH, PAIRS_PATH):
    if not os.path.exists(p):
        raise SystemExit(
            f"Missing {p}\n"
            f"Run:  python preprocess.py /path/to/master_citation_dataset.csv"
        )
with open(CORPUS_PATH) as _f:
    _bundled_corpus = json.load(_f)
with open(PAIRS_PATH) as _f:
    _bundled_pairs = json.load(_f)
_set_active(_bundled_corpus, _bundled_pairs, "bundled")

print(f"Loaded {len(REFS):,} refs, {len(ARTICLES):,} articles, "
      f"{len(JOURNALS)} journals, {N_PAIRS:,} co-citation pairs")


# ---------------------------------------------------------------------------
# Community detection (used by clustering + bridging analysis)
# ---------------------------------------------------------------------------
try:
    import networkx as nx
    from networkx.algorithms.community import greedy_modularity_communities
    _HAVE_NX = True
except ImportError:           # graceful fallback if networkx is absent
    _HAVE_NX = False


def _detect_communities(node_ids, edges):
    """Assign each node to a community.

    Uses greedy modularity maximisation (networkx) when available: this
    finds densely-connected groups even inside one connected graph, so
    the CPA and PCSR clusters are detected at ANY edge threshold -- not
    only once the weak links between them are filtered away.

    Falls back to connected components if networkx is not installed.
    Returns {node_id: community_index}, communities numbered largest first,
    plus the list of community sizes.
    """
    if _HAVE_NX and edges:
        G = nx.Graph()
        G.add_nodes_from(node_ids)
        for x, y, w in edges:
            G.add_edge(x, y, weight=w)
        try:
            comms = list(greedy_modularity_communities(G, weight="weight"))
        except Exception:
            comms = None
        if comms:
            ordered = sorted(comms, key=len, reverse=True)
            comp_of = {}
            for idx, members in enumerate(ordered):
                for n in members:
                    comp_of[n] = idx
            # any nodes networkx left out (isolated) -> own communities
            nxt = len(ordered)
            sizes = [len(m) for m in ordered]
            for n in node_ids:
                if n not in comp_of:
                    comp_of[n] = nxt
                    sizes.append(1)
                    nxt += 1
            return comp_of, sizes

    # fallback: connected components via union-find
    parent = {n: n for n in node_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for x, y, w in edges:
        ra, rb = find(x), find(y)
        if ra != rb:
            parent[ra] = rb

    groups = defaultdict(list)
    for n in node_ids:
        groups[find(n)].append(n)
    ordered = sorted(groups.values(), key=len, reverse=True)
    comp_of = {}
    for idx, members in enumerate(ordered):
        for n in members:
            comp_of[n] = idx
    return comp_of, [len(m) for m in ordered]


# ---------------------------------------------------------------------------
# Co-citation graph for a filtered article set
# ---------------------------------------------------------------------------
def build_graph(year_from, year_to, min_strength, max_nodes, journal,
                min_citations=0, bridging=False):
    """Recompute the co-citation network for the filtered article subset.

    Filtering by citing-article year and/or journal changes WHICH
    articles contribute, so edge strengths are recomputed (subgraph
    semantics): an edge weight is the number of *passing* articles that
    co-cite both endpoints.

    Two distinct filters operate here, on two distinct objects:
      * min_strength  -- minimum EDGE weight: how many articles must
                         co-cite a pair for the link to be drawn.
      * min_citations -- minimum NODE weight: how many times a reference
                         must be cited within the selection to be
                         eligible to appear at all.
    A reference cited fewer than `min_citations` times is removed before
    any edge is considered, so its links never appear.

    bridging=True adds, per node, a flag for whether its edges cross
    between clusters (i.e. whether it bridges them).
    """
    # 1. boolean mask over articles: does each article pass the filter?
    passes = [False] * len(ARTICLES)
    selected = 0
    for ai, a in enumerate(ARTICLES):
        ay = a["year"]
        if ay is None or ay < year_from or ay > year_to:
            continue
        if journal and a["journal"] != journal:
            continue
        passes[ai] = True
        selected += 1

    # 2. local citation count for EVERY reference in the selection
    #    (independent of edges -- needed for the node-level filter)
    ref_local_total = defaultdict(int)
    for ai, a in enumerate(ARTICLES):
        if passes[ai]:
            for r in a["refs"]:
                ref_local_total[r] += 1

    # 3. node-level filter: references prominent enough to be eligible
    eligible = {r for r, c in ref_local_total.items()
                if c >= min_citations}

    # 4. walk the precomputed pairs; recompute strength against the mask.
    #    An edge survives only if BOTH endpoints are eligible nodes.
    edges = []                       # (x, y, strength)
    strong_pairs = 0
    for k in range(N_PAIRS):
        x, y = PX[k], PY[k]
        if x not in eligible or y not in eligible:
            continue
        w = 0
        for ai in PARTS[k]:
            if passes[ai]:
                w += 1
        if w >= min_strength:
            strong_pairs += 1
            edges.append((x, y, w))

    if not edges:
        return {"nodes": [], "edges": [], "selected_articles": selected,
                "strong_pairs": 0, "clusters": []}

    # 5. cap node count by local citation count (tie-break: degree)
    deg = defaultdict(int)
    for x, y, w in edges:
        deg[x] += 1
        deg[y] += 1
    candidates = sorted(deg.keys(),
                        key=lambda r: (ref_local_total[r], deg[r]),
                        reverse=True)
    keep = set(candidates[:max_nodes])
    edges = [(x, y, w) for x, y, w in edges if x in keep and y in keep]

    if not edges:
        return {"nodes": [], "edges": [], "selected_articles": selected,
                "strong_pairs": strong_pairs, "clusters": []}

    # 6. assemble nodes from surviving edges
    node_ids = set()
    edge_deg = defaultdict(int)
    for x, y, w in edges:
        node_ids.add(x)
        node_ids.add(y)
        edge_deg[x] += 1
        edge_deg[y] += 1

    # 7. detect communities; compute per-node bridging metrics
    comp_of, comp_sizes = _detect_communities(node_ids, edges)

    # per-node bridging metrics. For each node we track:
    #   intra  -- number of links staying inside its own community
    #   cross  -- number of links crossing to a different community
    #   reach  -- the SET of foreign communities it links into
    #   w_cross/w_intra -- the same, weighted by co-citation strength
    cross_links = defaultdict(int)
    intra_links = defaultdict(int)
    w_cross = defaultdict(int)
    w_intra = defaultdict(int)
    reach = defaultdict(set)         # foreign communities a node touches
    for x, y, w in edges:
        cx, cy = comp_of[x], comp_of[y]
        if cx == cy:
            intra_links[x] += 1
            intra_links[y] += 1
            w_intra[x] += w
            w_intra[y] += w
        else:
            cross_links[x] += 1
            cross_links[y] += 1
            w_cross[x] += w
            w_cross[y] += w
            reach[x].add(cy)
            reach[y].add(cx)

    nodes = []
    for nid in node_ids:
        r = REF_BY_I[nid]
        cr, it = cross_links[nid], intra_links[nid]
        total_links = cr + it
        # bridging ratio: share of a node's links that leave its community.
        # This separates genuine bridges (high ratio) from mega-cited hubs
        # that simply have many links, most of them intra-community.
        ratio = (cr / total_links) if total_links else 0.0
        nodes.append({
            "id": nid,
            "label": r["label"],
            "year": r["year"],
            "total": r["total"],            # corpus-wide citations
            "local": ref_local_total[nid],  # citations within selection
            "degree": edge_deg[nid],
            "cluster": comp_of[nid],
            "cross": cr,                     # links to other communities
            "intra": it,                     # links within own community
            "w_cross": w_cross[nid],         # cross links, strength-weighted
            "w_intra": w_intra[nid],         # intra links, strength-weighted
            "reach": len(reach[nid]),        # # of foreign communities touched
            "bridge_ratio": round(ratio, 3),
            # a genuine bridge: links into >=1 other community AND at
            # least a quarter of its links are cross-community
            "bridge": cr > 0 and ratio >= 0.25,
        })

    return {
        "nodes": nodes,
        "edges": [{"s": x, "t": y, "w": w,
                   "cross": comp_of[x] != comp_of[y]}
                  for x, y, w in edges],
        "clusters": comp_sizes,
        "selected_articles": selected,
        "strong_pairs": strong_pairs,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/meta")
def meta():
    return jsonify({
        "year_min": CORPUS["year_min"],
        "year_max": CORPUS["year_max"],
        "journals": JOURNALS,
        "n_articles": len(ARTICLES),
        "n_refs": len(REFS),
        "n_pairs": N_PAIRS,
        "source": ACTIVE_SOURCE,
    })


@app.route("/api/load", methods=["POST"])
def load():
    """Replace the active dataset with the user's uploaded CSV.

    Accepts the file either as a multipart upload (field name `file`)
    or as the raw request body. The CSV must use the same column names
    as master_citation_dataset.csv (citing_UT, cited_canonical_id,
    plus optional citing_year/journal/title and cited_label/year).

    Returns the new /api/meta payload on success so the frontend can
    refresh its controls without a second round-trip.
    """
    raw = b""
    upload = request.files.get("file")
    if upload is not None:
        raw = upload.read()
    if not raw:
        # Allow raw POST bodies too (curl convenience)
        raw = request.get_data(cache=False)
    if not raw:
        return jsonify({"error": "no CSV file provided"}), 400
    if len(raw) > MAX_UPLOAD_BYTES:
        return jsonify({
            "error": f"file is {len(raw)/1e6:.1f} MB; the upload limit "
                     f"is {MAX_UPLOAD_BYTES/1e6:.1f} MB"
        }), 413

    try:
        text = raw.decode("utf-8-sig")  # tolerate Excel-saved BOMs
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception as e:
            return jsonify({"error": f"could not decode file: {e}"}), 400

    try:
        corpus, pairs = build_from_csv(csv.DictReader(io.StringIO(text)))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"failed to parse CSV: {e}"}), 400

    _set_active(corpus, pairs, "uploaded")
    return jsonify({
        "year_min": CORPUS["year_min"],
        "year_max": CORPUS["year_max"],
        "journals": JOURNALS,
        "n_articles": len(ARTICLES),
        "n_refs": len(REFS),
        "n_pairs": N_PAIRS,
        "source": ACTIVE_SOURCE,
    })


@app.route("/api/reset", methods=["POST"])
def reset_corpus():
    """Revert the active dataset to the bundled corpus."""
    _set_active(_bundled_corpus, _bundled_pairs, "bundled")
    return jsonify({
        "year_min": CORPUS["year_min"],
        "year_max": CORPUS["year_max"],
        "journals": JOURNALS,
        "n_articles": len(ARTICLES),
        "n_refs": len(REFS),
        "n_pairs": N_PAIRS,
        "source": ACTIVE_SOURCE,
    })


@app.route("/api/graph")
def graph():
    try:
        year_from = int(request.args.get("year_from", CORPUS["year_min"]))
        year_to = int(request.args.get("year_to", CORPUS["year_max"]))
        min_strength = max(2, int(request.args.get("min_strength", 5)))
        max_nodes = max(10, min(600, int(request.args.get("max_nodes", 150))))
        journal = request.args.get("journal", "").strip()
        min_citations = max(0, int(request.args.get("min_citations", 0)))
        bridging = request.args.get("bridging", "0") in ("1", "true", "True")
    except ValueError:
        return jsonify({"error": "invalid parameters"}), 400

    return jsonify(build_graph(year_from, year_to, min_strength,
                               max_nodes, journal,
                               min_citations=min_citations,
                               bridging=bridging))


if __name__ == "__main__":
    print("Open  http://127.0.0.1:5000  in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False)
