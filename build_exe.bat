@echo off
REM ============================================================================
REM  Build iPhoneExporter.exe (Windows) with PyInstaller.
REM  Run this on the Windows machine where the app already works.
REM ============================================================================
setlocal

echo Installing build dependencies (without changing existing versions)...
python -m pip install pyinstaller
python -m pip install pymobiledevice3 pillow pillow-heif
if errorlevel 1 goto :err

REM This Anaconda env has a broken setuptools (missing 'backports.tarfile'),
REM which makes PyInstaller crash while probing setuptools. Installing the
REM missing submodule fixes it.
echo.
echo Repairing setuptools dependency (backports.tarfile)...
python -m pip install backports.tarfile

REM PyInstaller refuses to run if the obsolete 'pathlib' backport is installed.
REM It only shadows the built-in pathlib, so removing it is safe. Ignore the
REM "not installed" message if it isn't present.
echo.
echo Removing obsolete 'pathlib' backport if present...
python -m pip uninstall -y pathlib

echo.
echo Ensuring ffmpeg.exe is available to bundle...
set "FFMPEG=%~dp0ffmpeg.exe"
REM NOTE: FFZIP/FFDIR must be set OUTSIDE the if-block below. In a parenthesized
REM block cmd expands %VAR% at parse time, so vars set inside the block read empty.
set "FFZIP=%TEMP%\ffmpeg-release-essentials.zip"
set "FFDIR=%TEMP%\ffmpeg-extract"
if exist "%FFMPEG%" (
  echo Found existing ffmpeg.exe - reusing it.
) else (
  echo Downloading ffmpeg static build...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop';" ^
    "Invoke-WebRequest -UseBasicParsing -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile '%FFZIP%';" ^
    "if (Test-Path '%FFDIR%') { Remove-Item -Recurse -Force '%FFDIR%' };" ^
    "Expand-Archive -Path '%FFZIP%' -DestinationPath '%FFDIR%' -Force;" ^
    "$exe = Get-ChildItem -Path '%FFDIR%' -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1;" ^
    "Copy-Item $exe.FullName '%FFMPEG%' -Force;"
  if errorlevel 1 goto :err
  if not exist "%FFMPEG%" goto :err
  echo Saved ffmpeg.exe next to the build script.
)

echo.
echo Locating interpreter DLLs (tkinter / sqlite3 / ssl) so PyInstaller bundles them...
REM Anaconda keeps tcl86t.dll, tk86t.dll, sqlite3.dll, libssl, ... in Library\bin.
REM If that dir is not on PATH, PyInstaller can't resolve them and the exe crashes
REM on GUI start / database read. Put the interpreter's DLL dirs on PATH.
for /f "delims=" %%B in ('python -c "import sys;print(sys.base_prefix)"') do set "BASEPREFIX=%%B"
echo Interpreter prefix: %BASEPREFIX%
set "PATH=%BASEPREFIX%\Library\bin;%BASEPREFIX%\DLLs;%PATH%"

echo.
echo Building one-file windowed exe...
pyinstaller --noconfirm --onefile --windowed --name iPhoneExporter ^
  --add-binary "%FFMPEG%;." ^
  --collect-all pymobiledevice3 ^
  --collect-submodules pymobiledevice3 ^
  --collect-all pillow_heif ^
  --collect-all construct ^
  iphone_export_gui.py
if errorlevel 1 goto :err

echo.
echo ============================================================
echo  Done.  -^>  dist\iPhoneExporter.exe
echo  (ffmpeg.exe is bundled INSIDE the exe; MOV-^>MP4 conversion
echo   works out of the box - no separate ffmpeg needed.)
echo ============================================================
goto :eof

:err
echo.
echo BUILD FAILED — see the messages above.
exit /b 1
