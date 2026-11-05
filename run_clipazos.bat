@echo off
setlocal
chcp 65001 >nul

rem La carpeta del .bat es la raíz del proyecto, aunque se lance desde otro sitio.
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo No se encontró el entorno virtual .venv en esta carpeta.
    exit /b 1
)

set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set PYTHONIOENCODING=utf-8
call ".venv\Scripts\activate.bat"
if not exist "output\clipazos" mkdir "output\clipazos"

rem Cada ejecución queda guardada en output\clipazos\ultimo_run.log.
echo ===== %date% %time% ===== > "output\clipazos\ultimo_run.log"
python clipazos.py %* >> "output\clipazos\ultimo_run.log" 2>&1
if errorlevel 1 (
    type "output\clipazos\ultimo_run.log"
    echo.
    echo Clipazos ES terminó con errores. Revisa output\clipazos\ultimo_run.log
    exit /b 1
)
type "output\clipazos\ultimo_run.log"

echo.
echo Clipazos ES listo. Los vídeos están en output\clipazos.
endlocal
