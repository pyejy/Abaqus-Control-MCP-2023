@echo off
REM ===========================================================================
REM  Install the MCP SERVER side into the SYSTEM Python 3 (needs mcp + anyio).
REM  Run this from a normal Command Prompt / PowerShell. It does NOT touch
REM  Abaqus' embedded Python 2.7 -- that side is handled by install_plugin.bat.
REM ===========================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found on PATH. Use your system Python 3.10+.
    exit /b 1
)

echo [1/2] Upgrading pip and build backend...
python -m pip install --upgrade pip hatchling || goto :err

echo [2/2] Installing package (editable) with deps mcp, anyio...
python -m pip install -e . || goto :err

echo.
echo ===========================================================================
echo  Server installed. Entry points now available:
echo     abaqus-control-mcp-server
echo     abaqus-control-check / doctor / setup
echo.
echo  Register with Claude Code (MCP stdio server):
echo     claude mcp add abaqus -- ^
echo         python -m abaqus_mcp_bridge.server
echo  (or point your MCP client's command at "abaqus-control-mcp-server")
echo ===========================================================================
goto :eof

:err
echo [ERROR] installation failed; see messages above.
exit /b 1
