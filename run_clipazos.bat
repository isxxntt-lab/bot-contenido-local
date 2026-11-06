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

rem La última ejecución queda en ultimo_run.log y todas se acumulan en historial.log.
set "LOG=output\clipazos\ultimo_run.log"
echo ===== %date% %time% ===== > "%LOG%"
python clipazos.py %* >> "%LOG%" 2>&1
set "RESULTADO=%errorlevel%"
type "%LOG%" >> "output\clipazos\historial.log"
type "%LOG%"
if not "%RESULTADO%"=="0" (
    echo.
    echo Clipazos ES terminó con errores. Revisa output\clipazos\historial.log
    exit /b 1
)

echo.
echo Clipazos ES listo. Los vídeos están en output\clipazos.
endlocal
