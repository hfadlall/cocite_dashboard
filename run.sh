#!/usr/bin/env bash
# run.sh -- one-command launcher for the dashboard.
#
# The dashboard ships with no bundled corpus.  Start the server and
# upload a long-format citation CSV via the Corpus panel in the
# sidebar.
set -e
cd "$(dirname "$0")"
echo "Starting dashboard at http://127.0.0.1:5000"
echo "Upload a CSV via the sidebar to load a corpus."
python3 app.py
