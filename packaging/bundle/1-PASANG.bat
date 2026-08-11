@echo off
rem Pemasangan MoM-IGD. Klik dua kali berkas ini.
rem
rem -ExecutionPolicy Bypass hanya berlaku untuk proses PowerShell ini saja.
rem Kebijakan di laptop Anda tidak diubah sama sekali.
title MoM-IGD - Pemasangan
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\pasang.ps1"
echo.
pause
