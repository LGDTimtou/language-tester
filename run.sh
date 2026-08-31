#!/usr/bin/env bash
# Starts the Swedish Vocab Trainer locally at http://127.0.0.1:5055
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

if [ ! -f vocab.db ]; then
    echo "No vocab.db found - building it from the PatreonDownloads PDFs..."
    python3 parse_vocab.py
fi

echo "Checking lesson exercises are parsed..."
python3 parse_exercises.py

echo "Starting server at http://127.0.0.1:5055"
python3 app.py
