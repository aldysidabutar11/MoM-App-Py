@echo off
rem Membuka aplikasi MoM-IGD. Klik dua kali berkas ini.
rem Jendela hitam yang muncul biarkan terbuka selama aplikasi dipakai.
title MoM-IGD
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\jalankan.ps1"
