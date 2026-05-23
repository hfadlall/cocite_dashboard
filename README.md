# Bibliometric Co-Citation Network

An interactive dashboard for exploring co-citation structure in any
bibliographic corpus.  The user supplies a long-format citation CSV
via the upload control; the dashboard recomputes the network live
for any chosen subset of citing articles.

Two references are **co-cited** whenever they appear together in the
same article's reference list.  The more articles cite both, the
stronger the link between them.  Because filters change *which*
articles contribute, edge strengths and the resulting clustering are
recomputed for the selected subset (subgraph semantics), not read
off a fixed graph.

## What you can do

- **Filter by citing-article year** — restrict which papers contribute
  links (e.g. "what does work published 2010–2020 co-cite?").
- **Filter by citing journal** — isolate one journal's co-citation
  structure.
- **Set the minimum co-citation strength** — an edge is drawn only if
  at least N selected articles co-cite the pair.
- **Set the minimum citation count** — a node filter: drop references
  cited fewer than M times in the selection before any edge is
  considered.
- **Cap the node count** — keep the view legible; ranks references by
  how often they are cited within the current selection.
- **Switch edge-weight modes** — raw co-citation count (default),
  cosine, Jaccard, or scaled association strength.  Each rescales
  edges differently; tooltips show all four values regardless of
  which is active.
- **Colour nodes by era or by cluster.**
- **Bridging mode** — fade each cluster and highlight references
  whose links cross between clusters.
- **Browse the strongest pairs / bridging works** — ranked panel,
  top-right toggle.
- **Search** — highlight references by author or title.
- **Export** — node CSV and edge CSV of the visible subgraph, with
  the full weight breakdown.

## CSV format

Long format, one row per citation edge.  Required columns:

- `citing_UT`             — unique id of the citing article
- `cited_canonical_id`    — unique id of the cited reference

Optional columns (used when present, treated as blank when absent):

- `citing_year`, `citing_journal`, `citing_title`, `citing_DOI`
- `cited_label`, `cited_year`

Rows missing either required column are silently skipped, so the
parser tolerates hand-edited CSVs with omissions.

## Setup

Requires Python 3.9+ and the two declared dependencies (Flask,
networkx).

```bash
cd cocite_dashboard
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5000> and upload a CSV via the Corpus
panel in the sidebar.  No data is bundled with the dashboard; if no
CSV is uploaded, the canvas shows an upload prompt.

For a hosted deployment (Vercel etc.) the same flow applies: the
server holds the corpus in memory only while the function is warm,
and the user re-uploads after a cold start.

## Working outside the dashboard

`cocitation.py` is a standalone, Flask-independent module containing
the analytical core.  Use it from a notebook or a one-off script:

```python
from cocitation import load_corpus, build_cocitation_graph
# corpus.json + pairs.json are produced by preprocess.py from a CSV
corpus, pairs = load_corpus("data/corpus.json", "data/pairs.json")
result = build_cocitation_graph(
    corpus, pairs,
    min_strength=5, min_citations=0, max_nodes=150,
    edge_weight_mode="cosine",     # or "raw" / "jaccard" /
                                   #   "association_strength_scaled"
    min_normalized_weight=0.3,
)
# -> {"nodes": [...], "edges": [...], "clusters": [...], ...}
```

The module also runs as a CLI:

```bash
python preprocess.py /path/to/master_citation_dataset.csv
python cocitation.py --output-prefix data/exports/run1 \
                    --edge-weight-mode cosine \
                    --min-normalized-weight 0.3
```

This writes GraphML + node/edge CSVs suitable for Gephi, VOSviewer,
or any spreadsheet tool.

## Project layout

```
cocite_dashboard/
  preprocess.py        CSV -> (corpus, pairs) dicts; also writes
                       data/corpus.json + data/pairs.json
  cocitation.py        standalone analytical core; Flask-independent
  app.py               Flask backend: /api/meta, /api/graph,
                       /api/load, /api/reset
  requirements.txt     Flask + networkx
  static/
    index.html         dashboard page + CSS
    app.js             force layout, canvas rendering, controls
```

## API

`GET /api/meta` — corpus summary.  Returns `{loaded: bool, source,
year_min, year_max, journals, n_articles, n_refs, n_pairs}`.
`loaded:false` means no CSV has been uploaded yet.

`GET /api/graph` — the filtered co-citation graph.  Returns 400 with
`{error, no_corpus:true}` if no CSV has been uploaded.  Query
parameters:

| param                   | meaning                                                    |
|-------------------------|------------------------------------------------------------|
| `year_from`             | earliest citing-article year                               |
| `year_to`               | latest citing-article year                                 |
| `journal`               | exact citing-journal name, or empty for all                |
| `min_strength`          | minimum articles co-citing a pair (>= 2)                   |
| `min_citations`         | minimum citations of a reference in the selection          |
| `max_nodes`             | cap on nodes shown (10–600)                                |
| `edge_weight_mode`      | `raw` / `cosine` / `jaccard` / `association_strength_scaled` |
| `min_normalized_weight` | optional floor on the active mode's edge weight            |

`POST /api/load` — multipart upload of a citation CSV (gzip-encoded
on the wire is supported and recommended).  Replaces the active
dataset.

`POST /api/reset` — clear the loaded corpus.  After this the dashboard
returns to the "no corpus loaded" state until the next upload.

## Notes

- The dashboard is corpus-agnostic; the analytical core does no
  domain assumptions.  CLAUDE.md retains some context from the
  research project that motivated the original build, but the tool
  itself works on any bibliographic CSV that follows the column
  shape above.
- `local` (citations within the current selection) drives node
  sizing and ranking; `total` (corpus-wide) is shown in tooltips for
  context.
- The force layout runs a fixed number of iterations on each
  rebuild, then stops; drag any node to nudge it.
- The pair index inside `preprocess.py` keeps pairs co-cited at
  least twice corpus-wide.  A pair co-cited only once can never meet
  a meaningful strength threshold, so the `min_strength` slider's
  floor is 2.
