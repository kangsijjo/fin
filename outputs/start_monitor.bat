@echo off
REM ============================================================
REM  KIS Data Collection Monitor launcher
REM  This window only shows status. Closing it does NOT stop collection.
REM ============================================================
cd /d "%~dp0"
title KIS Data Collection Monitor
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0monitor.ps1"