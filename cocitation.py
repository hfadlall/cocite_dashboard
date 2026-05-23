#!/usr/bin/env python3
"""
cocitation.py -- standalone co-citation analysis module.

This is the analytical core of the dashboard, with no Flask dependency.
A collaborator can import it directly:

    from cocitation import load_corpus, build_cocitation_graph
    corpus, pairs = load_corpus("data/corpus.json", "data/pairs.json")
    result = build_cocitation_graph(
        corpus, pairs,
        year_from=1995, year_to=2024,
        min_strength=5, min_citations=0, max_nodes=150,
    )
    # -> {"nodes": [...], "edges": [...], "clusters": [...], ...}

The module also runs standalone (`python cocitation.py [--options]`) and
writes the resulting network as GraphML + node/edge CSV files for
inspection in Gephi, VOSviewer, or any spreadsheet tool.


Co-citation, in one paragraph
-----------------------------
Two references are co-cited when the same citing article lists both.
An edge's weight = the number of citing articles that co-cite that pair.
This module recomputes the network for any chosen subset of citing
articles (filtered by year and/or journal); edge weights and the
resulting community structure are properties of THAT subset, not of
some fixed corpus-wide graph. The whole point of computing things per
request is to honour this subgraph semantics.


Two filters, two distinct objects -- never conflate them
-------------------------------------------------------
* ``min_strength``  -- EDGE filter.  Minimum number of citing articles
  that must co-cite a pair for the link to be drawn.  A property of a
  pair.  Measures relatedness.
* ``min_citations`` -- NODE filter.  Minimum number of times a reference
  must be cited within the selection for it to appear at all.  A
  property of a single reference.  Measures influence.  Applied BEFORE
  any edge is considered, so a reference that fails it contributes none
  of its links.

The analytically informative regime for studying bridges between
clusters is HIGH min_citations + LOW min_strength: keep only prominent
references as nodes, but allow weak ties so cross-cluster links
survive.


Clustering choice
-----------------
Communities are detected with greedy modularity maximisation
(``networkx.algorithms.community.greedy_modularity_communities``).
Connected-components is provided as a fallback only if networkx is
missing.  The choice is deliberate: connected-components collapses the
whole graph into one component as soon as weak edges are admitted, so
it would only "find" the major split at a high edge threshold --
exactly where the bridges of interest have already been filtered away.
Modularity finds densely-connected subgroups inside one connected
graph, at any threshold.

The cluster IDs are arbitrary integers, sorted so that cluster 0 is
the largest.  They are NOT comparable across different filter
settings, and they are NOT comparable to VOSviewer's clustering (which
uses a different algorithm and a tuneable resolution parameter).


Bridging metrics
----------------
For each node we compute, against its assigned cluster:

* ``cross``        - number of edges going to a DIFFERENT cluster
* ``intra``        - number of edges staying inside its OWN cluster
* ``w_cross``      - sum of strengths of those cross-cluster edges
* ``w_intra``      - sum of strengths of intra-cluster edges
* ``reach``        - number of distinct other clusters it touches
* ``bridge_ratio`` - cross / (cross + intra)
* ``bridge``       - True iff cross >= 1 AND bridge_ratio >= 0.25

bridge_ratio is the meaningful metric, not raw cross count: a
mega-cited hub will have many cross links simply because it has many
links total.  The ratio test isolates references whose CITATION
POSITION genuinely spans clusters.

The aggregated ``w_cross`` / ``w_intra`` sums use the RAW co-citation
counts even when a normalised edge-weight mode is selected, so they
remain interpretable as citation tallies across modes.

Caveat: this surfaces references whose position bridges clusters,
which is not the same as identifying the lightly-cited paper that
actually created the bridge.  The bridging paper itself often falls
below the node filter; what survives is the cross-cluster link it
created between two prominent anchors.


Edge-weight modes (normalisation)
---------------------------------
``raw`` is the default and reproduces the original dashboard
behaviour: each edge's weight is c_ij, the number of selected citing
articles that co-cite both endpoints.

Three normalisation modes are available, each rescaling a co-citation
strength by the citation counts of its endpoints.  Let

    c_i  = local citation count of reference i (within the selection)
    c_j  = local citation count of reference j
    c_ij = number of selected articles co-citing i and j
    N    = number of selected citing articles

* ``cosine``                      = c_ij / sqrt(c_i * c_j)        (0..1)
* ``jaccard``                     = c_ij / (c_i + c_j - c_ij)     (0..1)
* ``association_strength_scaled`` = N * c_ij / (c_i * c_j)        (unbounded;
                                                                   centred near 1)

The four modes do NOT share a numeric scale.  Cosine and Jaccard are
bounded in [0, 1]; the scaled association-strength is an unbounded
ratio centred near 1 (values > 1 indicate co-occurrence above the
chance level under independence).  Any UI threshold on the active
mode's weight must adapt its range and default accordingly -- a
0.5 cutoff is restrictive on cosine, permissive on association
strength.

The selected mode's weight is used for:
  - community detection (modularity weighting),
  - the dashboard's top-pairs ranking and edge-width rendering,
  - the ``selected_weight`` field on every output edge,
  - the optional ``min_normalized_weight`` filter.

The raw count c_ij is always preserved on every edge as ``w``, so a
collaborator can recover the original network from any export
regardless of which mode was active.  ``min_strength`` is always
applied to c_ij (not to the selected weight); this is intentional
and important, because normalisation makes a 1-or-2 co-citation pair
look artificially strong when both endpoints are themselves rare.
The raw threshold guards against that; the normalised threshold then
acts as a SECOND filter on top.
"""
from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Supported edge-weight normalisation modes. See module docstring.
EDGE_WEIGHT_MODES = (
    "raw",
    "cosine",
    "jaccard",
    "association_strength_scaled",
)

# Networkx is the one external dependency. If it is missing we fall
# back to connected-components for clustering -- still produces a
# result, but a much less useful one (see module docstring).
try:
    import networkx as nx
    from networkx.algorithms.community import greedy_modularity_communities
    _HAVE_NX = True
except ImportError:
    nx = None
    greedy_modularity_communities = None
    _HAVE_NX = False


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def load_corpus(corpus_path: str, pairs_path: str) -> Tuple[dict, dict]:
    """Load the preprocessed corpus + co-citation pair index from disk.

    Both files are produced by ``preprocess.py`` from a long-format
    citation CSV.  This function does no validation beyond the JSON
    parse -- the assumption is that the files came out of preprocess.py
    or follow that schema:

    corpus = {
        "refs":     [{"i": int, "label": str, "year": int|None, "total": int}, ...],
        "articles": [{"ut": str, "year": int|None, "journal": str,
                      "title": str, "refs": [int, ...]}, ...],
        "journals": [str, ...],
        "year_min": int,
        "year_max": int,
    }
    pairs = {
        "x":    [int, ...],   # source ref id
        "y":    [int, ...],   # target ref id
        "arts": [[int, ...], ...],   # contributing article indices per pair
    }

    Returns the two dicts.
    """
    with open(corpus_path, "r", encoding="utf-8") as f:
        corpus = json.load(f)
    with open(pairs_path, "r", encoding="utf-8") as f:
        pairs = json.load(f)
    return corpus, pairs


# ---------------------------------------------------------------------------
# Community detection
# ---------------------------------------------------------------------------
def detect_communities(
    node_ids: Iterable[int],
    edges: Sequence[Tuple[int, int, int]],
) -> Tuple[Dict[int, int], List[int]]:
    """Assign each node to a community.

    Uses greedy modularity maximisation (networkx) when available; that
    finds densely-connected groups even inside one connected graph, so
    a substantive split between sub-literatures is detected at ANY
    edge threshold, not just once the weak ties have been filtered
    away.

    Falls back to connected components (via union-find) if networkx is
    not installed -- the result is then much less useful because weak
    edges merge everything into one component, but the function still
    returns something rather than blowing up.

    Parameters
    ----------
    node_ids : iterable of int
        Every node that should appear in the partition.  Nodes with no
        incident edge are assigned to a singleton community at the end.
    edges : sequence of (x, y, w)
        Weighted undirected edges.

    Returns
    -------
    comp_of : dict {node_id: community_index}
    sizes   : list of community sizes, parallel to community_index;
              communities are numbered so 0 is the largest, 1 is the
              second largest, and so on.
    """
    node_ids = list(node_ids)
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
            comp_of: Dict[int, int] = {}
            for idx, members in enumerate(ordered):
                for n in members:
                    comp_of[n] = idx
            # nodes networkx left out (no edges) get their own communities
            nxt = len(ordered)
            sizes = [len(m) for m in ordered]
            for n in node_ids:
                if n not in comp_of:
                    comp_of[n] = nxt
                    sizes.append(1)
                    nxt += 1
            return comp_of, sizes

    # ---- fallback: connected components via union-find ----
    parent = {n: n for n in node_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for x, y, _w in edges:
        ra, rb = find(x), find(y)
        if ra != rb:
            parent[ra] = rb

    groups: Dict[int, List[int]] = defaultdict(list)
    for n in node_ids:
        groups[find(n)].append(n)
    ordered_groups = sorted(groups.values(), key=len, reverse=True)
    comp_of = {}
    for idx, members in enumerate(ordered_groups):
        for n in members:
            comp_of[n] = idx
    return comp_of, [len(m) for m in ordered_groups]


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
def _compute_edge_weights(c_ij: int, c_i: int, c_j: int,
                          n_selected: int) -> Tuple[float, float, float]:
    """Return (cosine, jaccard, association_strength_scaled) for one pair.

    All three are derived from (c_ij, c_i, c_j) plus N for the scaled
    association strength.  See the module docstring's edge-weight-modes
    section for the formulae.  Returns 0.0 for any variant whose
    denominator would be zero -- in practice this only happens with
    pathological inputs, since c_ij > 0 implies c_i, c_j > 0.
    """
    if c_i > 0 and c_j > 0:
        cosine = c_ij / math.sqrt(c_i * c_j)
        ass_scaled = (n_selected * c_ij) / (c_i * c_j) if n_selected else 0.0
    else:
        cosine = 0.0
        ass_scaled = 0.0
    union = c_i + c_j - c_ij
    jaccard = c_ij / union if union > 0 else 0.0
    return cosine, jaccard, ass_scaled


def _pick_selected_weight(mode: str, c_ij: int, cosine: float,
                          jaccard: float, ass_scaled: float):
    """Pick the active edge weight for the chosen mode.

    Returns int(c_ij) in raw mode, float otherwise.  Keeping raw mode's
    selected_weight as an int preserves byte-equivalence with the
    pre-normalisation output (the dashboard ignored the field then;
    callers that read it now still get the same numeric value).
    """
    if mode == "cosine":
        return cosine
    if mode == "jaccard":
        return jaccard
    if mode == "association_strength_scaled":
        return ass_scaled
    return c_ij  # raw


def build_cocitation_graph(
    corpus: Mapping,
    pairs: Mapping,
    *,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    journal: Optional[str] = None,
    min_strength: int = 2,
    min_citations: int = 0,
    max_nodes: int = 150,
    edge_weight_mode: str = "raw",
    min_normalized_weight: float = 0.0,
) -> dict:
    """Compute the co-citation network for a filtered article subset.

    Parameters
    ----------
    corpus, pairs
        The two dicts returned by ``load_corpus``.
    year_from, year_to
        Inclusive citing-article year range.  Defaults to the corpus's
        full range when None.  Articles with a missing year are
        excluded by the lower bound check (treated as not-passing).
    journal
        Optional citing-journal filter.  None or empty string means no
        filter.
    min_strength
        EDGE filter on the raw c_ij count.  Always applied, regardless
        of edge_weight_mode -- this is what stops a 1-or-2 co-citation
        pair from looking "strong" under normalisation.
    min_citations
        NODE filter.  Applied BEFORE edges are considered: any
        reference cited fewer than ``min_citations`` times in the
        selection is dropped, and none of its links appear.
    max_nodes
        Cap on the number of distinct references shown, ranked by
        local citation count (with degree as tiebreaker).  Edges
        between dropped nodes are dropped too.
    edge_weight_mode
        One of "raw", "cosine", "jaccard", "association_strength_scaled".
        Selects which weight drives clustering, the ``selected_weight``
        field, and the optional normalised filter below.  See module
        docstring for formulae and scale notes.  Default "raw"
        reproduces the original dashboard behaviour.
    min_normalized_weight
        SECOND edge filter, applied on top of ``min_strength``.  Edges
        are dropped unless their selected_weight (under the active
        mode) is at least this value.  Default 0 means the normalised
        filter is inactive; combined with edge_weight_mode="raw" this
        leaves the original behaviour untouched.

    Returns
    -------
    dict with the following keys:

      ``nodes``  -- list of node dicts (id, label, year, total, local,
                    degree, cluster, cross, intra, w_cross, w_intra,
                    reach, bridge_ratio, bridge).  w_cross / w_intra
                    sum RAW c_ij even when a normalised mode is
                    selected, so they remain interpretable across
                    modes.
      ``edges``  -- list of edge dicts.  Each carries:
                       s, t           - endpoint reference ids
                       w              - raw c_ij (always; backward compat)
                       cross          - True iff edge spans clusters
                       source_local   - c_i, citations of source in selection
                       target_local   - c_j, citations of target in selection
                       cosine, jaccard, association_strength_scaled
                                      - the three normalised weights,
                                        always present for tooltip /
                                        export use
                       selected_weight - the weight under
                                        edge_weight_mode (drives
                                        clustering & ranking)
      ``clusters``           -- community sizes in cluster-id order.
      ``selected_articles``  -- number of citing articles passing the
                                year + journal filter.
      ``strong_pairs``       -- number of pairs surviving BOTH the
                                raw and normalised edge filters,
                                BEFORE the max_nodes cap.
      ``edge_weight_mode``   -- echo of the mode actually used.

    The function is pure: same inputs always produce the same output
    (modulo set iteration order; see the determinism check in the
    module's CLI).
    """
    if edge_weight_mode not in EDGE_WEIGHT_MODES:
        raise ValueError(
            f"edge_weight_mode must be one of {EDGE_WEIGHT_MODES}; "
            f"got {edge_weight_mode!r}")

    refs = corpus["refs"]
    articles = corpus["articles"]
    PX = pairs["x"]
    PY = pairs["y"]
    PARTS = pairs["arts"]
    n_pairs = len(PX)

    if year_from is None:
        year_from = corpus["year_min"]
    if year_to is None:
        year_to = corpus["year_max"]
    # treat empty string as "no journal filter" (matches Flask handler)
    if journal is None:
        journal = ""

    # 1. boolean mask over citing articles: does each pass the filter?
    passes = [False] * len(articles)
    selected = 0
    for ai, a in enumerate(articles):
        ay = a["year"]
        if ay is None or ay < year_from or ay > year_to:
            continue
        if journal and a["journal"] != journal:
            continue
        passes[ai] = True
        selected += 1

    # 2. local citation count per reference within the selection
    #    (independent of edges -- the node-level filter needs this).
    ref_local_total: Dict[int, int] = defaultdict(int)
    for ai, a in enumerate(articles):
        if passes[ai]:
            for r in a["refs"]:
                ref_local_total[r] += 1

    # 3. node-level filter: references prominent enough to be eligible
    eligible = {r for r, c in ref_local_total.items()
                if c >= min_citations}

    # 4. walk the precomputed pair index, recompute c_ij against the
    #    passing-articles mask, then apply BOTH edge filters:
    #      - the raw threshold (min_strength on c_ij), and
    #      - the normalised threshold (min_normalized_weight on the
    #        active mode's weight).
    #    Both endpoints must be eligible nodes.  Each surviving edge is
    #    carried with all four weight variants so downstream stages
    #    (and exports) need not recompute them.
    raw_edges = []  # list of (x, y, c_ij, cosine, jaccard, ass_scaled, sel)
    strong_pairs = 0
    for k in range(n_pairs):
        x, y = PX[k], PY[k]
        if x not in eligible or y not in eligible:
            continue
        c_ij = 0
        for ai in PARTS[k]:
            if passes[ai]:
                c_ij += 1
        # Apply the RAW threshold first, on c_ij.  This guards against
        # the artefact of a pair with c_ij = 1 or 2 looking "strong"
        # under normalisation when both endpoints are themselves rare.
        if c_ij < min_strength:
            continue
        c_i = ref_local_total[x]
        c_j = ref_local_total[y]
        cosine, jaccard, ass_scaled = _compute_edge_weights(
            c_ij, c_i, c_j, selected)
        selected_w = _pick_selected_weight(
            edge_weight_mode, c_ij, cosine, jaccard, ass_scaled)
        # Second (optional) filter, in the units of the active mode.
        # With default min_normalized_weight=0 this is a no-op.
        if selected_w < min_normalized_weight:
            continue
        strong_pairs += 1
        raw_edges.append((x, y, c_ij, cosine, jaccard, ass_scaled,
                          selected_w))

    if not raw_edges:
        return {"nodes": [], "edges": [], "selected_articles": selected,
                "strong_pairs": 0, "clusters": [],
                "edge_weight_mode": edge_weight_mode}

    # 5. cap the displayed node set by local citation count.  Ranking
    #    uses local citations + degree as tiebreaker, NOT edge weights,
    #    so the cap is mode-independent.
    deg: Dict[int, int] = defaultdict(int)
    for x, y, *_ in raw_edges:
        deg[x] += 1
        deg[y] += 1
    candidates = sorted(deg.keys(),
                        key=lambda r: (ref_local_total[r], deg[r]),
                        reverse=True)
    keep = set(candidates[:max_nodes])
    raw_edges = [e for e in raw_edges if e[0] in keep and e[1] in keep]

    if not raw_edges:
        return {"nodes": [], "edges": [], "selected_articles": selected,
                "strong_pairs": strong_pairs, "clusters": [],
                "edge_weight_mode": edge_weight_mode}

    # 6. assemble the node set from surviving edges
    node_ids = set()
    edge_deg: Dict[int, int] = defaultdict(int)
    for x, y, *_ in raw_edges:
        node_ids.add(x)
        node_ids.add(y)
        edge_deg[x] += 1
        edge_deg[y] += 1

    # 7. cluster on the SELECTED weight -- this is the main place
    #    normalisation changes the analysis.  Modularity is unitless,
    #    so passing floats is fine.
    cluster_input = [(x, y, sel) for x, y, _c, _co, _ja, _as, sel
                     in raw_edges]
    comp_of, comp_sizes = detect_communities(node_ids, cluster_input)

    # Bridging aggregates use RAW c_ij so a node's w_cross / w_intra
    # remain interpretable as "citation strength of cross/intra links"
    # regardless of which mode is active.
    cross_links: Dict[int, int] = defaultdict(int)
    intra_links: Dict[int, int] = defaultdict(int)
    w_cross: Dict[int, int] = defaultdict(int)
    w_intra: Dict[int, int] = defaultdict(int)
    reach: Dict[int, set] = defaultdict(set)
    for x, y, c_ij, *_ in raw_edges:
        cx, cy = comp_of[x], comp_of[y]
        if cx == cy:
            intra_links[x] += 1
            intra_links[y] += 1
            w_intra[x] += c_ij
            w_intra[y] += c_ij
        else:
            cross_links[x] += 1
            cross_links[y] += 1
            w_cross[x] += c_ij
            w_cross[y] += c_ij
            reach[x].add(cy)
            reach[y].add(cx)

    # refs[i] has its own "i" field equal to its index in the list
    # (see preprocess.py), so direct indexing works as a per-node lookup.
    nodes = []
    for nid in node_ids:
        r = refs[nid]
        cr, it = cross_links[nid], intra_links[nid]
        total_links = cr + it
        ratio = (cr / total_links) if total_links else 0.0
        nodes.append({
            "id": nid,
            "label": r["label"],
            "year": r["year"],
            "total": r["total"],
            "local": ref_local_total[nid],
            "degree": edge_deg[nid],
            "cluster": comp_of[nid],
            "cross": cr,
            "intra": it,
            "w_cross": w_cross[nid],
            "w_intra": w_intra[nid],
            "reach": len(reach[nid]),
            "bridge_ratio": round(ratio, 3),
            "bridge": cr > 0 and ratio >= 0.25,
        })

    return {
        "nodes": nodes,
        "edges": [{
            "s": x, "t": y,
            "w": c_ij,                       # raw c_ij, always
            "cross": comp_of[x] != comp_of[y],
            "source_local": ref_local_total[x],
            "target_local": ref_local_total[y],
            "cosine": round(cosine, 4),
            "jaccard": round(jaccard, 4),
            "association_strength_scaled": round(ass_scaled, 4),
            "selected_weight": (sel if edge_weight_mode == "raw"
                                else round(sel, 4)),
        } for x, y, c_ij, cosine, jaccard, ass_scaled, sel in raw_edges],
        "clusters": comp_sizes,
        "selected_articles": selected,
        "strong_pairs": strong_pairs,
        "edge_weight_mode": edge_weight_mode,
    }


# ---------------------------------------------------------------------------
# Export helpers (used by the __main__ block)
# ---------------------------------------------------------------------------
def to_networkx_graph(result: dict):
    """Build a networkx.Graph carrying the node/edge attributes.

    Used as an intermediate step for GraphML output and any other
    networkx-based downstream analysis a collaborator might run.
    """
    if not _HAVE_NX:
        raise RuntimeError(
            "networkx is required for to_networkx_graph(); install via "
            "`pip install networkx`."
        )
    G = nx.Graph()
    for n in result["nodes"]:
        # GraphML doesn't allow None; coerce missing year to -1 so the
        # column survives the round-trip.
        attrs = {k: ("" if v is None else v)
                 for k, v in n.items() if k != "id"}
        G.add_node(n["id"], **attrs)
    for e in result["edges"]:
        G.add_edge(e["s"], e["t"], weight=e["w"], cross=bool(e["cross"]))
    return G


def write_nodes_csv(result: dict, path: str) -> None:
    """One row per node with all bridging + citation metrics.

    Cluster ID is written 1-indexed for spreadsheet friendliness
    (matches the dashboard's "Export visible graph" CSV).
    """
    cols = [
        "ref_id", "label", "year", "local_citations", "total_citations",
        "cluster", "degree", "co_citation_strength_total",
        "cross_cluster_links", "intra_cluster_links",
        "cross_strength", "intra_strength",
        "clusters_reached", "bridge_ratio", "is_bridge",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for n in sorted(result["nodes"], key=lambda x: x["id"]):
            w.writerow([
                n["id"], n["label"],
                "" if n["year"] is None else n["year"],
                n["local"], n["total"],
                n["cluster"] + 1,  # 1-indexed for readability
                n["degree"], n["w_cross"] + n["w_intra"],
                n["cross"], n["intra"],
                n["w_cross"], n["w_intra"],
                n["reach"], n["bridge_ratio"],
                "true" if n["bridge"] else "false",
            ])


def write_edges_csv(result: dict, path: str) -> None:
    """One row per edge with full weight breakdown + labels.

    ``weight`` always carries the raw co-citation count for backward
    compatibility; ``selected_weight`` carries the value under the mode
    the network was built with.  The three normalised variants are
    written regardless of mode so the colleague can recompute or
    re-rank in their tool of choice.
    """
    by_id = {n["id"]: n for n in result["nodes"]}
    mode = result.get("edge_weight_mode", "raw")
    cols = [
        "source_id", "source_label", "target_id", "target_label",
        "weight",                       # = raw c_ij (backward compat)
        "raw_cocitations",              # explicit alias
        "source_local_citations",       # c_i
        "target_local_citations",       # c_j
        "cosine_weight", "jaccard_weight",
        "association_strength_scaled",
        "selected_weight", "edge_weight_mode",
        "cross_cluster",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for e in sorted(result["edges"], key=lambda x: (x["s"], x["t"])):
            sl = by_id.get(e["s"], {}).get("label", "")
            tl = by_id.get(e["t"], {}).get("label", "")
            w.writerow([
                e["s"], sl, e["t"], tl,
                e["w"], e["w"],                       # weight + raw_cocitations
                e.get("source_local", ""),
                e.get("target_local", ""),
                e.get("cosine", ""),
                e.get("jaccard", ""),
                e.get("association_strength_scaled", ""),
                e.get("selected_weight", e["w"]),
                mode,
                "true" if e["cross"] else "false",
            ])


def write_graphml(result: dict, path: str) -> None:
    """Write the network as GraphML for Gephi / VOSviewer / yEd."""
    G = to_networkx_graph(result)
    nx.write_graphml(G, path)


# ---------------------------------------------------------------------------
# CLI -- runs the analysis end-to-end and writes export files
# ---------------------------------------------------------------------------
def _main_cli():
    import argparse
    here = os.path.dirname(os.path.abspath(__file__))
    default_corpus = os.path.join(here, "data", "corpus.json")
    default_pairs = os.path.join(here, "data", "pairs.json")
    default_prefix = os.path.join(here, "data", "cocite_export")

    ap = argparse.ArgumentParser(
        description="Build a co-citation network from the bundled "
                    "corpus + pair index and write it as GraphML and "
                    "node/edge CSV files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--corpus", default=default_corpus,
                    help="path to corpus.json")
    ap.add_argument("--pairs", default=default_pairs,
                    help="path to pairs.json")
    ap.add_argument("--output-prefix", default=default_prefix,
                    help="prefix for output files; will produce "
                         "PREFIX.graphml, PREFIX_nodes.csv, "
                         "PREFIX_edges.csv")
    ap.add_argument("--year-from", type=int, default=None,
                    help="lower bound on citing-article year "
                         "(inclusive); default = corpus minimum")
    ap.add_argument("--year-to", type=int, default=None,
                    help="upper bound on citing-article year "
                         "(inclusive); default = corpus maximum")
    ap.add_argument("--journal", default="",
                    help="restrict to one citing journal "
                         "(case-sensitive exact match); empty = all")
    ap.add_argument("--min-strength", type=int, default=5,
                    help="EDGE filter -- minimum co-citation strength "
                         "(articles co-citing a pair)")
    ap.add_argument("--min-citations", type=int, default=0,
                    help="NODE filter -- minimum citations within the "
                         "selection for a reference to appear")
    ap.add_argument("--max-nodes", type=int, default=150,
                    help="cap on number of nodes (ranked by local "
                         "citation count, degree as tiebreaker)")
    ap.add_argument("--edge-weight-mode",
                    choices=EDGE_WEIGHT_MODES, default="raw",
                    help="how edge weights are computed; drives "
                         "community detection, ranking, and the "
                         "selected_weight column")
    ap.add_argument("--min-normalized-weight", type=float, default=0.0,
                    help="SECOND edge filter, applied on top of "
                         "min-strength, in the units of the active "
                         "edge-weight mode")
    ap.add_argument("--check-determinism", action="store_true",
                    help="run the analysis twice and confirm the "
                         "result is identical before writing")
    args = ap.parse_args()

    print(f"loading corpus from {args.corpus}")
    corpus, pairs = load_corpus(args.corpus, args.pairs)
    print(f"  {len(corpus['refs']):,} references, "
          f"{len(corpus['articles']):,} citing articles, "
          f"{len(pairs['x']):,} indexed co-citation pairs")

    print(f"building network "
          f"(year={args.year_from}-{args.year_to}, "
          f"min_strength={args.min_strength}, "
          f"min_citations={args.min_citations}, "
          f"max_nodes={args.max_nodes}, "
          f"journal={args.journal or 'all'}, "
          f"edge_weight_mode={args.edge_weight_mode}, "
          f"min_normalized_weight={args.min_normalized_weight})")
    result = build_cocitation_graph(
        corpus, pairs,
        year_from=args.year_from, year_to=args.year_to,
        journal=args.journal,
        min_strength=args.min_strength,
        min_citations=args.min_citations,
        max_nodes=args.max_nodes,
        edge_weight_mode=args.edge_weight_mode,
        min_normalized_weight=args.min_normalized_weight,
    )

    n_bridges = sum(1 for n in result["nodes"] if n["bridge"])
    print(f"  {len(result['nodes'])} nodes, {len(result['edges'])} edges, "
          f"{len(result['clusters'])} clusters, {n_bridges} bridging works")
    print(f"  cluster sizes (largest first): {result['clusters']}")
    print(f"  selected articles: {result['selected_articles']}; "
          f"pairs above strength threshold: {result['strong_pairs']}")

    if args.check_determinism:
        # Compute again and compare.  Any difference (clustering,
        # edge set, anything) means there is a nondeterminism in the
        # pipeline.
        print("re-running to verify determinism...")
        result2 = build_cocitation_graph(
            corpus, pairs,
            year_from=args.year_from, year_to=args.year_to,
            journal=args.journal,
            min_strength=args.min_strength,
            min_citations=args.min_citations,
            max_nodes=args.max_nodes,
            edge_weight_mode=args.edge_weight_mode,
            min_normalized_weight=args.min_normalized_weight,
        )
        a = json.dumps(_canonical(result), sort_keys=True)
        b = json.dumps(_canonical(result2), sort_keys=True)
        if a == b:
            print("  determinism: OK (identical output)")
        else:
            print("  determinism: WARNING -- outputs differ between runs")

    if not result["nodes"]:
        print("no nodes survived the filters -- nothing to export")
        return

    graphml_path = args.output_prefix + ".graphml"
    nodes_path = args.output_prefix + "_nodes.csv"
    edges_path = args.output_prefix + "_edges.csv"
    os.makedirs(os.path.dirname(graphml_path) or ".", exist_ok=True)

    write_graphml(result, graphml_path)
    write_nodes_csv(result, nodes_path)
    write_edges_csv(result, edges_path)
    print(f"wrote {graphml_path}")
    print(f"wrote {nodes_path}")
    print(f"wrote {edges_path}")


def _canonical(result: dict) -> dict:
    """Sort nodes/edges by id so two results can be compared
    independently of set iteration order."""
    return {
        **result,
        "nodes": sorted(result["nodes"], key=lambda n: n["id"]),
        "edges": sorted(result["edges"], key=lambda e: (e["s"], e["t"])),
    }


if __name__ == "__main__":
    _main_cli()
