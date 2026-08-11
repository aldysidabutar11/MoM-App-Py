# Membuka jendela aplikasi MoM-IGD.
#
# Perintahnya harus dijalankan dari dalam folder app, karena konfigurasi dicari
# secara relatif terhadap folder kerja (config\default.toml lalu config\local.toml).
# Menjalankannya dari tempat lain akan membuka pemasangan yang salah.

$ErrorActionPreference = 'Stop'

$Bundle = Split-Path -Parent $PSScriptRoot
$AppDir = Join-Path $Bundle 'app'
$VenvPy = Join-Path $AppDir '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPy)) {
    Write-Host ''
    Write-Host '  Aplikasi belum dipasang di laptop ini.' -ForegroundColor Yellow
    Write-Host '  Klik dua kali 1-PASANG.bat terlebih dahulu.' -ForegroundColor Yellow
    Write-Host ''
    Read-Host '  Tekan Enter untuk menutup'
    exit 1
}

Write-Host ''
Write-Host '  Membuka MoM-IGD...' -ForegroundColor Cyan
Write-Host '  Jendela hitam ini biarkan terbuka selama aplikasi dipakai.' -ForegroundColor DarkGray
Write-Host ''

Push-Location $AppDir
try { & $VenvPy -m mom_igd shell; $kode = $LASTEXITCODE }
finally { Pop-Location }

if ($kode -ne 0) {
    Write-Host ''
    Write-Host "  Aplikasi berhenti dengan kode $kode." -ForegroundColor Yellow
    Write-Host '  Jalankan 3-PERIKSA.bat untuk melihat apa yang kurang.' -ForegroundColor Yellow
    Write-Host ''
    Read-Host '  Tekan Enter untuk menutup'
}
exit $kode
