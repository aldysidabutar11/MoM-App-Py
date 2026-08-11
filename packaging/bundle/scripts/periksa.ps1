# Pemeriksaan kesehatan pemasangan. Tidak mengubah apa pun.
#
# Tiga tingkat, dari yang paling cepat:
#   doctor      - apa yang siap dan apa yang kurang di laptop ini
#   asr verify  - membaca ulang setiap byte model dan mencocokkan sidik jarinya
#   pytest      - seluruh uji otomatis aplikasi, sekitar 10 menit
#
# Tingkat ketiga hanya berjalan bila diminta dengan -Lengkap, karena lama.

[CmdletBinding()]
param([switch] $Lengkap)

$ErrorActionPreference = 'Stop'

$Bundle = Split-Path -Parent $PSScriptRoot
$AppDir = Join-Path $Bundle 'app'
$VenvPy = Join-Path $AppDir '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPy)) {
    Write-Host ''
    Write-Host '  Aplikasi belum dipasang. Jalankan 1-PASANG.bat dahulu.' -ForegroundColor Yellow
    Write-Host ''
    Read-Host '  Tekan Enter untuk menutup'
    exit 1
}

function Judul($teks) {
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
    Write-Host "  $teks" -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
}

Push-Location $AppDir
try {
    Judul 'Kesiapan laptop ini'
    & $VenvPy -m mom_igd doctor
    $kodeDoctor = $LASTEXITCODE

    Judul 'Keutuhan model AI'
    Write-Host '  Membaca ulang 4,3 GB dan mencocokkan sidik jarinya. Sekitar satu menit.'
    & $VenvPy -m mom_igd asr verify
    $kodeModel = $LASTEXITCODE

    $kodeUji = 0
    if ($Lengkap) {
        Judul 'Uji otomatis aplikasi'
        Write-Host '  Sekitar 10 menit. Tidak memakai mikrofon, model, atau jaringan.'
        & $VenvPy -m pytest -q
        $kodeUji = $LASTEXITCODE
    }
}
finally { Pop-Location }

Write-Host ''
Write-Host ('=' * 74)
if ($kodeDoctor -eq 0) { Write-Host '  Kesiapan laptop : OK, tidak ada FAIL' -ForegroundColor Green }
else                   { Write-Host '  Kesiapan laptop : ADA FAIL, lihat di atas' -ForegroundColor Red }
if ($kodeModel -eq 0)  { Write-Host '  Model AI        : utuh' -ForegroundColor Green }
else                   { Write-Host '  Model AI        : BERMASALAH, pasang ulang dari paket' -ForegroundColor Red }
if ($Lengkap) {
    if ($kodeUji -eq 0) { Write-Host '  Uji otomatis    : lulus semua' -ForegroundColor Green }
    else                { Write-Host '  Uji otomatis    : ADA YANG GAGAL' -ForegroundColor Red }
} else {
    Write-Host '  Uji otomatis    : dilewati (jalankan lewat PowerShell dengan -Lengkap)' -ForegroundColor DarkGray
}
Write-Host ('=' * 74)
Write-Host ''
Read-Host '  Tekan Enter untuk menutup'
if ($kodeDoctor -ne 0 -or $kodeModel -ne 0 -or $kodeUji -ne 0) { exit 1 }
exit 0
