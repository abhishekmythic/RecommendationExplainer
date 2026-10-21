#!/bin/sh
# Start the app with the project's own virtual environment, from any directory.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -m streamlit run app.py "$@"
