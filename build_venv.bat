@echo off
REM ============================================================================
REM  Build iPhoneExporter.exe in a CLEAN, isolated virtual environment.
REM
REM  Why: building from Anaconda drags the whole scientific stack (PyQt5, scipy,
REM  pandas, jupyter, ...) into the exe -> 1 GB+ and very slow. A fresh venv
REM  contains ONLY what the app needs, so the exe is small and the build is fast.
REM  It also avoids Anaconda's broken setuptools / pathlib backport issues.
REM ============================================================================
setlocal
set "VENV=%~dp0build-venv"
set "PY=%VENV%\Scripts\python.exe"

echo === Selecting a non-Anaconda Python to build from ===
REM PyInstaller + Anaconda has broken DLL bundling: conda's _ctypes/_ssl/_tkinter/
REM _sqlite3 link against DLLs in a nonstandard Library\bin layout, and the packaged
REM exe crashes at runtime (e.g. "_ctypes: DLL load failed / ffi.dll not found").
REM The python.org 'py' launcher never resolves to conda, so build from that.
where py >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: the python.org 'py' launcher was not found.
  echo Install Python 3.12 from https://www.python.org/downloads/windows/
  echo ^(keep the "py launcher" option checked^), then re-run this script.
  goto :err
)
set "BUILDPY=py -3"
echo Build interpreter:
%BUILDPY% -c "import sys;print(sys.executable)"
%BUILDPY% -c "import sys,re;sys.exit(1 if re.search('conda',sys.executable,re.I) else 0)"
if errorlevel 1 (
  echo.
  echo ERROR: 'py -3' resolved to an Anaconda Python, which we are avoiding.
  echo Install Python 3.12 from python.org and re-run.
  goto :err
)

echo.
echo === Cleaning stale venv / build cache (interpreter may have changed) ===
if exist "%VENV%" rmdir /s /q "%VENV%"
if exist "%~dp0build" rmdir /s /q "%~dp0build"
if exist "%~dp0iPhoneExporter.spec" del /q "%~dp0iPhoneExporter.spec"

echo.
echo === Creating clean build venv at %VENV% ===
%BUILDPY% -m venv "%VENV%"
if errorlevel 1 goto :err

echo.
echo === Installing ONLY the needed packages ===
"%PY%" -m pip install --upgrade pip setuptools wheel
"%PY%" -m pip install pymobiledevice3 pillow pillow-heif pyinstaller pywin32
if errorlevel 1 goto :err

echo.
echo === Ensuring ffmpeg.exe is available to bundle ===
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
echo === Building one-file windowed exe ===
REM Built from a clean python.org venv: _ctypes/_ssl/_tkinter/_sqlite3 and their
REM companion DLLs live in the standard layout, so PyInstaller resolves them
REM automatically (no Anaconda Library\bin PATH hack needed).
REM pywin32 (win32com/pythoncom) is bundled for the GUI's MTP fallback path.
"%PY%" -m PyInstaller --noconfirm --onefile --windowed --name iPhoneExporter ^
  --add-binary "%FFMPEG%;." ^
  --collect-all pymobiledevice3 ^
  --collect-all pillow_heif ^
  --collect-all construct ^
  --hidden-import iphone_export_mtp ^
  --hidden-import win32com.client ^
  --hidden-import win32timezone ^
  --hidden-import PIL.ImageTk ^
  --hidden-import PIL._imagingtk ^
  --collect-submodules win32com ^
  --exclude-module PyQt5 --exclude-module PyQt6 ^
  --exclude-module PySide2 --exclude-module PySide6 ^
  --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
  --exclude-module numpy --exclude-module notebook --exclude-module jupyterlab ^
  --exclude-module dask --exclude-module distributed --exclude-module numba ^
  --exclude-module llvmlite --exclude-module bokeh --exclude-module sphinx ^
  --exclude-module h5py --exclude-module sqlalchemy --exclude-module tables ^
  --exclude-module pyarrow --exclude-module plotly --exclude-module panel ^
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
echo BUILD FAILED - see the messages above.
exit /b 1
