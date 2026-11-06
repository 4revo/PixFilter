@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "ENTRY=%ROOT%main.py"
set "REQ=%ROOT%requirements.txt"
set "BUILD_DIR=%ROOT%build_onedir"
set "DIST_DIR=%ROOT%dist_onedir"
set "APP_DIR=%DIST_DIR%\PixFilter_v1.0.3"
set "FINAL_DIR=%DIST_DIR%\PixFilter_v1.0.3_folder"
set "OUTPUT=%FINAL_DIR%\PixFilter_v1.0.3.exe"
set "CURRENT_DIR_REL=dist_onedir\PixFilter_v1.0.3"
set "FINAL_DIR_REL=dist_onedir\PixFilter_v1.0.3_folder"
set "SPEC_FILE=%ROOT%PixFilter_onedir.spec"
set "GEN_SPEC=%ROOT%PixFilter_v1.0.3.spec"
set "ICON=%ROOT%PixFilter.ico"

echo PixFilter 1.0.3 onedir build script
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

rem Clean only this script's output folders and intermediate build dir.
rem IMPORTANT: never remove dist, and never remove other contents of dist_onedir.
echo [INFO] Cleaning onedir build directory and same-name artifacts...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%BUILD_DIR%" goto clean_fail

if not exist "%FINAL_DIR%" goto final_dir_removed
set "LOCKED_PATH=%FINAL_DIR%"
rmdir /s /q "%FINAL_DIR%" 2>nul
if exist "%FINAL_DIR%" goto output_locked
echo [INFO] Removing %FINAL_DIR_REL% (same version overwrite)

:final_dir_removed
if not exist "%APP_DIR%" goto current_dir_absent
set "LOCKED_PATH=%APP_DIR%"
rmdir /s /q "%APP_DIR%" 2>nul
if exist "%APP_DIR%" goto output_locked
echo [INFO] Removing %CURRENT_DIR_REL% (same version overwrite)
goto cleanup_specs

:current_dir_absent
echo [INFO] Current version directory not found: %CURRENT_DIR_REL%

:cleanup_specs
rem Remove only this script's generated spec; the onefile spec is not touched.
if exist "%GEN_SPEC%" del /f /q "%GEN_SPEC%"

rem Build onedir windowed executable
rem NOTE: onedir starts faster than onefile because it does not self-extract.
echo [INFO] Starting PyInstaller onedir build. Please wait...
echo [INFO] First build can take several minutes.
echo.

"%PY%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
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

rem PyInstaller names the folder after --name. Rename it to the _folder suffix.
if not exist "%APP_DIR%\PixFilter_v1.0.3.exe" goto missing_output
echo [INFO] Renaming output folder to %FINAL_DIR_REL%...
ren "%APP_DIR%" "PixFilter_v1.0.3_folder"
if errorlevel 1 goto rename_fail
if not exist "%OUTPUT%" goto missing_output

rem Refresh this script's canonical spec after a good build.
if exist "%GEN_SPEC%" move /y "%GEN_SPEC%" "%SPEC_FILE%" >nul
if exist "%GEN_SPEC%" goto spec_sync_fail

echo [OK] Build completed.
echo Output folder:
echo "%FINAL_DIR%"
echo Executable:
echo "%OUTPUT%"
echo Spec:
echo "%SPEC_FILE%"
echo Note: onedir starts faster than onefile because it does not self-extract.
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
echo [ERROR] Could not clean old build files. Close any program using build_onedir or dist_onedir and retry.
pause
exit /b 1

:output_locked
echo [ERROR] Cannot remove the same-version artifact:
echo "%LOCKED_PATH%"
echo [ERROR] The folder is in use. Please close any running PixFilter window and run this script again.
pause
exit /b 1

:rename_fail
echo [ERROR] Build finished but the output folder could not be renamed.
echo Expected intermediate:
echo "%APP_DIR%"
echo Expected final:
echo "%FINAL_DIR%"
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
