#!/usr/bin/env python3
"""
preprocess.py — Convert the master citation dataset into the structures
the dashboard backend needs.

Input  : master_citation_dataset.csv  (long format, one row per citation edge)
Outputs:
  data/corpus.json   references + citing articles
  data/pairs.json    precomputed co-citation index:
                     every reference pair co-cited by >= 2 articles,
                     with the list of contributing article indices.

The pairs index is what makes journal/year filtering fast: instead of
recomputing 2.4M pairwise combinations per request, the backend walks
~77k surviving pairs and counts which contributing articles pass the
current filter.

Run once after placing the CSV:
    python preprocess.py /path/to/master_citation_dataset.csv
"""
import csv
import json
import sys
import os
from collections import defaultdict

DEFAULT_INPUT = "master_citation_dataset.csv"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CORPUS_OUT = os.path.join(DATA_DIR, "corpus.json")
PAIRS_OUT = os.path.join(DATA_DIR, "pairs.json")

# pairs co-cited fewer than this many times can never survive a useful
# strength filter, so they are dropped from the index to save memory.
MIN_GLOBAL_STRENGTH = 2


def short_label(raw):
    parts = [p.strip() for p in raw.split(",")]
    return ", ".join(parts[:3])


def build_from_csv(rows):
    """Build (corpus, pairs) dicts from an iterable of row dicts.

    Accepts anything iterable that yields the same row shape as
    csv.DictReader -- a DictReader directly, a list of dicts, etc.

    Required columns: citing_UT, cited_canonical_id. Rows missing
    either are silently dropped. All other columns (citing_year,
    citing_journal, citing_title, cited_label, cited_year) are
    optional and treated as empty when absent, so a hand-edited CSV
    with omitted rows or blanked-out cells still parses cleanly.
    """
    ref_label, ref_year, ref_total = {}, {}, {}
    articles = {}

    for row in rows:
        ut = (row.get("citing_UT") or "").strip()
        cid = (row.get("cited_canonical_id") or "").strip()
        if not ut or not cid:
            continue

        art = articles.get(ut)
        if art is None:
            cy_raw = (row.get("citing_year") or "").strip()
            try:
                cy = int(float(cy_raw)) if cy_raw else None
            except ValueError:
                cy = None
            art = {
                "year": cy,
                "journal": (row.get("citing_journal") or "").strip(),
                "title": (row.get("citing_title") or "").strip(),
                "refs": set(),
            }
            articles[ut] = art
        art["refs"].add(cid)

        if cid not in ref_label:
            ref_label[cid] = short_label(row.get("cited_label") or cid)
        ref_total[cid] = ref_total.get(cid, 0) + 1
        if cid not in ref_year:
            ry_raw = (row.get("cited_year") or "").strip()
            try:
                ref_year[cid] = int(float(ry_raw)) if ry_raw else None
            except ValueError:
                ref_year[cid] = None

    if not articles:
        raise ValueError(
            "no usable rows -- the CSV must have columns "
            "'citing_UT' and 'cited_canonical_id' with at least one "
            "non-empty row"
        )

    ref_ids = sorted(ref_total.keys())
    ref_index = {cid: i for i, cid in enumerate(ref_ids)}
    refs_out = [{"i": ref_index[c], "label": ref_label[c],
                 "year": ref_year.get(c), "total": ref_total[c]}
                for c in ref_ids]

    articles_out = []
    for ut, art in articles.items():
        articles_out.append({
            "ut": ut, "year": art["year"], "journal": art["journal"],
            "title": art["title"],
            "refs": sorted(ref_index[c] for c in art["refs"]),
        })

    journals = sorted({a["journal"] for a in articles_out if a["journal"]})
    years = [a["year"] for a in articles_out if a["year"]]
    # If no usable years, fall back to a wide bracket so the slider
    # still has a finite range to render.
    year_min = min(years) if years else 1900
    year_max = max(years) if years else 2100

    corpus = {
        "refs": refs_out,
        "articles": articles_out,
        "journals": journals,
        "year_min": year_min,
        "year_max": year_max,
    }

    # ---- build the co-citation pair index ----
    pair_arts = defaultdict(list)
    for ai, a in enumerate(articles_out):
        r = a["refs"]
        n = len(r)
        for i in range(n):
            ri = r[i]
            for j in range(i + 1, n):
                pair_arts[(ri, r[j])].append(ai)

    kept = [(x, y, arts) for (x, y), arts in pair_arts.items()
            if len(arts) >= MIN_GLOBAL_STRENGTH]
    pairs = {
        "x": [p[0] for p in kept],
        "y": [p[1] for p in kept],
        "arts": [p[2] for p in kept],
    }
    return corpus, pairs


def main(input_path):
    with open(input_path, encoding="utf-8") as f:
        corpus, pairs = build_from_csv(csv.DictReader(f))

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CORPUS_OUT, "w") as f:
        json.dump(corpus, f, separators=(",", ":"))
    with open(PAIRS_OUT, "w") as f:
        json.dump(pairs, f, separators=(",", ":"))

    corpus_mb = os.path.getsize(CORPUS_OUT) / 1e6
    pairs_mb = os.path.getsize(PAIRS_OUT) / 1e6
    print(f"Wrote {CORPUS_OUT}  ({corpus_mb:.2f} MB)")
    print(f"  {len(corpus['refs']):,} references, "
          f"{len(corpus['articles']):,} articles, "
          f"{len(corpus['journals'])} journals")
    print(f"Wrote {PAIRS_OUT}  ({pairs_mb:.2f} MB)")
    print(f"  {len(pairs['x']):,} co-citation pairs (co-cited >= "
          f"{MIN_GLOBAL_STRENGTH} times)")
    print(f"  citing-year range: {corpus['year_min']}-{corpus['year_max']}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT
    if not os.path.exists(path):
        sys.exit(f"Input not found: {path}\n"
                 f"Usage: python preprocess.py "
                 f"/path/to/master_citation_dataset.csv")
    main(path)
