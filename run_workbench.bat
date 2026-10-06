@echo off
REM ---------------------------------------------------------------------------
REM  Spotting Quantification -- double-click to run.
REM
REM  The whole program in one window: plate templates, experiments (where the
REM  photos are measured) and the review of their results, as tabs, with Home
REM  to start from. It runs spotting_app.py with no stage, i.e.
REM      spotting_app.py workbench
REM
REM  Optionally pass files or result folders to open, e.g.
REM      run_workbench.bat "Experiment Designs\Set09.spotexp.json"
REM
REM  The single-tool launchers (run_plate_designer.bat, run_experiment_designer
REM  .bat, run_review.bat) still work, each tool on its own.
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Find an interpreter -- the project environment first, for the same reasons
REM as run_review.bat: a plain "python" on PATH is often the Microsoft Store
REM build, which runs but has none of the packages, and the probe stays a cheap
REM "import sys" because it runs on every launch. Windows also ships a stub
REM python.exe that only opens the Store, so a candidate has to EXECUTE
REM something before it is believed.
set "PYEXE="
call :probe "%USERPROFILE%\miniconda3\envs\spotting\python.exe"
call :probe "%LOCALAPPDATA%\miniconda3\envs\spotting\python.exe"
call :probe "%USERPROFILE%\anaconda3\envs\spotting\python.exe"
call :probe python
call :probe py
call :probe "%USERPROFILE%\miniconda3\python.exe"

if not defined PYEXE (
    echo.
    echo   ERROR: No working Python was found.
    echo.
    echo   A python.exe on your PATH may be the Microsoft Store placeholder,
    echo   which cannot run anything. To create the project environment:
    echo.
    echo       conda create -n spotting --override-channels -c conda-forge python=3.13
    echo       conda activate spotting
    echo       pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
chcp 65001 >nul

set "ARGS="
:collect
if "%~1"=="" goto run
set "ARGS=!ARGS! "%~1""
shift
goto collect

:run
REM Hand the window to pythonw so no console is left sitting behind it. The
REM program reports its own problems: a crash at startup is written to
REM spotting_error.log and shown in a dialog.
if exist "%PYWEXE%" (
    start "" "%PYWEXE%" "%~dp0spotting_app.py" workbench %ARGS%
    endlocal
    exit /b 0
)

"%PYEXE%" "%~dp0spotting_app.py" workbench %ARGS%
set "RC=%ERRORLEVEL%"

REM Only hold the console open when something went wrong.
if not "%RC%"=="0" (
    echo.
    echo   *** Finished with errors (exit code %RC%^) ***
    echo.
    pause
)
endlocal
exit /b %RC%

REM ---------------------------------------------------------------------------
REM First candidate that actually executes wins. Its windowed twin is recorded
REM at the same time, since it sits beside it.
:probe
if defined PYEXE goto :eof
"%~1" -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE=%~1"
set "PYWEXE=%~dp1pythonw.exe"
if /I "%~nx1"=="py" set "PYWEXE=%~dp1pyw.exe"
goto :eof
