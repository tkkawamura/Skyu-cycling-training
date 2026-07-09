#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Edit it before running the app."
fi

echo
echo "Setup complete."
echo "Run:"
echo "  source .venv/bin/activate"
echo "  flask --app app run --host 0.0.0.0 --port 5050"
