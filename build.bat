@echo off
REM TaskPaw V2 - Package as Windows .exe
REM Prerequisites: pip install pyinstaller pystray Pillow psutil

echo ========================================
echo   TaskPaw V2 Build Tool
echo ========================================
echo.

REM Make sure all runtime + build dependencies are present.
REM psutil is required (replaces wmic for CPU/RAM stats).
python -c "import PyInstaller, pystray, PIL, psutil" 2>nul
if errorlevel 1 (
    echo Installing build/runtime dependencies...
    pip install pyinstaller pystray Pillow psutil
)

echo Starting build...
echo.

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "TaskPaw" ^
    --hidden-import pystray ^
    --hidden-import pystray._win32 ^
    --hidden-import PIL ^
    --hidden-import PIL._tkinter_finder ^
    --hidden-import psutil ^
    taskpaw.py

echo.
if exist "dist\TaskPaw.exe" (
    echo ========================================
    echo   Build successful!
    echo   Output: dist\TaskPaw.exe
    echo ========================================
) else (
    echo Build failed, please check the error messages.
)

pause
