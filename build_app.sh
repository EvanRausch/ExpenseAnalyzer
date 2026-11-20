#!/usr/bin/env bash
# build_app.sh — Build BudgetTracker.app with PyInstaller
#
# Usage:
#   ./build_app.sh
#
# Output: dist/BudgetTracker.app
# After building, BudgetTracker.app and the project's data/ folder must share
# the same parent directory (they already do when run from here).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="/Users/evanrausch-larouche/miniconda3/envs/budgettracker/bin/python"
PYINSTALLER="/Users/evanrausch-larouche/miniconda3/envs/budgettracker/bin/pyinstaller"

# Install PyInstaller into the env if not present
if ! "$PYTHON" -c "import PyInstaller" 2>/dev/null; then
    echo "Installing PyInstaller…"
    "$PYTHON" -m pip install pyinstaller
fi

echo "Cleaning previous build…"
rm -rf build dist

echo "Building BudgetTracker.app…"
"$PYINSTALLER" BudgetTracker.spec

# Create ~/Library/Application Support/BudgetTracker/ and symlink data/ into it.
# This lets the .app find data no matter where it's installed.
APP_SUPPORT="$HOME/Library/Application Support/BudgetTracker"
mkdir -p "$APP_SUPPORT"

if [ -L "$APP_SUPPORT/data" ]; then
    echo "Symlink already exists: $APP_SUPPORT/data"
elif [ -e "$APP_SUPPORT/data" ]; then
    echo "WARNING: $APP_SUPPORT/data exists but is not a symlink — leaving it alone."
else
    ln -s "$SCRIPT_DIR/data" "$APP_SUPPORT/data"
    echo "✓ Linked: $APP_SUPPORT/data -> $SCRIPT_DIR/data"
fi

echo ""
echo "✓ Done! App is at: $(pwd)/dist/BudgetTracker.app"
echo ""
echo "To install: drag dist/BudgetTracker.app to /Applications (or anywhere)."
echo "Data is served from: $APP_SUPPORT/data"
