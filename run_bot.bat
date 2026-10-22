@echo off
setlocal
chcp 65001 >nul

rem La carpeta del .bat es la raíz del proyecto, aunque se lance desde otro sitio.
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo No se encontró el entorno virtual .venv en esta carpeta.
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python main.py
if errorlevel 1 (
    echo.
    echo La extracción terminó con errores.
    exit /b 1
)

echo.
echo La extracción finalizó correctamente. Los posts están en la carpeta output.
endlocal
