@echo off
REM ===========================================================================
REM  VERIFY the ported kernel files actually compile under Abaqus 2023's
REM  embedded Python 2.7. This is the authoritative check (the author's machine
REM  had no local CPython 2.x). Run from a normal Command Prompt where the
REM  `abaqus` command is available.
REM ===========================================================================
setlocal
cd /d "%~dp0"

where abaqus >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 'abaqus' not found on PATH. Run this from a shell where Abaqus
    echo         commands work (e.g. after the Abaqus environment is set up).
    exit /b 1
)

echo === Embedded interpreter version (expect 2.7.15) ===
abaqus python -c "import sys; print(sys.version)"
echo.

echo === Compile-check diagnostics.py under 2.7 ===
abaqus python -m py_compile abaqus_mcp_bridge\diagnostics.py && echo   OK: diagnostics.py compiles on 2.7 || goto :err

echo === Compile-check agent.py under 2.7 ===
abaqus python -m py_compile abaqus_mcp_bridge\agent.py && echo   OK: agent.py compiles on 2.7 || goto :err

echo === Behaviour smoke-test of the AST fallback under 2.7 ===
abaqus python tests\smoke_diagnostics.py && echo   OK: recovery works on 2.7 || goto :err

echo.
echo ALL CHECKS PASSED -- the port is valid on Abaqus 2023 / Python 2.7.15.
goto :eof

:err
echo.
echo [FAIL] A step above errored. Copy the traceback and report it.
exit /b 1
