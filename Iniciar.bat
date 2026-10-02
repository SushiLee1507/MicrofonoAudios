@echo off
title Soundboard Mic
cd /d "%~dp0"

rem --- 1) Lanzador "py" (viene con el instalador oficial de Python) ---
where pyw >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import tkinter" >nul 2>nul
    if not errorlevel 1 (
        start "" pyw -3 "%~dp0soundboard.py"
        exit /b 0
    )
)

rem --- 2) "python" en el PATH ---
python -c "import tkinter" >nul 2>nul
if not errorlevel 1 (
    where pythonw >nul 2>nul
    if not errorlevel 1 (
        start "" pythonw "%~dp0soundboard.py"
        exit /b 0
    )
    python "%~dp0soundboard.py"
    exit /b 0
)

rem --- 3) No hay Python ---
echo.
echo  ============================================================
echo   FALTA PYTHON
echo  ============================================================
echo.
echo   Esta app necesita Python 3.9 o superior.
echo.
echo   1. Se abrira la pagina de descarga de Python.
echo   2. Instala Python y marca la casilla "Add python.exe to PATH".
echo      Deja marcada tambien la opcion "tcl/tk and IDLE".
echo   3. Vuelve a abrir Iniciar.bat.
echo.
echo   El resto de requisitos (paquetes y microfono virtual) los
echo   comprueba la propia app en su menu "Requisitos".
echo.
start "" https://www.python.org/downloads/windows/
pause
