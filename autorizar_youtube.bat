@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist "credenciales\client_secret.json" (
    echo Falta credenciales\client_secret.json
    echo Descárgalo de Google Cloud: cliente OAuth de tipo "App de escritorio".
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python youtube_upload.py --autorizar
pause
endlocal
