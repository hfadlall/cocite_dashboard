"""Vercel serverless entry point.

Vercel's @vercel/python runtime auto-detects a WSGI-compatible `app`
attribute and runs it as the function handler. We just re-export the
Flask app from app.py at the project root, so the exact same code path
serves both `python app.py` locally and Vercel in production.

This file is Vercel-only. Local development still uses app.py directly.
"""
import os
import sys

# api/index.py lives one directory deeper than app.py, so the project root
# isn't on sys.path by default in the serverless sandbox. Insert it so
# `from app import app` resolves to the Flask app at the repo root.
sys.path.insert(0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  -- exported for the Vercel Python runtime
