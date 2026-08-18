@echo off
REM Build a portable --onedir distribution (U11 -- R18).
REM
REM Deliberately creates its own venv rather than building from whatever
REM Python happens to be active. Anaconda in particular bundles extra DLLs
REM that PyInstaller then pulls into the bundle, inflating it and causing
REM import errors at runtime (KTD8).

setlocal

echo.
echo === Creating a clean build environment ===
if exist build_venv (
    echo Reusing existing build_venv
) else (
    py -3 -m venv build_venv || python -m venv build_venv
    if errorlevel 1 goto :failed
)

call build_venv\Scripts\activate.bat

echo.
echo === Installing dependencies ===
python -m pip install --upgrade pip --quiet
python -m pip install -e . --quiet
if errorlevel 1 goto :failed
REM Pinned rather than floating: a PyInstaller release is the single most
REM likely thing to break this build, and a known-good version costs nothing.
python -m pip install "pyinstaller==6.21.0" --quiet
if errorlevel 1 goto :failed

echo.
echo === Building ===
pyinstaller --noconfirm --clean airplane-notifier.spec
if errorlevel 1 goto :failed

echo.
echo === Build complete ===
echo Output: dist\airplane-notifier\airplane-notifier.exe
echo.
echo Before running it, copy your credentials.json into
echo   dist\airplane-notifier\
echo It is not bundled, so the OAuth client secret stays out of the build.
echo.
goto :done

:failed
echo.
echo *** BUILD FAILED ***
echo If venv creation failed, ensure a vanilla Python 3 is on PATH.
exit /b 1

:done
endlocal
