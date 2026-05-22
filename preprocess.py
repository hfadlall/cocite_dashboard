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


def main(input_path):
    ref_label, ref_year, ref_total = {}, {}, {}
    articles = {}

    with open(input_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ut = row["citing_UT"]
            cid = row["cited_canonical_id"]

            art = articles.get(ut)
            if art is None:
                cy = row["citing_year"]
                try:
                    cy = int(float(cy)) if cy else None
                except ValueError:
                    cy = None
                art = {"year": cy, "journal": row["citing_journal"],
                       "title": row["citing_title"], "refs": set()}
                articles[ut] = art
            art["refs"].add(cid)

            if cid not in ref_label:
                ref_label[cid] = short_label(row["cited_label"])
            ref_total[cid] = ref_total.get(cid, 0) + 1
            if cid not in ref_year:
                ry = row["cited_year"]
                try:
                    ref_year[cid] = int(float(ry)) if ry else None
                except ValueError:
                    ref_year[cid] = None

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

    journals = sorted({a["journal"] for a in articles_out})
    years = [a["year"] for a in articles_out if a["year"]]

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CORPUS_OUT, "w") as f:
        json.dump({"refs": refs_out, "articles": articles_out,
                   "journals": journals,
                   "year_min": min(years), "year_max": max(years)},
                  f, separators=(",", ":"))

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
    pairs_payload = {
        "x": [p[0] for p in kept],
        "y": [p[1] for p in kept],
        "arts": [p[2] for p in kept],
    }
    with open(PAIRS_OUT, "w") as f:
        json.dump(pairs_payload, f, separators=(",", ":"))

    corpus_mb = os.path.getsize(CORPUS_OUT) / 1e6
    pairs_mb = os.path.getsize(PAIRS_OUT) / 1e6
    print(f"Wrote {CORPUS_OUT}  ({corpus_mb:.2f} MB)")
    print(f"  {len(refs_out):,} references, {len(articles_out):,} articles, "
          f"{len(journals)} journals")
    print(f"Wrote {PAIRS_OUT}  ({pairs_mb:.2f} MB)")
    print(f"  {len(kept):,} co-citation pairs (co-cited >= "
          f"{MIN_GLOBAL_STRENGTH} times)")
    print(f"  citing-year range: {min(years)}-{max(years)}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT
    if not os.path.exists(path):
        sys.exit(f"Input not found: {path}\n"
                 f"Usage: python preprocess.py "
                 f"/path/to/master_citation_dataset.csv")
    main(path)
