@echo off
REM ---------------------------------------------------------------------------
REM  Plate Template Designer -- double-click to run.
REM
REM  Design the layout of a spotting assay: how big the grid is, which cells
REM  hold which sample, biological replicate and dilution level, and which
REM  sample is the control on each plate. Saves a reusable template that does
REM  not name any strains, so one template serves many experiments.
REM
REM  Optionally pass a template to open:
REM      run_plate_designer.bat "Plate Templates\my_layout.json"
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

REM Find an interpreter. The project environment is tried FIRST, matching the
REM other launchers. The designer itself needs nothing beyond the standard
REM library, so any working Python would do -- but preferring whatever "python"
REM happens to mean on PATH is how a launcher quietly picks the Microsoft Store
REM build, which runs and has no packages. Keeping the order the same
REM everywhere means that cannot come back if this app ever gains a dependency.
REM
REM Windows also ships a stub python.exe on PATH that only opens the Microsoft
REM Store, so a candidate has to EXECUTE something before it is believed.
set "PYEXE="
call :probe "%USERPROFILE%\miniconda3\envs\spotting\python.exe"
if not defined PYEXE call :probe "%LOCALAPPDATA%\miniconda3\envs\spotting\python.exe"
if not defined PYEXE call :probe "%USERPROFILE%\anaconda3\envs\spotting\python.exe"
if not defined PYEXE call :probe python
if not defined PYEXE call :probe py
if not defined PYEXE call :probe "%USERPROFILE%\miniconda3\python.exe"

if not defined PYEXE (
    echo.
    echo   ERROR: No working Python was found.
    echo.
    echo   A python.exe on your PATH may be the Microsoft Store placeholder,
    echo   which cannot run anything. To create the project environment:
    echo.
    echo       conda create -n spotting --override-channels -c conda-forge python=3.13
    echo.
    pause
    exit /b 1
)

REM Keep delta characters in template names readable.
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
chcp 65001 >nul

"%PYEXE%" -m plate_template.app %*
set "RC=%ERRORLEVEL%"

REM It is a GUI: only hold the console open when something went wrong.
if not "%RC%"=="0" (
    echo.
    echo   *** Finished with errors (exit code %RC%^) ***
    echo.
    pause
)
endlocal
exit /b %RC%

REM ---------------------------------------------------------------------------
:probe
if defined PYEXE goto :eof
"%~1" -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE=%~1"
goto :eof
