@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo [1/2] Installing packages...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :err
echo [2/2] Building exe...
python -m PyInstaller --noconfirm --onefile --windowed --name SubtitleTranslator --collect-submodules charset_normalizer subtitle_translator.py
if errorlevel 1 goto :err
echo.
echo Done: dist\SubtitleTranslator.exe
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
