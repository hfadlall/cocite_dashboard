#!/usr/bin/env python3
"""
app.py -- Flask backend for the bibliometric co-citation dashboard.

Holds the active corpus + pair index in memory and exposes the
analytical functions from cocitation.py over HTTP.  The dashboard
starts with NO data loaded; the user must upload a long-format
citation CSV via /api/load to populate it.  All the actual analysis
(filtering, edge weighting, community detection, bridging metrics)
lives in cocitation.py so the same code can be used standalone by a
collaborator.

Run:  python app.py
Then open  http://127.0.0.1:5000
"""
import csv
import gzip
import io
import os

from flask import Flask, jsonify, request, send_from_directory

from preprocess import build_from_csv
from cocitation import EDGE_WEIGHT_MODES, build_cocitation_graph

# Hard ceiling on the COMPRESSED upload size. Vercel Hobby caps request
# bodies at ~4.5 MB; the frontend gzips the CSV before sending so this
# is what arrives over the wire.  Bibliographic CSVs typically compress
# 6-10x, so this accommodates raw inputs in the ~30 MB range.
MAX_UPLOAD_BYTES = 4_500_000
# Separate guardrail on the DECOMPRESSED size to bound memory + parse
# time and to neutralise any malformed/gzip-bomb payloads.
MAX_DECOMPRESSED_BYTES = 50_000_000

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 100_000  # small headroom

# ---------------------------------------------------------------------------
# Active dataset (module-level globals).  The dashboard is corpus-
# agnostic: there is no bundled default.  Both globals stay None until
# a CSV is uploaded via /api/load, at which point this module holds
# the parsed corpus in memory until either /api/reset clears it or the
# Vercel function instance goes cold.
#
# That ephemerality is intentional.  When the function goes cold and a
# new instance starts, the user re-uploads -- there is no persistent
# server-side state and no shared corpus across users.
# ---------------------------------------------------------------------------
CORPUS = None
PAIRS = None
ACTIVE_SOURCE = "none"   # "none" or "uploaded" -- exposed via /api/meta


def _set_active(corpus, pairs, source):
    """Install corpus + pairs as the active dataset for graph queries.

    Passing (None, None, "none") returns the dashboard to its empty
    initial state.  Rotating all three globals in one shot keeps any
    in-flight request consistent.
    """
    global CORPUS, PAIRS, ACTIVE_SOURCE
    CORPUS = corpus
    PAIRS = pairs
    ACTIVE_SOURCE = source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _meta_payload():
    """JSON-serialisable summary of the active corpus.

    Returns a uniform shape regardless of whether a corpus is loaded;
    the ``loaded`` flag and the ``source`` field distinguish the two
    states.  Empty-corpus values (None years, empty lists, zeros) let
    the frontend reset its controls without special-casing the keys.
    """
    if CORPUS is None:
        return {
            "loaded": False,
            "source": ACTIVE_SOURCE,  # "none"
            "year_min": None,
            "year_max": None,
            "journals": [],
            "n_articles": 0,
            "n_refs": 0,
            "n_pairs": 0,
        }
    return {
        "loaded": True,
        "source": ACTIVE_SOURCE,
        "year_min": CORPUS["year_min"],
        "year_max": CORPUS["year_max"],
        "journals": CORPUS["journals"],
        "n_articles": len(CORPUS["articles"]),
        "n_refs": len(CORPUS["refs"]),
        "n_pairs": len(PAIRS["x"]),
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
    """Clear the loaded corpus, returning the dashboard to its empty
    initial state.  Renamed from "revert" since there is no longer a
    bundled corpus to revert to: clearing means there is no data at
    all until the next upload."""
    _set_active(None, None, "none")
    return jsonify(_meta_payload())


@app.route("/api/graph")
def graph():
    # Refuse cleanly when no corpus is loaded so the frontend gets a
    # clear, actionable message instead of a 500.
    if CORPUS is None:
        return jsonify({
            "error": "no corpus loaded -- upload a CSV first",
            "no_corpus": True,
        }), 400

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
        edge_weight_mode = request.args.get("edge_weight_mode", "raw")
        if edge_weight_mode not in EDGE_WEIGHT_MODES:
            edge_weight_mode = "raw"
        # min_normalized_weight is mode-scaled (0..1 for cosine/jaccard,
        # unbounded for association strength), so we accept a float and
        # only floor it at 0.  Negative inputs are ignored.
        min_normalized_weight = max(
            0.0, float(request.args.get("min_normalized_weight", 0)))
    except ValueError:
        return jsonify({"error": "invalid parameters"}), 400

    return jsonify(build_cocitation_graph(
        CORPUS, PAIRS,
        year_from=year_from, year_to=year_to,
        journal=journal,
        min_strength=min_strength,
        min_citations=min_citations,
        max_nodes=max_nodes,
        edge_weight_mode=edge_weight_mode,
        min_normalized_weight=min_normalized_weight,
    ))


if __name__ == "__main__":
    print("Open  http://127.0.0.1:5000  in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False)
