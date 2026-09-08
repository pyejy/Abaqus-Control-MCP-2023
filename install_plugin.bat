@echo off
REM ===========================================================================
REM  Install the Abaqus/CAE GUI-side MCP bridge PLUGIN for Abaqus 2023.
REM
REM  Real-world lesson: Abaqus 2023 only LOADS a GUI plug-in that lives in a
REM  "<Name>_<ver>\<Name>_plugin.py + <Name>\" sub-package folder. A flat .py in
REM  the plug-ins dir is COMPILED but never executed (that's why the bridge menu
REM  never appeared until we switched to this layout).
REM
REM  This script therefore:
REM    1. builds the sub-package from source   (python build_plugin_pkg.py)
REM    2. removes any STALE copy of the plugin in the target dir
REM    3. copies the fresh <Name>_<ver> folder into the CAE plug-in directory
REM
REM  Default target: E:\Abaqus2023\Temp\abaqus_plugins   (your CAE reads this)
REM  Override with:  set ABAQUS_MCP_PLUGIN_DIR=... & install_plugin.bat
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM ---- pick the CAE plug-in directory ------------------------------
if "%ABAQUS_MCP_PLUGIN_DIR%"=="" set "ABAQUS_MCP_PLUGIN_DIR=E:\Abaqus2023\Temp\abaqus_plugins"

echo Target plug-in dir: %ABAQUS_MCP_PLUGIN_DIR%
if not exist "%ABAQUS_MCP_PLUGIN_DIR%" (
    echo [ERROR] plug-in dir does not exist: %ABAQUS_MCP_PLUGIN_DIR%
    echo   If your Abaqus uses a different location, set ABAQUS_MCP_PLUGIN_DIR first,
    echo   e.g.  set ABAQUS_MCP_PLUGIN_DIR=C:\Users\you\abaqus_plugins
    exit /b 1
)

REM ---- 1) build the deployable folder from source ------------------
echo [1/3] Building plugin package from source...
where python >nul 2>nul || (echo [ERROR] python not found on PATH & exit /b 1)
python build_plugin_pkg.py || goto :err

REM ---- 2) remove stale copy ----------------------------------------
set "PKG_DST=%ABAQUS_MCP_PLUGIN_DIR%\AbaqusMcpBridge_1.0.0"
if exist "%PKG_DST%" (
    echo [2/3] Removing stale copy in target dir...
    rmdir /s /q "%PKG_DST%" || goto :err
) else (
    echo [2/3] No stale copy present.
)

REM ---- 3) copy fresh package ---------------------------------------
echo [3/3] Copying fresh package to plug-in dir...
xcopy /E /I /Y "_pkg_out\AbaqusMcpBridge_1.0.0" "%PKG_DST%" >nul || goto :err

echo.
echo Installed. Files in %PKG_DST%:
dir /b "%PKG_DST%"
echo.
echo ---------------------------------------------------------------------------
echo  Next steps in Abaqus/CAE 2023:
echo    1. FULLY RESTART CAE (plug-ins load at startup).
echo    2. Menu: Plug-ins -^> Abaqus-Control-MCP -^> Start MCP Bridge
echo    3. Message area should print: Listening on 127.0.0.1:48152
echo    4. From a normal cmd verify end-to-end:
echo         cd /d %~dp0
echo         python -m abaqus_mcp_bridge.cli check
echo       (expects python: 2.7.15 and ok:true)
echo ---------------------------------------------------------------------------
goto :eof

:err
echo.
echo [ERROR] install failed. Fix the message above and rerun.
exit /b 1
