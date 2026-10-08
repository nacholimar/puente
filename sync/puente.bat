@echo off
REM Arranca el watcher del puente. Dejá esta ventana abierta mientras trabajás,
REM o agregá un acceso directo a esta .bat en la carpeta Inicio para que arranque solo.
cd /d "%~dp0"
python drop-sync.py %*
pause
