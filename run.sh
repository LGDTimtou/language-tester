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

# Keep the progress DB OUT of this (Dropbox-synced) folder. A cloud sync can
# silently roll a live SQLite file back and wipe your quiz/exercise progress.
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/lswk-trainer"
mkdir -p "$DATA_HOME"
export LSWK_DB="${LSWK_DB:-$DATA_HOME/vocab.db}"

if [ ! -f "$LSWK_DB" ] && [ -f vocab.db ]; then
    echo "Moving your progress DB out of the synced folder -> $LSWK_DB"
    cp vocab.db "$LSWK_DB"
    echo "(the old ./vocab.db is left in place but no longer used; you can delete it)"
fi

if [ ! -f "$LSWK_DB" ]; then
    echo "No database yet - building vocabulary from the PatreonDownloads PDFs..."
    python3 parse_vocab.py
fi

echo "Checking lesson exercises..."
python3 parse_exercises.py

echo "Starting server at http://127.0.0.1:5055   (DB: $LSWK_DB)"
python3 app.py
