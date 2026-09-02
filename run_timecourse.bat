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
REM  Drag the top folder onto this file, or run it and type the path.
REM  Extra options pass straight through, e.g.
REM      run_timecourse.bat "D:\Set09" --workers 8
REM      run_timecourse.bat "D:\Set09" --estimate
REM ---------------------------------------------------------------------------

setlocal
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

set "ROOT=%~1"
if "%ROOT%"=="" (
    echo.
    set /p "ROOT=  Path to the time-course folder (e.g. D:\Set09): "
)
if "%ROOT%"=="" (
    echo   No folder given.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   Spotting time course -- scoring photo sets
echo  ============================================================

REM %~2 onwards are passed through; %* would repeat the folder.
set "EXTRA="
shift
:collect
if "%~1"=="" goto run
set "EXTRA=%EXTRA% %1"
shift
goto collect

:run
%PYEXE% "%~dp0src\spotting_timecourse.py" "%ROOT%" %EXTRA%
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
