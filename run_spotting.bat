@echo off
REM ---------------------------------------------------------------------------
REM  Spotting assay quantification -- double-click to run.
REM
REM  Put your photos in the "Spotting Assays" folder next to this file, named
REM      <set>.<plate><TREATMENT>.JPG      e.g.  1.2GLU.JPG
REM  then run this. It will ask for the strains and the dilutions, then write
REM  the data and figures into "Spotting Assays\Results".
REM
REM  Answers are remembered in spotting_config.json, so later runs only ask
REM  about things they have not seen. To start the questions over, run this
REM  file from a terminal with:   run_spotting.bat --reask
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

REM Prefer a real python on PATH; fall back to the py launcher.
set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
    where py >nul 2>nul && set "PYEXE=py"
)
if not defined PYEXE (
    echo.
    echo   ERROR: Python was not found on your PATH.
    echo   Install Python, or open an Anaconda Prompt and run:
    echo       python spotting_batch.py "Spotting Assays"
    echo.
    pause
    exit /b 1
)

REM Keep the delta characters in strain names readable in the console.
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul

echo.
echo  ============================================================
echo   Spotting assay quantification
echo  ============================================================

%PYEXE% "%~dp0spotting_batch.py" "%~dp0Spotting Assays" %*
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
