#!/usr/bin/env python3
"""
app.py -- Flask backend for the co-citation network dashboard.

Holds the active corpus + pair index in memory, serves the static
dashboard, and exposes the analytical functions in cocitation.py
over HTTP.  All the actual analysis (filtering, edge weighting,
community detection, bridging metrics) lives in cocitation.py so
the same code can be used standalone by a collaborator.

Run:  python app.py
Then open  http://127.0.0.1:5000
"""
import csv
import gzip
import io
import json
import os

from flask import Flask, jsonify, request, send_from_directory

from preprocess import build_from_csv
from cocitation import build_cocitation_graph

BASE = os.path.dirname(os.path.abspath(__file__))
CORPUS_PATH = os.path.join(BASE, "data", "corpus.json")
PAIRS_PATH = os.path.join(BASE, "data", "pairs.json")

# Hard ceiling on the COMPRESSED upload size. Vercel Hobby caps request
# bodies at ~4.5 MB; the frontend gzips the CSV before sending so this
# is what arrives over the wire. Raw CSVs typically compress 6-10x for
# bibliographic data, so this comfortably accommodates the ~14 MB
# master_citation_dataset.csv.
MAX_UPLOAD_BYTES = 4_500_000
# Separate guardrail on the DECOMPRESSED size to bound memory + parse
# time and to neutralise any malformed/gzip-bomb payloads.
MAX_DECOMPRESSED_BYTES = 50_000_000

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
ACTIVE_SOURCE = "bundled"   # "bundled" or "uploaded" -- exposed via /api/meta


def _set_active(corpus, pairs, source):
    """Install corpus + pairs as the active dataset for graph queries.

    The Flask handlers below read from these globals and pass them
    into build_cocitation_graph(); rotating both pointers in one shot
    keeps any in-flight request consistent.
    """
    global CORPUS, PAIRS, ACTIVE_SOURCE
    CORPUS = corpus
    PAIRS = pairs
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

print(f"Loaded {len(CORPUS['refs']):,} refs, "
      f"{len(CORPUS['articles']):,} articles, "
      f"{len(CORPUS['journals'])} journals, "
      f"{len(PAIRS['x']):,} co-citation pairs")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _meta_payload():
    """JSON-serialisable summary of the active corpus.

    Used by /api/meta, /api/load (post-upload echo), and /api/reset.
    Kept as a single helper so all three stay in lockstep.
    """
    return {
        "year_min": CORPUS["year_min"],
        "year_max": CORPUS["year_max"],
        "journals": CORPUS["journals"],
        "n_articles": len(CORPUS["articles"]),
        "n_refs": len(CORPUS["refs"]),
        "n_pairs": len(PAIRS["x"]),
        "source": ACTIVE_SOURCE,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/meta")
def meta():
    return jsonify(_meta_payload())


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
            "error": f"compressed payload is {len(raw)/1e6:.1f} MB; "
                     f"the upload limit is {MAX_UPLOAD_BYTES/1e6:.1f} MB"
        }), 413

    # gzip-detect by magic bytes (1f 8b). The frontend always gzips
    # before sending so a real CSV that survives Vercel's body limit
    # almost certainly arrives compressed -- but accept raw text too
    # for curl users and as a fallback if CompressionStream is missing.
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError as e:
            return jsonify({"error": f"could not decompress upload: {e}"}), 400
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            return jsonify({
                "error": f"file expands to {len(raw)/1e6:.1f} MB; the "
                         f"decompressed limit is "
                         f"{MAX_DECOMPRESSED_BYTES/1e6:.0f} MB"
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
    return jsonify(_meta_payload())


@app.route("/api/reset", methods=["POST"])
def reset_corpus():
    """Revert the active dataset to the bundled corpus."""
    _set_active(_bundled_corpus, _bundled_pairs, "bundled")
    return jsonify(_meta_payload())


@app.route("/api/graph")
def graph():
    try:
        year_from = int(request.args.get("year_from", CORPUS["year_min"]))
        year_to = int(request.args.get("year_to", CORPUS["year_max"]))
        # Bounds are enforced here, at the HTTP boundary, not in
        # cocitation.py -- a colleague calling that module from a
        # notebook may legitimately want to push the parameters past
        # these dashboard-safe ranges.
        min_strength = max(2, int(request.args.get("min_strength", 5)))
        max_nodes = max(10, min(600, int(request.args.get("max_nodes", 150))))
        journal = request.args.get("journal", "").strip()
        min_citations = max(0, int(request.args.get("min_citations", 0)))
    except ValueError:
        return jsonify({"error": "invalid parameters"}), 400

    return jsonify(build_cocitation_graph(
        CORPUS, PAIRS,
        year_from=year_from, year_to=year_to,
        journal=journal,
        min_strength=min_strength,
        min_citations=min_citations,
        max_nodes=max_nodes,
    ))


if __name__ == "__main__":
    print("Open  http://127.0.0.1:5000  in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False)
