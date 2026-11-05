@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "ENTRY=%ROOT%main.py"
set "REQ=%ROOT%requirements.txt"
set "BUILD_DIR=%ROOT%build"
set "DIST_DIR=%ROOT%dist"
set "OUTPUT=%DIST_DIR%\PixFilter_v1.0.exe"
set "SPEC_FILE=%ROOT%PixFilter_v1.0.spec"
set "OLD_SPEC=%ROOT%PixFilter.spec"

echo PixFilter 1.0 build script
echo Working directory: "%CD%"
echo.

rem Select Python
set "PY=python"
where python >nul 2>nul
if errorlevel 1 goto try_py_launcher
"%PY%" --version >nul 2>nul
if errorlevel 1 goto no_python
goto python_ready

:try_py_launcher
where py >nul 2>nul
if errorlevel 1 goto no_python
set "PY=py"
"%PY%" --version >nul 2>nul
if errorlevel 1 goto no_python

:python_ready
echo [OK] Python:
"%PY%" --version

if not exist "%ENTRY%" goto missing_entry
if not exist "%REQ%" goto missing_requirements

rem Check project dependencies
set "DEP_CHECK=import tkinter, PIL, PIL.ImageTk, imagehash, numpy, scipy, scipy.ndimage, scipy.fft, scipy.fftpack, scipy.signal, pywt"
"%PY%" -c "%DEP_CHECK%" >nul 2>nul
if not errorlevel 1 goto deps_ready
echo [WARN] Dependencies are missing. Installing requirements.txt...
"%PY%" -m pip install -r "%REQ%"
if errorlevel 1 goto dep_fail
"%PY%" -c "%DEP_CHECK%" >nul 2>nul
if errorlevel 1 goto dep_fail

:deps_ready
echo [OK] Dependencies import successfully.

rem Check PyInstaller
"%PY%" -m PyInstaller --version >nul 2>nul
if not errorlevel 1 goto pyi_ready

echo [INFO] PyInstaller not found. Installing...
"%PY%" -m pip install pyinstaller
if not errorlevel 1 goto pyi_check
echo [WARN] Global install failed. Trying user install...
"%PY%" -m pip install --user pyinstaller

:pyi_check
"%PY%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 goto pyi_fail

:pyi_ready
echo [OK] PyInstaller version:
"%PY%" -m PyInstaller --version

rem Clean old build and dist artifacts
echo [INFO] Cleaning old build and dist artifacts...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%BUILD_DIR%" goto clean_fail
if exist "%OUTPUT%" del /f /q "%OUTPUT%"
if exist "%OUTPUT%" goto clean_fail
if exist "%DIST_DIR%\PixFilter_v1.0" rmdir /s /q "%DIST_DIR%\PixFilter_v1.0"
if exist "%DIST_DIR%\PixFilter_v1.0" goto clean_fail
if exist "%SPEC_FILE%" del /f /q "%SPEC_FILE%"
if exist "%OLD_SPEC%" del /f /q "%OLD_SPEC%"

rem Build onefile windowed executable
echo [INFO] Starting PyInstaller build. Please wait...
echo [INFO] First build can take several minutes.
echo.

"%PY%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name "PixFilter_v1.0" ^
  --distpath "%DIST_DIR%" ^
  --workpath "%BUILD_DIR%" ^
  --specpath "%ROOT%." ^
  --hidden-import "tkinter" ^
  --hidden-import "PIL._tkinter_finder" ^
  --hidden-import "PIL.ImageTk" ^
  --hidden-import "PIL.Image" ^
  --hidden-import "numpy" ^
  --hidden-import "scipy.ndimage" ^
  --hidden-import "scipy.fft" ^
  --hidden-import "scipy.fftpack" ^
  --hidden-import "scipy.signal" ^
  --hidden-import "imagehash" ^
  --hidden-import "pywt" ^
  --collect-submodules "scipy" ^
  --collect-submodules "imagehash" ^
  --collect-all "pywt" ^
  --exclude-module "matplotlib" ^
  --exclude-module "pandas" ^
  --exclude-module "PyQt5" ^
  --exclude-module "PyQt6" ^
  --exclude-module "IPython" ^
  --exclude-module "notebook" ^
  --exclude-module "pytest" ^
  "%ENTRY%"

if errorlevel 1 goto build_fail
if not exist "%OUTPUT%" goto missing_output

echo [OK] Build completed.
echo Output:
echo "%OUTPUT%"
echo Note: onefile first start may be slower.
echo Note: some antivirus software may report a false positive.
echo Note: verify the exe on a machine without Python installed.
pause
exit /b 0

:no_python
echo [ERROR] Python was not found. Install Python 3.10+ and add it to PATH.
pause
exit /b 1

:missing_entry
echo [ERROR] Entry file not found:
echo "%ENTRY%"
pause
exit /b 1

:missing_requirements
echo [ERROR] Requirements file not found:
echo "%REQ%"
pause
exit /b 1

:dep_fail
echo [ERROR] Dependency install or validation failed. Run manually:
echo "%PY%" -m pip install -r "%REQ%"
pause
exit /b 1

:pyi_fail
echo [ERROR] PyInstaller install or validation failed. Run manually:
echo "%PY%" -m pip install pyinstaller
pause
exit /b 1

:clean_fail
echo [ERROR] Could not clean old build files. Close any program using build or dist and retry.
pause
exit /b 1

:build_fail
echo [ERROR] PyInstaller build failed. Review the output above.
pause
exit /b 1

:missing_output
echo [ERROR] Expected output was not created:
echo "%OUTPUT%"
pause
exit /b 1
