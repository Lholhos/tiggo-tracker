#!/bin/bash
# ─────────────────────────────────────────────
#  DealRadar — Setup & Run
# ─────────────────────────────────────────────

set -e

echo ""
echo "  ╔════════════════════════════════════╗"
echo "  ║            DealRadar               ║"
echo "  ╚════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
  echo "  ✗ Python 3 not found. Install from https://python.org"
  exit 1
fi

PYTHON="$(command -v python3)"
echo "  ✓ Python: $("$PYTHON" --version)"

# NOTE: venv and run dir are kept outside OneDrive.
# OneDrive sync causes Python .pyc file reads to hang indefinitely.
VENV_DIR="$HOME/tiggo-tracker-venv"
RUN_DIR="$HOME/tiggo-tracker-run"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "  Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"
PYTHON="python"

# Check pip
if ! "$PYTHON" -m pip --version &> /dev/null; then
  echo "  ✗ pip not found in venv"
  exit 1
fi

# Install dependencies
echo ""
echo "  Installing Python packages..."
"$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet

echo ""
echo "  Installing Playwright Chromium (first run only)..."
"$PYTHON" -m playwright install chromium

# Copy Python source files to local run dir (avoids OneDrive sync hanging)
mkdir -p "$RUN_DIR"
cp "$PROJECT_DIR/app.py" "$PROJECT_DIR/scraper.py" "$PROJECT_DIR/sync_service.py" "$RUN_DIR/"
if [ -f "$PROJECT_DIR/.env" ]; then
    sed "s|^DB_PATH=.*|DB_PATH=$PROJECT_DIR/tracker.db|" "$PROJECT_DIR/.env" > "$RUN_DIR/.env"
fi
if [ -f "$PROJECT_DIR/serviceAccountKey.json" ]; then
    cp "$PROJECT_DIR/serviceAccountKey.json" "$RUN_DIR/"
fi

# Write database.py with absolute DB path pointing back to project dir
sed "s|DB_PATH = Path(__file__).parent / \"tracker.db\"|DB_PATH = Path(\"$PROJECT_DIR/tracker.db\")|" \
    "$PROJECT_DIR/database.py" > "$RUN_DIR/database.py"

echo ""
echo "  ─────────────────────────────────────"
echo "  Starting server..."
echo "  Open → http://localhost:5001"
echo "  Press Ctrl+C to stop"
echo "  ─────────────────────────────────────"
echo ""

cd "$RUN_DIR" && "$PYTHON" app.py
