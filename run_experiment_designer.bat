@echo off
REM ---------------------------------------------------------------------------
REM  Experiment Designer -- double-click to run.
REM
REM  A plate template says what the assay LOOKS like. An experiment says who was
REM  on it: which strain is in each sample slot, what it was grown on, which
REM  strain is the positive control, and where the photographs are.
REM
REM  The photo folder can be laid out however you file them. The layout is read
REM  automatically and every photo is listed with what it was understood to be,
REM  so anything read wrongly can be corrected by hand.
REM
REM  Optionally pass an experiment to open:
REM      run_experiment_designer.bat "Experiment Designs\Set01.spotexp.json"
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

REM Find an interpreter. The project environment is tried FIRST, for the same
REM reason run_review.bat does it: a plain "python" on PATH is very often the
REM Microsoft Store build, which RUNS perfectly well but has no packages in it.
REM Preferring it gives a window that opens and then cannot show a photograph,
REM because Pillow is installed in the project environment and not in that one.
REM
REM The probe itself stays "import sys" rather than "import PIL", because it
REM runs on every launch and probing each candidate for a real package costs
REM seconds of staring at an empty console. The window says plainly when Pillow
REM is missing, and names the interpreter it is running on, which is the right
REM place for it.
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

REM Keep delta characters in strain names readable.
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
chcp 65001 >nul

"%PYEXE%" -m experiments.app %*
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
