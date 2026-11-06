@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "ENTRY=%ROOT%main.py"
set "REQ=%ROOT%requirements.txt"
set "BUILD_DIR=%ROOT%build"
set "DIST_DIR=%ROOT%dist"
set "OUTPUT=%DIST_DIR%\PixFilter_v1.0.3.exe"
set "CURRENT_EXE_REL=dist\PixFilter_v1.0.3.exe"
set "SPEC_FILE=%ROOT%PixFilter.spec"
set "GEN_SPEC=%ROOT%PixFilter_v1.0.3.spec"
set "OLD_SPEC=%ROOT%PixFilter_v1.0.1.spec"
set "LEGACY_SPEC=%ROOT%PixFilter_v1.0.spec"
set "ICON=%ROOT%PixFilter.ico"

echo PixFilter 1.0.3 onefile build script
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
if not exist "%ICON%" goto missing_icon

rem Check project dependencies
set "DEP_CHECK=import PIL, imagehash, numpy, scipy, scipy.ndimage, scipy.fft, scipy.fftpack, scipy.signal, pywt, PySide6.QtCore, PySide6.QtGui, PySide6.QtWidgets, PySide6.QtSvg"
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

rem Clean build output.
rem IMPORTANT: never remove the whole dist directory. Only the current version
rem artifacts are overwritten; older version files must be kept.
echo [INFO] Cleaning build directory and current version artifacts...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%BUILD_DIR%" goto clean_fail

echo [INFO] Keeping other files in "%DIST_DIR%" (for example PixFilter_v1.0.1.exe).
echo [INFO] Only the current onefile exe will be replaced.
if not exist "%OUTPUT%" goto current_exe_absent
set "LOCKED_PATH=%OUTPUT%"
del /f /q "%OUTPUT%" 2>nul
if exist "%OUTPUT%" goto output_locked
echo [INFO] Removing %CURRENT_EXE_REL% (same version overwrite)
goto cleanup_specs

:current_exe_absent
echo [INFO] Current version output not found: %CURRENT_EXE_REL%

:cleanup_specs
rem Remove only stale generated/legacy spec files; never touch unrelated specs.
rem The canonical PixFilter.spec is refreshed from GEN_SPEC after a good build.
if exist "%GEN_SPEC%" del /f /q "%GEN_SPEC%"
if exist "%OLD_SPEC%" del /f /q "%OLD_SPEC%"
if exist "%LEGACY_SPEC%" del /f /q "%LEGACY_SPEC%"

rem Build onefile windowed executable
rem NOTE: onefile self-extracts on first launch; PySide6 bundle is large, expect a slower startup.
echo [INFO] Starting PyInstaller build. Please wait...
echo [INFO] First build can take several minutes.
echo.

"%PY%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name "PixFilter_v1.0.3" ^
  --icon "PixFilter.ico" ^
  --add-data "PixFilter.ico;." ^
  --distpath "%DIST_DIR%" ^
  --workpath "%BUILD_DIR%" ^
  --specpath "%ROOT%." ^
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
  --hidden-import "PySide6.QtCore" ^
  --hidden-import "PySide6.QtGui" ^
  --hidden-import "PySide6.QtWidgets" ^
  --hidden-import "PySide6.QtSvg" ^
  --collect-all "PySide6" ^
  --collect-all "shiboken6" ^
  --exclude-module "matplotlib" ^
  --exclude-module "pandas" ^
  --exclude-module "tkinter" ^
  --exclude-module "_tkinter" ^
  --exclude-module "PyQt5" ^
  --exclude-module "PyQt6" ^
  --exclude-module "IPython" ^
  --exclude-module "notebook" ^
  --exclude-module "pytest" ^
  "%ENTRY%"

if errorlevel 1 goto build_fail
if not exist "%OUTPUT%" goto missing_output

rem Keep exactly one spec file: refresh the canonical name from the generated one.
if exist "%GEN_SPEC%" move /y "%GEN_SPEC%" "%SPEC_FILE%" >nul
if exist "%GEN_SPEC%" goto spec_sync_fail

echo [OK] Build completed.
echo Output:
echo "%OUTPUT%"
echo Spec:
echo "%SPEC_FILE%"
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

:missing_icon
echo [ERROR] Icon file not found:
echo "%ICON%"
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

:output_locked
echo [ERROR] Cannot remove the same-version artifact:
echo "%LOCKED_PATH%"
echo [ERROR] The file is in use. Please close any running PixFilter window and run this script again.
pause
exit /b 1

:spec_sync_fail
echo [ERROR] Could not refresh "%SPEC_FILE%" from the generated spec.
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
