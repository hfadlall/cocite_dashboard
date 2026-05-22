# CLAUDE.md — Co-Citation Network Dashboard

This file orients Claude Code to the project. Read it before making changes.

## What this project is

A locally-served interactive dashboard for exploring co-citation patterns
in a bibliometric corpus on **the political role of the firm** (corporate
political activity / CPA and political CSR / PCSR research). It is a
working analytical tool supporting an academic manuscript — not a product.

The corpus: 686 citing articles drawn from 14 management journals,
~32,800 distinct cited references, ~77,300 co-citation pairs.

## The owner

Hussein — a PhD-trained management scholar. The manuscript this dashboard
supports argues, via co-citation structure, that CPA and PCSR research
have become separated into weakly-connected clusters (a field-level
reading of Freeman's separation thesis). The dashboard is exploratory
scaffolding for that argument; it is NOT the citable artifact. Published
figures for the paper are produced in VOSviewer.

Hussein is comfortable in the terminal but is not a software engineer.
Explain non-trivial changes in plain terms. Prefer clear, well-commented
code over clever code.

## Architecture

```
cocite_dashboard/
  preprocess.py    master_citation_dataset.csv -> data/corpus.json + data/pairs.json
  app.py           Flask backend; endpoints /api/meta and /api/graph
  run.sh           convenience launcher
  requirements.txt Flask + networkx
  data/            corpus.json, pairs.json  (generated; do not hand-edit)
  static/
    index.html     dashboard page + all CSS
    app.js         force layout, canvas rendering, controls
  README.md        user-facing documentation
```

Run: `python app.py`, then open http://127.0.0.1:5000

## Core concepts — do not get these wrong

**Co-citation.** Two references are co-cited when the same citing article
lists both. An edge's weight = the number of citing articles that co-cite
that pair.

**Subgraph semantics.** Filtering by citing-article year or journal changes
WHICH articles contribute. Edge weights and clusters are recomputed for
the filtered subset — never read off a fixed corpus-wide graph. This is
the whole reason the tool is server-side.

**Two filters, two distinct objects — never conflate them:**
- `min_strength`  — EDGE filter. Minimum articles co-citing a pair for the
  link to be drawn. A property of a pair. Measures relatedness.
- `min_citations` — NODE filter. Minimum times a reference is cited within
  the selection for it to appear at all. A property of a single reference.
  Measures influence. Applied BEFORE edges are considered.

The informative regime for studying emerging bridges between clusters is
HIGH citation strength + LOW co-citation strength: keep only prominent
references as nodes, but allow weak links so faint cross-cluster ties
survive.

**Clustering.** Communities are detected with greedy modularity
maximisation (networkx), with a connected-components fallback if networkx
is absent. Modularity is used deliberately: connected-components collapses
the whole graph into one component once weak edges are included, so it
only "finds" the CPA/PCSR split at a high edge threshold — exactly where
the bridges of interest have already been filtered away. Clusters are
numbered by size, largest first.

There is NO canonical cluster ID. VOSviewer assigns clusters via its own
algorithm at a chosen resolution parameter; numbering is not portable
across tools or settings. The dashboard's clusters are detected fresh per
filter setting — analytically conventional (modularity family) but not
identical to any VOSviewer run. Keep this honest in any UI copy.

**Bridging.** A genuine bridge is a reference whose links are
DISPROPORTIONATELY cross-cluster — measured by bridge_ratio =
cross / (cross + intra). Raw cross-link count is NOT the metric: mega-cited
hubs have many cross links simply by volume. A node is flagged `bridge`
when it has >=1 cross-cluster link AND bridge_ratio >= 0.25.

Caveat to preserve in any related work: bridging mode surfaces references
whose CITATION POSITION spans clusters. The lightly-cited paper actually
doing the bridging may itself fall below the node filter; what survives is
the cross-cluster link it created between prominent anchors.

## Performance

`preprocess.py` builds a pair index once: every reference pair co-cited
>= 2 times, with the list of contributing article indices. The backend
then recomputes filtered graphs by counting which contributing articles
pass — keeps every request in the tens-of-milliseconds range. Do not
replace this with per-request pairwise recomputation (that was the
original approach; it took ~2.4s and was abandoned).

If `master_citation_dataset.csv` changes, rerun `preprocess.py` to
regenerate `data/`.

## Conventions

- Keep the dependency footprint small: Flask and networkx only. Justify
  any new dependency before adding it.
- Backend is plain Flask, single file, synchronous. Keep it simple to run.
- Frontend is vanilla JS + canvas, no build step, no framework. All CSS
  lives in index.html. Keep it that way unless there is a strong reason.
- No browser localStorage/sessionStorage.
- The aesthetic is restrained and editorial (warm paper background, serif
  display type, monospace labels). Match it; do not introduce a generic
  dashboard look.
- Comment the WHY, not just the WHAT, especially in the co-citation and
  bridging logic.

## Likely next tasks (discussed, not yet built)

- Pinned vs emergent clusters: optionally compute clusters once at a
  reference setting and hold them fixed, so a reference keeps its color
  across filter changes. Currently clusters are purely emergent.
- CSV export of the currently visible subgraph (nodes + edges).
- A named-bridging-works view: identify the individual lightly-cited
  papers doing the bridging, not just the cross-cluster links. This is a
  harder analytical problem — requires keeping the node filter low and
  ranking actual bridge papers by their dual-cluster citation reach.
