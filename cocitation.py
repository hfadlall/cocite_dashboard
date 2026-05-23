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
it would only "find" the CPA/PCSR split at a high edge threshold --
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

Caveat: this surfaces references whose position bridges clusters,
which is not the same as identifying the lightly-cited paper that
actually created the bridge.  The bridging paper itself often falls
below the node filter; what survives is the cross-cluster link it
created between two prominent anchors.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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
    the CPA / PCSR style split is detected at ANY edge threshold, not
    just once the weak ties have been filtered away.

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
        EDGE filter.  Edges are kept only if at least this many of the
        passing articles co-cite both endpoints.  See the module
        docstring on the two-filters distinction.
    min_citations
        NODE filter.  Applied BEFORE edges are considered: any
        reference cited fewer than ``min_citations`` times in the
        selection is dropped, and none of its links appear.
    max_nodes
        Cap on the number of distinct references shown, ranked by
        local citation count (with degree as tiebreaker).  Edges
        between dropped nodes are dropped too.

    Returns
    -------
    dict with the following keys:

      ``nodes``  -- list of node dicts (id, label, year, total, local,
                    degree, cluster, cross, intra, w_cross, w_intra,
                    reach, bridge_ratio, bridge).
      ``edges``  -- list of edge dicts (s, t, w, cross) where ``cross``
                    is True iff the edge spans clusters.
      ``clusters`` -- list of community sizes in cluster-id order
                    (so ``clusters[0]`` is the size of cluster 0, which
                    is by construction the largest).
      ``selected_articles`` -- number of citing articles passing the
                    year + journal filter.
      ``strong_pairs``      -- number of pairs satisfying min_strength
                    over the selected articles, BEFORE the max_nodes
                    cap is applied.

    The function is pure: same inputs always produce the same output
    (modulo set iteration order; see notes in the module's __main__
    section for a determinism check).
    """
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

    # 4. walk the precomputed pair index, recomputing strength against
    #    the passing-articles mask.  An edge survives only if BOTH
    #    endpoints are eligible nodes; this is what makes the node
    #    filter strictly stronger than the edge filter.
    edges: List[Tuple[int, int, int]] = []
    strong_pairs = 0
    for k in range(n_pairs):
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

    # 5. cap the displayed node set by local citation count
    #    (degree as tiebreaker -- favours hubs that survive the filter).
    deg: Dict[int, int] = defaultdict(int)
    for x, y, _w in edges:
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

    # 6. assemble the node set from surviving edges
    node_ids = set()
    edge_deg: Dict[int, int] = defaultdict(int)
    for x, y, _w in edges:
        node_ids.add(x)
        node_ids.add(y)
        edge_deg[x] += 1
        edge_deg[y] += 1

    # 7. cluster + compute bridging metrics
    comp_of, comp_sizes = detect_communities(node_ids, edges)

    cross_links: Dict[int, int] = defaultdict(int)
    intra_links: Dict[int, int] = defaultdict(int)
    w_cross: Dict[int, int] = defaultdict(int)
    w_intra: Dict[int, int] = defaultdict(int)
    reach: Dict[int, set] = defaultdict(set)
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

    # refs[i] has its own "i" field equal to its index in the list
    # (see preprocess.py), so direct indexing works as a per-node lookup.
    nodes = []
    for nid in node_ids:
        r = refs[nid]
        cr, it = cross_links[nid], intra_links[nid]
        total_links = cr + it
        # bridge_ratio: share of a node's links that leave its community.
        # Separates genuine bridges (high ratio) from mega-cited hubs
        # that happen to have many cross links by sheer link volume.
        ratio = (cr / total_links) if total_links else 0.0
        nodes.append({
            "id": nid,
            "label": r["label"],
            "year": r["year"],
            "total": r["total"],            # corpus-wide citation count
            "local": ref_local_total[nid],  # citations within selection
            "degree": edge_deg[nid],
            "cluster": comp_of[nid],
            "cross": cr,                    # # of cross-cluster links
            "intra": it,                    # # of intra-cluster links
            "w_cross": w_cross[nid],        # strength sum of cross links
            "w_intra": w_intra[nid],        # strength sum of intra links
            "reach": len(reach[nid]),       # # of foreign clusters touched
            "bridge_ratio": round(ratio, 3),
            # a genuine bridge: at least one cross-cluster link AND a
            # quarter of all links cross-cluster.  The 0.25 cutoff is a
            # heuristic, not a derived threshold.
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
    """One row per edge with weight + cluster-crossing flag + labels."""
    by_id = {n["id"]: n for n in result["nodes"]}
    cols = ["source_id", "source_label", "target_id", "target_label",
            "weight", "cross_cluster"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for e in sorted(result["edges"], key=lambda x: (x["s"], x["t"])):
            sl = by_id.get(e["s"], {}).get("label", "")
            tl = by_id.get(e["t"], {}).get("label", "")
            w.writerow([e["s"], sl, e["t"], tl, e["w"],
                        "true" if e["cross"] else "false"])


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
          f"journal={args.journal or 'all'})")
    result = build_cocitation_graph(
        corpus, pairs,
        year_from=args.year_from, year_to=args.year_to,
        journal=args.journal,
        min_strength=args.min_strength,
        min_citations=args.min_citations,
        max_nodes=args.max_nodes,
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
