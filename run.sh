#!/usr/bin/env bash
# run.sh — one-command launcher for the co-citation dashboard.
#
# On first run, place master_citation_dataset.csv anywhere and run:
#   ./run.sh /path/to/master_citation_dataset.csv
# On later runs (data already preprocessed), just:
#   ./run.sh
set -e
cd "$(dirname "$0")"

if [ ! -f data/corpus.json ] || [ ! -f data/pairs.json ]; then
  if [ -z "$1" ]; then
    echo "First run: preprocessed data not found."
    echo "Usage: ./run.sh /path/to/master_citation_dataset.csv"
    exit 1
  fi
  echo "Preprocessing $1 ..."
  python3 preprocess.py "$1"
fi

echo "Starting dashboard at http://127.0.0.1:5000"
python3 app.py
