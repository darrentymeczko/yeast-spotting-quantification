@echo off
REM ---------------------------------------------------------------------------
REM  Spotting time course -- choose which photos to quantify.
REM
REM  A DIFFERENT pipeline from run_spotting.bat. That one takes a flat folder of
REM  photos you have already chosen by eye. This one takes the raw capture tree
REM
REM      Set09\16 Hours\Glucose\Plate 1 (Rep 1+2)\*.jpg
REM      Set09\16 Hours\Glucose\Plate 2 (Rep 3+4)\*.jpg
REM      Set09\40 Hours\Glycerol\Plate 1 (Rep 1+2)\*.jpg
REM
REM  and scores every timepoint x photo pairing x dilution, so you can see which
REM  are worth opening first.
REM
REM  SEVERAL SETS AT ONCE:
REM    * Just double-click this file -- a folder picker opens, and reopens until
REM      you press Cancel, so you can add as many as you like.
REM    * Pick the folder that CONTAINS your sets (e.g. "Deletion Strains") and
REM      every capture tree inside it runs -- Set01..Set10 in one go.
REM    * Or drag one or more folders onto this file.
REM
REM  Every strain-panel question is asked up front, before any measuring, so a
REM  long batch can be left alone once it starts.
REM
REM  Extra options pass straight through, e.g.
REM      run_timecourse.bat "D:\Set09" --workers 8
REM      run_timecourse.bat "D:\Set09" --estimate
REM      run_timecourse.bat --pick --figures none
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
    where py >nul 2>nul && set "PYEXE=py"
)
if not defined PYEXE (
    echo.
    echo   ERROR: Python was not found on your PATH.
    echo.
    pause
    exit /b 1
)

set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

echo.
echo  ============================================================
echo   Spotting time course -- scoring photo sets
echo  ============================================================

REM Pass every argument through untouched. Folders and switches are told apart
REM by the Python side, so dragging several folders on works, and so does
REM dragging none (which opens the picker).
set "ARGS="
:collect
if "%~1"=="" goto run
set "ARGS=!ARGS! "%~1""
shift
goto collect

:run
%PYEXE% "%~dp0src\spotting_timecourse.py" %ARGS%
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo   *** Finished with errors (exit code %RC%^) ***
) else (
    echo   Done.
)
echo.
pause
endlocal
exit /b %RC%
