#!/bin/bash
# TaskPaw Hub V2 - Package as macOS standalone app
# Run this on the Mac Mini

echo "========================================"
echo "  TaskPaw Hub Build Tool"
echo "========================================"
echo ""

# Use Homebrew Python (has modern Tcl/Tk, required for tkinter)
BREW_PYTHON="/opt/homebrew/bin/python3"
if [ ! -x "$BREW_PYTHON" ]; then
    echo "ERROR: Homebrew Python not found at $BREW_PYTHON"
    echo "Install it with: brew install python python-tk"
    exit 1
fi

# Use a virtual environment built from Homebrew Python
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment (Homebrew Python)..."
    $BREW_PYTHON -m venv .venv
fi

echo "Activating virtual environment..."
source .venv/bin/activate

# Check for PyInstaller
python3 -c "import PyInstaller" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installing PyInstaller..."
    pip install pyinstaller
fi

echo "Starting build..."
echo ""

python3 -m PyInstaller \
    --onefile \
    --name "TaskPawHub" \
    taskpaw_hub.py

echo ""
if [ -f "dist/TaskPawHub" ]; then
    echo "========================================"
    echo "  Build successful!"
    echo "  Output: dist/TaskPawHub"
    echo "========================================"
    echo ""
    echo "  To run: ./dist/TaskPawHub"
    echo "  To install: cp dist/TaskPawHub /usr/local/bin/"
else
    echo "Build failed, please check the error messages."
fi
