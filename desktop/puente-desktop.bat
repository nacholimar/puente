@echo off
REM Lanza la app de escritorio del puente (con ventana/bandeja).
REM Para arranque silencioso usamos pythonw desde el acceso directo de Inicio.
cd /d "%~dp0"
start "" pythonw puente_app.py
