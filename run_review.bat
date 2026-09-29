@echo off
REM ---------------------------------------------------------------------------
REM  Spotting Results Review -- double-click to run.
REM
REM  Stage 3 of the pipeline. run_timecourse.bat scores every timepoint x photo
REM  pairing x dilution and picks one winner per medium; this is where you look
REM  at all of them, keep the winner or choose a different candidate, and
REM  correct individual spots -- flag an outlier or type in a measured value
REM  you measured by hand.
REM
REM  Writes to Results\Timecourse\<set>\chosen\. The pipeline's own best\ is
REM  never touched, so re-running the timecourse cannot destroy a review.
REM
REM  Optionally pass the set to open:
REM      run_review.bat "Results\Timecourse\Set01"
REM
REM  To re-export every reviewed set without opening the window:
REM      run_review.bat --apply
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Find an interpreter. The project environment is tried FIRST and the probe is
REM only "import sys", because this runs on every launch: probing each candidate
REM with "import pandas" instead cost about two seconds of staring at an empty
REM console before anything appeared. The window itself says plainly when the
REM measurement stack is missing, which is the right place for it -- browsing
REM sheets and choosing a candidate genuinely do not need pandas.
REM
REM Windows also ships a stub python.exe on PATH that only opens the Microsoft
REM Store, so a candidate has to EXECUTE something before it is believed.
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

REM The strain names carry real delta characters; without this they arrive as
REM mojibake in the window title and in anything printed to the console.
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
chcp 65001 >nul

REM "--apply" runs the headless re-export instead of opening the window.
set "MODULE=results_review.app"
set "GUI=1"
set "ARGS="
:collect
if "%~1"=="" goto run
if /I "%~1"=="--apply" (
    set "MODULE=results_review.cli"
    set "GUI="
) else (
    set "ARGS=!ARGS! "%~1""
)
shift
goto collect

:run
REM Hand the window to pythonw so no console is left sitting behind it. A
REM console that stays open for the life of a GUI is what made a slow start look
REM like a failed one, and there is nothing to read in it: the window reports its
REM own problems, and anything fatal is written to review_error.log and shown in
REM a dialog by the app itself.
if defined GUI if exist "%PYWEXE%" (
    start "" "%PYWEXE%" -m %MODULE% %ARGS%
    endlocal
    exit /b 0
)

"%PYEXE%" -m %MODULE% %ARGS%
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
REM First candidate that actually executes wins. One cheap import, once. Its
REM windowed twin is recorded at the same time, since it sits beside it.
:probe
if defined PYEXE goto :eof
"%~1" -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE=%~1"
set "PYWEXE=%~dp1pythonw.exe"
if /I "%~nx1"=="py" set "PYWEXE=%~dp1pyw.exe"
goto :eof
