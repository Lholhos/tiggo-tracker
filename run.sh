#!/bin/bash
# ─────────────────────────────────────────────
#  Tiggo 8 Pro Price Tracker — Setup & Run
# ─────────────────────────────────────────────

set -e

echo ""
echo "  ╔════════════════════════════════════╗"
echo "  ║   Tiggo 8 Pro · Price Tracker      ║"
echo "  ╚════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
  echo "  ✗ Python 3 not found. Install from https://python.org"
  exit 1
fi

PYTHON="$(command -v python3)"
echo "  ✓ Python: $("$PYTHON" --version)"

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "  Creating virtual environment..."
    "$PYTHON" -m venv venv
fi

# Activate virtual environment
source venv/bin/activate
PYTHON="python"

# Check pip
if ! "$PYTHON" -m pip --version &> /dev/null; then
  echo "  ✗ pip not found in venv"
  exit 1
fi

# Install dependencies
echo ""
echo "  Installing Python packages..."
"$PYTHON" -m pip install -r requirements.txt --quiet

echo ""
echo "  Installing Playwright Chromium (first run only)..."
"$PYTHON" -m playwright install chromium

echo ""
echo "  ─────────────────────────────────────"
echo "  Starting server..."
echo "  Open → http://localhost:5001"
echo "  Press Ctrl+C to stop"
echo "  ─────────────────────────────────────"
echo ""

"$PYTHON" app.py
