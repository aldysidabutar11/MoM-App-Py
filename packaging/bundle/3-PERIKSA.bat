@echo off
rem Memeriksa kesehatan pemasangan. Tidak mengubah apa pun.
rem Jalankan ini kalau ada yang terasa tidak beres.
title MoM-IGD - Pemeriksaan
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\periksa.ps1"
