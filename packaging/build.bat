@echo off
rem One-click Windows build of rtlnc.exe (+ installer if Inno Setup 6 is installed).
rem Needs: Python 3.10-3.12 (64-bit) from python.org, and an internet connection.
setlocal
cd /d "%~dp0\.."

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
echo === Creating build environment
%PY% -m venv .buildenv || goto :err
call .buildenv\Scripts\activate.bat
python -m pip install --upgrade pip || goto :err
python -m pip install . pyinstaller || goto :err
python -m pip uninstall -y pyrtlsdrlib >nul 2>nul

echo === Fetching RTL-SDR Blog V4 driver DLLs
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\fetch_dlls.ps1 || goto :err

echo === Building rtlnc.exe
pyinstaller --noconfirm --clean packaging\rtlnc.spec || goto :err

set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  echo === Building installer
  %ISCC% packaging\rtlnc.iss || goto :err
  echo.
  echo Done: installer in dist\installer\, app folder in dist\rtlnc\
) else (
  echo.
  echo Done: app in dist\rtlnc\rtlnc.exe   ^(install Inno Setup 6 to also get an installer^)
)
pause
exit /b 0

:err
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
