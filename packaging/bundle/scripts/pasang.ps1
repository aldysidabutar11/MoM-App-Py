# =============================================================================
# MoM-IGD - pemasangan offline.
#
# Skrip ini memasang aplikasi dari bahan yang sudah ada di dalam paket ini.
# Setelah paket ini ada di laptop, tidak ada satu langkah pun yang membutuhkan
# internet: seluruh dependensi Python ada di vendor\wheels, dan seluruh model AI
# ada di bahan\models.
#
# Yang TIDAK dilakukan skrip ini, dan alasannya:
#   - tidak mengubah PATH, registry, firewall atau variabel lingkungan sistem;
#   - tidak menyentuh pengaturan mikrofon Anda;
#   - tidak membuka mikrofon (perekaman hanya dimulai oleh tombol di aplikasi);
#   - tidak menghubungi jaringan apa pun.
#
# Python 3.12 adalah satu-satunya hal yang mungkin perlu dipasang ke sistem, dan
# skrip ini bertanya lebih dulu, tidak memasang diam-diam.
# =============================================================================

[CmdletBinding()]
param(
    # Tempat seluruh hasil kerja aplikasi disimpan: rekaman, transkrip, notulen,
    # model dan basis data. Harus di luar folder aplikasi.
    [string] $DataRoot = 'C:\MoM-IGD-Data',

    # Jawab "ya" untuk semua pertanyaan. Untuk pemasangan massal.
    [switch] $Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$Bundle  = Split-Path -Parent $PSScriptRoot
$AppDir  = Join-Path $Bundle 'app'
$Wheels  = Join-Path $Bundle 'vendor\wheels'
$Bahan   = Join-Path $Bundle 'bahan'
$VenvPy  = Join-Path $AppDir '.venv\Scripts\python.exe'

# Ruang yang benar-benar dipakai: model 4,3 GB + venv 1,5 GB, ditambah kelonggaran.
$RUANG_MINIMUM_GB = 8

function Judul($teks) {
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
    Write-Host "  $teks" -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
}
function Info($teks)  { Write-Host "    $teks" }
function Oke($teks)   { Write-Host "  OK   $teks" -ForegroundColor Green }
function Ingat($teks) { Write-Host "  !    $teks" -ForegroundColor Yellow }
function Mati($teks) {
    Write-Host ''
    Write-Host "  GAGAL  $teks" -ForegroundColor Red
    Write-Host ''
    Write-Host '  Tidak ada yang dirusak. Perbaiki hal di atas lalu jalankan lagi.' -ForegroundColor Red
    Write-Host ''
    exit 1
}
function Tanya($pertanyaan) {
    if ($Yes) { return $true }
    $j = Read-Host "  $pertanyaan [Y/n]"
    if ($j -eq '' -or $j -match '^[Yy]') { return $true }
    return $false
}

Write-Host ''
Write-Host '  MoM-IGD - Notulen Rapat Otomatis, sepenuhnya offline' -ForegroundColor White
Write-Host '  Pemasangan ini tidak membutuhkan internet sama sekali.' -ForegroundColor DarkGray

# --- 1. Kelengkapan paket ---------------------------------------------------
# Diperiksa lebih dulu supaya paket yang ter-ekstrak setengah jalan ketahuan
# sekarang, bukan setelah venv terlanjur dibuat.
Judul '1/8  Memeriksa kelengkapan paket'
foreach ($p in @($AppDir, $Wheels, $Bahan, (Join-Path $Bahan 'models'))) {
    if (-not (Test-Path $p)) {
        Mati "Bagian paket tidak ditemukan: $p`n         Ekstrak ulang berkas ZIP-nya sampai selesai."
    }
}
# Ambang bawah yang longgar. Yang dijaga di sini adalah paket yang jelas-jelas
# terpotong -- ZIP yang diekstrak setengah jalan -- bukan jumlah dependensi yang
# tepat, karena jumlah itu memang berubah seiring versi. Yang menentukan lengkap
# atau tidak adalah pip: ia dijalankan dengan --no-index, jadi satu wheel yang
# hilang menghentikannya dengan pesan yang menyebut nama paketnya.
$jumlahWheel = (Get-ChildItem $Wheels -Filter *.whl -File).Count
if ($jumlahWheel -lt 40) {
    Mati "Hanya $jumlahWheel dependensi ditemukan, seharusnya sekitar 60. Paket tidak lengkap."
}
$gguf = Get-ChildItem (Join-Path $Bahan 'models') -Recurse -Filter *.gguf -File
if ($gguf.Count -lt 1) { Mati 'Model notulen (.gguf) tidak ada di dalam paket.' }
Oke "$jumlahWheel dependensi, dan model AI lengkap"

# --- 2. Python 3.12 ---------------------------------------------------------
Judul '2/8  Mencari Python 3.12'

function Cari-Python312 {
    $kandidat = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $hasil = & py -3.12 -c "import sys; print(sys.executable)"
        if ($LASTEXITCODE -eq 0 -and $hasil) { $kandidat += $hasil.Trim() }
    }
    $kandidat += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    $kandidat += (Join-Path $env:ProgramFiles 'Python312\python.exe')

    foreach ($c in $kandidat) {
        if (-not $c) { continue }
        if (-not (Test-Path $c)) { continue }
        # Shim Microsoft Store: pengalihan filesystem-nya merusak pemuatan modul
        # native, dan ctranslate2 serta llama.cpp keduanya native.
        if ($c -like '*WindowsApps*') { continue }
        $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])"
        if ($LASTEXITCODE -eq 0 -and $v.Trim() -eq '3.12') { return $c }
    }
    return $null
}

$python = Cari-Python312
if (-not $python) {
    $pemasang = Join-Path $Bundle 'vendor\python-3.12.10-amd64.exe'
    Ingat 'Python 3.12 belum ada di laptop ini.'
    Info  ''
    Info  'Versi lain tidak bisa dipakai, dan ini bukan pilihan gaya:'
    Info  '  - Python 3.14 belum punya wheel untuk pustaka AI yang dipakai;'
    Info  '  - versi dari Microsoft Store merusak pemuatan modul native.'
    Info  ''
    if (-not (Test-Path $pemasang)) { Mati "Pemasang tidak ada di paket: $pemasang" }
    Info  "Pemasang resmi python.org ikut di dalam paket ini:"
    Info  "  $pemasang"
    Info  'Akan dipasang hanya untuk akun Anda, tanpa mengubah PATH sistem.'
    Write-Host ''
    if (-not (Tanya 'Pasang Python 3.12.10 sekarang?')) {
        Mati 'Dibatalkan. Pasang Python 3.12 lalu jalankan skrip ini lagi.'
    }
    Info 'Memasang, mohon tunggu...'
    $p = Start-Process -FilePath $pemasang -Wait -PassThru -ArgumentList @(
        '/passive', 'InstallAllUsers=0', 'PrependPath=0',
        'Include_launcher=1', 'Include_test=0', 'Include_doc=0'
    )
    if ($p.ExitCode -ne 0) { Mati "Pemasang Python berhenti dengan kode $($p.ExitCode)." }
    $python = Cari-Python312
    if (-not $python) { Mati 'Python 3.12 masih belum terdeteksi setelah pemasangan.' }
}
$versiPenuh = (& $python -c "import sys; print(sys.version.split()[0])").Trim()
Oke "Python $versiPenuh  ($python)"

# --- 3. Folder data ---------------------------------------------------------
Judul '3/8  Menentukan tempat penyimpanan hasil'
Info 'Semua hasil kerja disimpan di satu folder, di luar folder aplikasi:'
Info 'rekaman, transkrip, notulen jadi, model AI dan basis data.'
Write-Host ''
if (-not $Yes) {
    $jawab = Read-Host "  Folder data [$DataRoot]"
    if ($jawab.Trim() -ne '') { $DataRoot = $jawab.Trim() }
}
$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)

if ($DataRoot.StartsWith($Bundle, [StringComparison]::OrdinalIgnoreCase)) {
    Mati "Folder data tidak boleh berada di dalam folder aplikasi.`n         Pilih tempat lain, misalnya C:\MoM-IGD-Data."
}
$drive = [System.IO.Path]::GetPathRoot($DataRoot)
if (-not (Test-Path $drive)) {
    Mati "Drive $drive tidak ada di laptop ini.`n         Pilih drive yang benar-benar ada, misalnya C:\MoM-IGD-Data."
}
$bebasGB = [math]::Round((Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($drive.TrimEnd('\'))'").FreeSpace / 1GB, 1)
if ($bebasGB -lt $RUANG_MINIMUM_GB) {
    Mati "Sisa ruang di $drive hanya $bebasGB GB. Dibutuhkan sekitar $RUANG_MINIMUM_GB GB."
}
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
Oke "$DataRoot  (sisa ruang $bebasGB GB)"

# --- 4. Lingkungan Python ---------------------------------------------------
Judul '4/8  Menyiapkan lingkungan Python'
Info 'Dipasang di dalam folder aplikasi saja. Python sistem Anda tidak diubah.'
if (Test-Path (Join-Path $AppDir '.venv')) {
    Info 'Lingkungan lama ditemukan, dibuat ulang supaya bersih.'
    Remove-Item (Join-Path $AppDir '.venv') -Recurse -Force
}
& $python -m venv (Join-Path $AppDir '.venv')
if ($LASTEXITCODE -ne 0) { Mati 'Pembuatan lingkungan Python gagal.' }
if (-not (Test-Path $VenvPy)) { Mati 'Lingkungan Python terbentuk tanpa python.exe.' }
Oke 'Lingkungan Python siap'

# --- 5. Dependensi, dari dalam paket ----------------------------------------
Judul '5/8  Memasang dependensi (dari paket, bukan dari internet)'
Info "$jumlahWheel paket dari vendor\wheels. Sekitar satu sampai dua menit."
# PIP_NO_INDEX membuat kegagalan menjadi jelas: kalau ada yang kurang, pip
# berhenti dan mengatakannya, bukan diam-diam mengunduh dari internet.
$env:PIP_NO_INDEX          = '1'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
& $VenvPy -m pip install --no-index --find-links $Wheels -r (Join-Path $AppDir 'requirements.txt') --quiet
$kodeRuntime = $LASTEXITCODE
& $VenvPy -m pip install --no-index --find-links $Wheels -r (Join-Path $AppDir 'requirements-dev.txt') --quiet
$kodeDev = $LASTEXITCODE
Remove-Item Env:\PIP_NO_INDEX -ErrorAction SilentlyContinue
Remove-Item Env:\PIP_DISABLE_PIP_VERSION_CHECK -ErrorAction SilentlyContinue
if ($kodeRuntime -ne 0) { Mati 'Pemasangan dependensi utama gagal.' }
if ($kodeDev -ne 0)     { Mati 'Pemasangan dependensi pemeriksaan gagal.' }
Oke 'Semua dependensi terpasang'

# --- 6. Konfigurasi ---------------------------------------------------------
Judul '6/8  Menulis konfigurasi mesin ini'
$templat = Join-Path $Bahan 'local.toml.templat'
if (-not (Test-Path $templat)) { Mati "Templat konfigurasi tidak ada: $templat" }
$isi = [System.IO.File]::ReadAllText($templat)

# Ditulis sebagai TOML "basic string" -- kutip ganda, backslash di-escape.
#
# Versi sebelumnya menggandakan backslash lalu meletakkannya di dalam kutip
# TUNGGAL. Kutip tunggal di TOML adalah literal string: isinya diambil apa
# adanya, tanpa memproses escape. Jadi yang tersimpan benar-benar
# "D:\\MoM-IGD-Data" dengan dua backslash. Itu lolos tanpa terlihat karena
# pathlib meringkas pemisah berulang, tetapi nilainya salah di setiap tempat
# lain yang membacanya -- dan path jaringan \\server\share akan menjadi
# \\\\server\\share, yang bukan lagi path yang sama.
#
# Kutip ganda dengan escape menangani keduanya, termasuk path yang mengandung
# tanda kutip -- yang tidak mungkin diwakili oleh literal string sama sekali.
$escaped = $DataRoot.Replace('\', '\\').Replace('"', '\"')
$isi = $isi.Replace('{{DATA_ROOT}}', '"' + $escaped + '"')
# Penanda yang belum terisi berarti paketnya dibangun dengan cara yang salah.
#
# Tanpa pemeriksaan ini, konfigurasi rusak tetap ditulis dan pemasangan berjalan
# terus sampai langkah berikutnya, lalu berhenti dengan "Pembuatan basis data
# gagal" dan sepuluh baris stack trace tomllib. Tidak ada satu pun di antaranya
# yang memberi tahu operator apa yang harus dilakukan. Kegagalan di sini menyebut
# penandanya dan menyebut penyebabnya.
# Angka termasuk: penanda RASIO_PASS2 berakhiran "2", dan pola tanpa \d
# melewatkannya diam-diam -- persis kesalahan yang pemeriksaan ini ada untuk
# mencegahnya, dua baris di bawah tempat ia ditulis.
$tersisa = [regex]::Matches($isi, '\{\{[A-Z0-9_]+\}\}') | ForEach-Object { $_.Value } | Sort-Object -Unique
if ($tersisa) {
    Mati ("Templat konfigurasi belum lengkap: " + ($tersisa -join ', ') + ".`n" +
          "         Paket ini dibangun dengan tidak benar -- build_bundle.py yang`n" +
          "         seharusnya mengisi penanda itu. Minta paket yang baru.")
}

$tujuan = Join-Path $AppDir 'config\local.toml'
[System.IO.File]::WriteAllText($tujuan, $isi, (New-Object System.Text.UTF8Encoding($false)))
Oke "config\local.toml -> $DataRoot"

# --- 7. Basis data, model, logo, daftar peserta -----------------------------
Judul '7/8  Menyiapkan basis data dan memasang model AI'
Push-Location $AppDir
try {
    & $VenvPy -m mom_igd db init
    if ($LASTEXITCODE -ne 0) { Mati 'Pembuatan basis data gagal.' }
    Oke 'Basis data dibuat'

    Info 'Menyalin model AI (4,3 GB). Ini bagian paling lama, sekitar 1-3 menit.'
    $modelTujuan = Join-Path $DataRoot 'models'
    New-Item -ItemType Directory -Force -Path $modelTujuan | Out-Null
    Copy-Item (Join-Path $Bahan 'models\*') -Destination $modelTujuan -Recurse -Force
    if (-not (Test-Path (Join-Path $modelTujuan 'installed.json'))) {
        Mati 'Daftar model tidak ikut tersalin.'
    }
    Oke 'Model AI terpasang'

    $logoSumber = Join-Path $Bahan 'branding'
    if (Test-Path $logoSumber) {
        $logoTujuan = Join-Path $DataRoot 'branding'
        New-Item -ItemType Directory -Force -Path $logoTujuan | Out-Null
        Copy-Item (Join-Path $logoSumber '*') -Destination $logoTujuan -Recurse -Force
        Oke 'Logo kop dokumen terpasang'
    }

    $benihPeserta = Join-Path $Bahan 'participants.local.toml'
    if (Test-Path $benihPeserta) {
        Copy-Item $benihPeserta -Destination (Join-Path $AppDir 'config\participants.local.toml') -Force
        & $VenvPy -m mom_igd participant import
        if ($LASTEXITCODE -ne 0) { Ingat 'Daftar peserta gagal dimuat. Aplikasi tetap jalan; tambahkan peserta lewat aplikasi.' }
        else { Oke 'Daftar peserta dimuat' }
    }
}
finally { Pop-Location }

# --- 8. Pemeriksaan akhir ---------------------------------------------------
Judul '8/8  Memeriksa hasil pemasangan'
Push-Location $AppDir
try {
    & $VenvPy -m mom_igd doctor
    $kodeDoctor = $LASTEXITCODE
}
finally { Pop-Location }

Write-Host ''
if ($kodeDoctor -eq 0) {
    Write-Host ('=' * 74) -ForegroundColor Green
    Write-Host '  SELESAI. Aplikasi siap dipakai.' -ForegroundColor Green
    Write-Host ('=' * 74) -ForegroundColor Green
    Write-Host ''
    Write-Host '  Menjalankan  : klik dua kali  2-JALANKAN.bat'
    Write-Host "  Hasil notulen: $DataRoot\exports"
    Write-Host '  Panduan pakai: PANDUAN.md'
    Write-Host ''
    Write-Host '  Baris bertanda WARN di atas adalah hal opsional, bukan kesalahan.' -ForegroundColor DarkGray
    Write-Host '  Yang menentukan adalah FAIL: 0.' -ForegroundColor DarkGray
    Write-Host ''
    exit 0
}
Write-Host ('=' * 74) -ForegroundColor Yellow
Write-Host '  Pemasangan selesai, tetapi pemeriksaan menemukan FAIL.' -ForegroundColor Yellow
Write-Host ('=' * 74) -ForegroundColor Yellow
Write-Host ''
Write-Host '  Setiap baris FAIL di atas menyebutkan cara memperbaikinya.'
Write-Host '  Penyebab paling sering: mikrofon belum terpasang di laptop ini.'
Write-Host '  Pasang mikrofonnya, lalu jalankan 3-PERIKSA.bat untuk memeriksa lagi.'
Write-Host ''
exit 1
