# Co-Citation Network Dashboard

An interactive, locally-served dashboard for exploring co-citation patterns
in the *Political Role of the Firm* bibliometric corpus (686 citing
articles, ~32,800 distinct cited references, 14 management journals).

Two references are **co-cited** whenever they appear together in the same
article's reference list. The more articles cite both, the stronger the
link between them. This dashboard recomputes that network live for any
subset of citing articles you select.

## What you can do

- **Filter by citing-article year** — restrict which papers contribute
  links (e.g. "what did work published 2010–2020 co-cite?").
- **Filter by citing journal** — isolate one journal's co-citation
  structure (e.g. *Journal of Business Ethics* vs *Strategic Management
  Journal*).
- **Set the minimum co-citation strength** — the slider; an edge is drawn
  only if at least N selected articles co-cite the pair.
- **Cap the node count** — keeps the view legible; ranks references by how
  often they are cited within the current selection.
- **Color nodes by era** — decade of the cited work.
- **Browse the strongest pairs** — ranked table, top-right toggle.
- **Search** — highlight references by author or title.

Because journal/year filtering changes *which* articles contribute,
edge strengths are recomputed for the selected subset (subgraph
semantics) — not read off a fixed corpus-wide graph.

## Setup

Requires Python 3.9+ and one dependency (Flask).

```bash
cd cocite_dashboard
pip install -r requirements.txt
```

## First run — preprocess the data

Place `master_citation_dataset.csv` anywhere, then:

```bash
python preprocess.py /path/to/master_citation_dataset.csv
```

This writes two files into `data/`:

- `corpus.json` — references and citing articles
- `pairs.json`  — the precomputed co-citation pair index

The pair index is what keeps the dashboard fast: it stores, for every
reference pair co-cited at least twice, the list of articles that
produced it. Filtering then reduces to counting which contributing
articles pass — every request stays in the millisecond range despite the
~2.4M raw reference-pair combinations in the corpus.

## Run

```bash
python app.py
```

Then open <http://127.0.0.1:5000>.

Or use the launcher, which preprocesses on first run if needed:

```bash
./run.sh /path/to/master_citation_dataset.csv   # first time
./run.sh                                        # later
```

## Project layout

```
cocite_dashboard/
  preprocess.py        CSV -> data/corpus.json + data/pairs.json
  app.py               Flask backend; /api/meta and /api/graph
  run.sh               convenience launcher
  requirements.txt
  data/
    corpus.json        generated
    pairs.json         generated
  static/
    index.html         dashboard page
    app.js             force layout, canvas rendering, controls
```

## API

`GET /api/meta` — year range, journal list, corpus counts.

`GET /api/graph` — the filtered co-citation graph. Query parameters:

| param          | meaning                                          |
|----------------|--------------------------------------------------|
| `year_from`    | earliest citing-article year                     |
| `year_to`      | latest citing-article year                       |
| `min_strength` | minimum articles co-citing a pair (>= 2)          |
| `max_nodes`    | cap on nodes shown (10–600)                       |
| `journal`      | exact citing-journal name, or empty for all       |

Returns nodes (with `total` corpus-wide citations and `local` citations
within the selection) and edges (with co-citation weight `w`).

## Notes

- The pair index keeps pairs co-cited **at least twice** corpus-wide. A
  pair co-cited only once can never meet a meaningful strength threshold,
  so the `min_strength` slider's floor is 2.
- `local` (citations within the current selection) drives node sizing and
  ranking; `total` (corpus-wide) is shown in tooltips for context.
- The force layout runs a fixed number of iterations on each rebuild,
  then stops — drag any node to nudge it; the graph does not jitter.
- To regenerate after changing the source CSV, rerun `preprocess.py`.
