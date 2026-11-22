@echo off
setlocal
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import PIL, numpy" >nul 2>&1
    if errorlevel 1 goto :repair
    ".venv\Scripts\python.exe" app.py %*
    goto :done
)
py -3 -c "import PIL, numpy, rawpy" >nul 2>&1
if not errorlevel 1 (
    py -3 app.py %*
    goto :done
)
set "FIELDROOM_PY="
for /f "delims=" %%N in ('where node.exe 2^>nul') do if exist "%%~dpN..\..\python\python.exe" set "FIELDROOM_PY=%%~dpN..\..\python\python.exe"
if defined FIELDROOM_PY goto :bundled
for /f "usebackq delims=" %%P in (`powershell.exe -NoProfile -Command "$pkg = Get-AppxPackage -Name 'OpenAI.CodexPrimaryRuntime*' | Sort-Object Version -Descending | Select-Object -First 1; if ($pkg) { $p=Join-Path $pkg.InstallLocation 'dependencies\python\python.exe'; if (Test-Path -LiteralPath $p) { $p } }"`) do set "FIELDROOM_PY=%%P"
:bundled
if defined FIELDROOM_PY (
    "%FIELDROOM_PY%" app.py %*
    goto :done
)
py -3 -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
if errorlevel 1 (
    echo Install Python 3.10 or later with the Python launcher and reopen this file.
    pause
    popd
    exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 goto :done
:repair
".venv\Scripts\python.exe" -m pip install .
if errorlevel 1 goto :done
".venv\Scripts\python.exe" app.py %*
:done
set "FIELDROOM_EXIT=%ERRORLEVEL%"
if not "%FIELDROOM_EXIT%"=="0" pause
popd
exit /b %FIELDROOM_EXIT%
