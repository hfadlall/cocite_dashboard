# CLAUDE.md — Bibliometric Co-Citation Network

This file orients Claude Code to the project. Read it before making changes.

## What this project is

A locally-served interactive dashboard for exploring co-citation
patterns in *any* bibliographic corpus.  The user uploads a long-format
citation CSV via the sidebar; the dashboard recomputes the network on
the fly for the selected subset of citing articles.  No data is bundled
with the dashboard — if no CSV is uploaded, the canvas shows an upload
prompt.

It is a working analytical tool used in academic research, not a
product.  Published figures for the manuscripts that use it are
produced in VOSviewer; this dashboard is exploratory scaffolding.

## The owner

Hussein — a PhD-trained management scholar.  The tool was originally
built to support a manuscript on the political role of the firm
(corporate political activity / political CSR), arguing via
co-citation structure that the two sub-literatures have become
weakly-connected clusters.  The tool has since been generalised so a
collaborator can drop in any compatible CSV.  Domain-specific framing
should NOT appear in code, UI copy, or docstrings; keep them
corpus-agnostic.

Hussein is comfortable in the terminal but is not a software engineer.
Explain non-trivial changes in plain terms. Prefer clear, well-commented
code over clever code.

## Architecture

```
cocite_dashboard/
  preprocess.py    long-format CSV -> (corpus, pairs) dicts.  build_from_csv
                   is imported by app.py to parse uploaded CSVs at runtime;
                   the script can also be invoked directly to write
                   data/corpus.json + data/pairs.json for offline use.
  cocitation.py    standalone analytical core (no Flask dep): subgraph
                   filtering, four edge-weight modes, modularity
                   clustering, bridging metrics.  Imported by app.py;
                   also runs as a CLI that writes GraphML + node/edge
                   CSV for sharing.
  app.py           Flask backend; endpoints /api/meta, /api/graph,
                   /api/load, /api/reset.  Starts with no data; the
                   user must upload a CSV.
  requirements.txt Flask + networkx
  data/            gitignored at runtime; preprocess.py + cocitation.py
                   CLI write artefacts here for local inspection
  static/
    index.html     dashboard page + all CSS
    app.js         force layout, canvas rendering, controls
  README.md        user-facing documentation
```

Run: `python app.py`, then open http://127.0.0.1:5000 and upload a CSV.

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
