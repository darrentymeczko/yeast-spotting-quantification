@echo off
REM ---------------------------------------------------------------------------
REM  Data Review -- double-click to run.
REM
REM  Before an experiment's statistics are run, flip through every photograph
REM  it imported and flag what is not good data: a whole plate, or single spots
REM  by clicking on them. Spot positions come from the program's own detection
REM  ("Locate spots..." in the window), which fills the same measurement cache
REM  a run reads, so nothing is measured twice.
REM
REM  The additional multi-step analysis excludes what is flagged; the results
REM  review marks it, for you to decide.
REM
REM  Optionally pass the experiment to review:
REM      run_data_review.bat "Experiment Designs\Set01.spotexp.json"
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

REM Find an interpreter, the project environment first -- see
REM run_experiment_designer.bat for why, and why each candidate has to run.
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
    echo       conda activate spotting
    echo       pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM Keep delta characters in strain names readable.
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
chcp 65001 >nul

"%PYEXE%" -m data_review.app %*
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
