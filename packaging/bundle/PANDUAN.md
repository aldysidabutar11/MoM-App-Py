# MoM-IGD — Panduan Pemakaian

Aplikasi ini mengubah **rekaman rapat** menjadi **notulen rapat berbentuk dokumen
Word**, seluruhnya di dalam laptop Anda.

Tidak ada satu pun bagian yang menghubungi internet. Tidak ada layanan awan, tidak ada
pengiriman rekaman ke server mana pun, tidak ada telemetri. Suara rapat, transkrip dan
notulennya tidak pernah meninggalkan mesin ini. Itu bukan pengaturan yang bisa
dinyalakan atau dimatikan — memang tidak ada kodenya.

---

## Isi ringkas

| | |
|---|---|
| [A. Yang dibutuhkan](#a-yang-dibutuhkan) | Sebelum mulai |
| [B. Pemasangan](#b-pemasangan-sekali-saja) | Sekali saja, sekitar 5 menit |
| [C. Menjalankan](#c-menjalankan) | Setiap hari |
| [D. Memakai, dari rapat sampai dokumen](#d-memakai-dari-rapat-sampai-dokumen) | Langkah 1 sampai 4 |
| [E. Di mana hasilnya](#e-di-mana-hasilnya) | Folder yang perlu dibuka |
| [F. Yang perlu diketahui sebelum dipakai sungguhan](#f-yang-perlu-diketahui-sebelum-dipakai-sungguhan) | **Baca bagian ini** |
| [G. Kalau ada masalah](#g-kalau-ada-masalah) | Gejala dan penyebabnya |
| [H. Isi paket ini](#h-isi-paket-ini) | Apa yang dikirim dan apa yang tidak |

---

## A. Yang dibutuhkan

| Kebutuhan | Keterangan |
|---|---|
| **Windows 10/11 64-bit** | Windows 11 sudah membawa komponen tampilan yang dipakai (WebView2). |
| **RAM 16 GB** | Bisa jalan di 8 GB, tetapi pembuatan notulen memakai sekitar 5,1 GB memori saat berjalan dan laptop akan terasa berat. Angka itu diukur, bukan perkiraan. |
| **Ruang kosong 8 GB** | Model AI 4,3 GB, ditambah rekaman dan hasil kerja. |
| **Mikrofon** | Mikrofon USB memberi hasil paling baik. Mikrofon bawaan laptop bisa dipakai untuk mencoba. |
| **Python 3.12** | **Tidak perlu disiapkan sendiri.** Kalau belum ada, pemasang resminya ikut di dalam paket ini dan skrip pemasangan akan menawarkannya. |
| **Internet** | **Tidak dibutuhkan sama sekali**, termasuk saat memasang. |

Kartu grafis NVIDIA tidak diperlukan dan tidak dipakai. Semua perhitungan berjalan di
prosesor biasa.

---

## B. Pemasangan (sekali saja)

**1. Ekstrak berkas ZIP-nya sampai selesai.**

Klik kanan → *Extract All*. Letakkan di folder yang tidak akan dipindah-pindah, misalnya
`C:\MoM-IGD-Offline`.

> Jangan menjalankan langsung dari dalam ZIP tanpa mengekstrak. Windows akan membukanya
> di folder sementara dan pemasangan akan hilang saat jendelanya ditutup.

**2. Klik dua kali `1-PASANG.bat`.**

Skrip akan menanyakan dua hal saja:

- **Apakah Python 3.12 boleh dipasang?** Hanya muncul kalau belum ada. Dipasang untuk
  akun Anda saja, tanpa mengubah PATH sistem.
- **Folder data mau di mana?** Tekan Enter untuk memakai `C:\MoM-IGD-Data`.

Setelah itu berjalan sendiri, sekitar 3 sampai 5 menit. Yang dikerjakannya:

1. memeriksa kelengkapan paket;
2. mencari atau memasang Python 3.12;
3. membuat folder data;
4. menyiapkan lingkungan Python **di dalam folder aplikasi** — Python sistem Anda tidak diubah;
5. memasang seluruh dependensi **dari dalam paket ini**, bukan dari internet;
6. menulis konfigurasi mesin ini;
7. membuat basis data, memasang model AI 4,3 GB, logo kop, dan daftar peserta;
8. memeriksa hasilnya.

**3. Baca baris terakhir.**

Yang Anda cari adalah **`FAIL: 0`**.

Baris bertanda `WARN` **bukan kesalahan**. Itu hal opsional atau hal untuk tahap
pengembangan berikutnya, dan pemasangan yang sehat pun memunculkan sekitar sembilan baris
`WARN` — pada mesin uji hasilnya `27 PASS, 9 WARN, 0 FAIL`. Yang menentukan hanya `FAIL`.

Dua `WARN` yang pasti muncul dan memang seharusnya begitu:

- **`usb_conference_microphone`** — belum ada mikrofon konferensi USB. Mikrofon bawaan
  laptop tetap bisa dipakai untuk mencoba, tetapi untuk rapat sungguhan di ruangan
  hasilnya kurang baik: mikrofon bawaan menajamkan suara yang paling dominan dan menekan
  yang lain, dan makin besar ruangannya makin parah.
- **`consent_text`** — teks persetujuan biometrik masih berstatus draf dan belum ditinjau
  bagian legal. Hanya relevan untuk fitur pengenalan suara, yang belum ada di versi ini.

Kalau ada `FAIL`, setiap barisnya menyebutkan cara memperbaikinya. Penyebab yang paling
sering adalah mikrofon yang belum tercolok.

Setelah pemasangan berhasil, **folder hasil ekstrak boleh dihapus** kalau butuh ruang —
tetapi menyimpannya lebih praktis, karena `3-PERIKSA.bat` ada di situ.

---

## C. Menjalankan

Klik dua kali **`2-JALANKAN.bat`**.

Akan muncul dua jendela:

- **jendela hitam** — mesin aplikasinya. **Biarkan terbuka.** Menutupnya menutup aplikasi.
- **jendela aplikasi** — yang Anda pakai.

Untuk menutup: tutup jendela aplikasinya. Jendela hitam akan ikut menutup.

**Mode gelap dan terang** ada di pojok kanan atas: *Sistem*, *Terang*, *Gelap*. Pilihannya
diingat sampai pemakaian berikutnya. Pilih **Terang** kalau layar susah terbaca di luar
ruangan.

---

## D. Memakai, dari rapat sampai dokumen

Aplikasi ini bekerja dalam empat langkah berurutan. Menu sebelah kiri memberi nomornya,
dan hasil langkah sebelumnya menjadi bahan langkah berikutnya.

### Langkah 1 — Rekam rapat

1. Buka **Langkah 1 — Rekam rapat**.
2. Pada kartu **Microphone**, pilih perangkat lalu klik **Gunakan perangkat ini**.
3. Klik **Tes microphone (10–15 s)** dan bicaralah seperti biasa. Ini membuktikan
   mikrofonnya benar-benar menangkap suara Anda, dan menunjukkan level masukannya.
   Sebaiknya jangan dilewati: mikrofon yang salah pilih baru ketahuan setelah rapat
   selesai, dan saat itu sudah terlambat.
4. Klik **Jalankan preflight**. Pemeriksaan sebelum merekam: ruang disk, perangkat,
   basis data.
5. Isi **Judul rapat**. Boleh dikosongkan — akan memakai stempel waktu.
6. Klik **Start**.

Selama merekam: **Pause**, **Resume**, dan **Stop**. Penghitung waktu berjalan, dan level
suara terlihat langsung. Panel **Suara ke teks** menampilkan potongan teks sementara agar
Anda yakin suaranya masuk — itu bukan transkrip akhir.

**Mikrofon tidak pernah terbuka sebelum Anda menekan tombol.** Tidak saat aplikasi
dibuka, tidak saat daftar perangkat ditampilkan.

Setelah **Stop**, rekaman tersimpan lengkap dengan sidik jari keutuhan. **Audio aslinya
tidak pernah diubah lagi** oleh tahap mana pun sesudahnya.

### Langkah 2 — Peserta rapat

Bisa dilakukan sebelum atau sesudah merekam.

1. Buka **Langkah 2 — Peserta rapat**.
2. Pada kartu **Roster rapat**, pilih rapatnya.
3. Ketik nama di kolom pencarian lalu klik **Cari**, dan pilih orangnya dari
   **Direktori peserta**.

Direktori sudah terisi rekan-rekan Anda saat pemasangan. Untuk menambah orang baru,
pakai kartu **Tambah peserta**.

> **Yang perlu diluruskan:** mengisi daftar peserta **tidak** membuat aplikasi mengenali
> siapa yang berbicara. Pengenalan suara adalah tahap pengembangan berikutnya dan belum
> ada di versi ini. Daftar peserta dipakai untuk dua hal: mencatat siapa yang hadir di
> kop notulen, dan **memperbaiki ejaan nama** yang memang disebut di dalam rapat.
>
> Karena itu label pembicara pada transkrip akan tetap tertulis `UNASSIGNED`, berapa pun
> banyaknya nama yang Anda masukkan. Itu bukan kerusakan; itu tanda jujur bahwa aplikasi
> tidak tahu, dan lebih baik daripada menebak.

### Langkah 3 — Ubah rekaman jadi teks

1. Buka **Langkah 3 — Ubah rekaman jadi teks**.
2. Pilih rekamannya pada kartu **Pilih rekaman**.
3. Klik tombol jalankan transkripsi.

Prosesnya dua tahap dan sengaja begitu: tahap pertama cepat mentranskrip seluruh rekaman,
tahap kedua mentranskrip ulang bagian-bagian yang paling meragukan dengan model yang jauh
lebih akurat.

**Perkiraan waktu:** sekitar **tiga perempat durasi rapat**. Rapat 60 menit perlu sekitar
40–45 menit. Angka ini dari pengukuran, bukan perkiraan optimis, dan laptop yang lebih
lambat akan lebih lama lagi.

Aplikasi boleh ditinggal. Kalau ingin lebih cepat dengan menukar sedikit ketepatan kata,
lihat catatan `pass2_budget_ratio` di bagian G.

### Langkah 4 — Notulen

1. Buka **Langkah 4 — Notulen**.
2. Pilih transkripnya.
3. Pilih **Format dokumen**: **Word (.docx)**, HTML, Markdown, atau teks biasa.
4. Klik tombol buat notulen.

Model bahasa membaca transkrip dan menyusun ringkasan, keputusan, poin tindakan beserta
pemiliknya dan tenggat waktunya.

Lamanya bergantung pada panjang rapat: rapat pendek selesai dalam hitungan menit, rapat
panjang jauh lebih lama. Tidak ada angka pasti yang disebut di sini karena belum diukur
pada berbagai panjang rapat — tidak seperti waktu transkripsi di Langkah 3, yang angkanya
memang dari pengukuran.

Selama proses berjalan laptop akan terasa berat: model ini memakai sekitar 5,1 GB memori,
di dalam proses terpisah yang berhenti sendiri setelah selesai.

Setelah selesai, hasilnya tampil di layar dan tombol **ekspor** muncul. Klik tombol itu
untuk menghasilkan berkas dokumennya.

Ada satu pilihan: **Sembunyikan poin yang belum terverifikasi dari dokumen**. Poinnya
tetap tersimpan di basis data dan jumlahnya tetap disebut di dokumen; yang berubah hanya
apakah isinya ikut dicetak.

---

## E. Di mana hasilnya

Semuanya di bawah folder data yang Anda pilih saat pemasangan — **di luar folder
aplikasi**, supaya menghapus atau memperbarui aplikasi tidak pernah menghapus hasil kerja.

Kalau tadi memakai bawaan, folder itu adalah `C:\MoM-IGD-Data`.

| Folder | Isinya |
|---|---|
| **`exports\`** | **Notulen jadi.** Word, HTML, Markdown dan teks. **Ini folder yang Anda buka.** |
| `recordings\` | Audio asli, satu folder per rapat, lengkap dengan sidik jari keutuhannya. Tidak pernah diubah setelah perekaman. |
| `working\` | Salinan kerja 16 kHz yang dibaca saat transkripsi. Turunan, aman dihapus. |
| `db\` | Basis data: rapat, peserta, transkrip, notulen. **Ini yang perlu dicadangkan.** |
| `models\` | Model AI, 4,3 GB. |
| `branding\` | Logo kop dokumen. |
| `logs\`, `temp\`, `backups\`, `keys\`, `voiceprints\` | Pendukung. |

Berkas notulen dinamai dengan nomor pengenal rapat, bukan judulnya, supaya nama orang
tidak pernah masuk ke nama berkas.

---

## F. Yang perlu diketahui sebelum dipakai sungguhan

Bagian ini yang paling penting dibaca. Semuanya disengaja.

**1. Hasilnya adalah DRAF, dan dokumennya mengatakan begitu.**

Ada tulisan draf tepat di bawah kop, dan tidak bisa dimatikan. Blok tanda tangan di bawah
sengaja **kosong tanpa nama**, dan dokumennya menyatakan bahwa aplikasi tidak menyetujui
apa pun. Alur persetujuan oleh manusia adalah tahap pengembangan berikutnya.

**Selalu baca dan koreksi sebelum dikirim.** Ini alat bantu penyusun draf, bukan pengganti
notulis.

**2. Aplikasi tidak tahu siapa yang berbicara.** Sudah dijelaskan di Langkah 2. Semua
segmen tertulis `UNASSIGNED`.

**3. Ketepatannya belum pernah diukur secara resmi.**

Pada pemakaian nyata hasilnya baik untuk bahasa Indonesia bercampur istilah teknis, tetapi
angka ketepatan resmi butuh transkrip pembanding yang dibuat manusia, dan itu belum ada.
Jadi tidak ada angka yang diklaim di sini. Perlakukan transkripnya sebagai bahan yang
perlu diperiksa.

**4. Nama pemilik tugas diperiksa, dan yang tidak terbukti akan dibuang.**

Sebuah poin tindakan hanya mempertahankan nama pemiliknya kalau nama itu **benar-benar
diucapkan** di dalam rapat. Kalau tidak, namanya dihapus dan penghapusannya dicatat serta
dihitung di dokumen.

Ini disengaja: pemilik tugas yang dikarang lebih berbahaya daripada poin yang hilang.
Yang hilang akan disadari orang yang hadir; yang dikarang akan dipercaya.

**5. Daftar peserta tidak pernah masuk ke bahan yang dibaca model.**

Daftar itu baru dipakai *setelah* sebuah nama terbukti diucapkan, dan hanya untuk
membetulkan ejaannya. Daftar peserta tidak akan pernah bisa memunculkan nama yang tidak
disebut di rapat.

**6. Keputusan yang kemudian dibatalkan akan ditandai** dan tidak dimasukkan ke ringkasan.

**7. Rekaman rapat berisi suara orang.** Ikuti aturan perusahaan tentang pemberitahuan dan
persetujuan sebelum merekam.

---

## G. Kalau ada masalah

**Langkah pertama untuk gejala apa pun:** klik dua kali **`3-PERIKSA.bat`**. Skrip itu
memeriksa kesiapan laptop dan membaca ulang seluruh 4,3 GB model untuk memastikan tidak
ada yang rusak. Tidak mengubah apa pun.

| Gejala | Penyebab dan penanganan |
|---|---|
| `1-PASANG.bat` berkedip lalu hilang | ZIP belum diekstrak, atau diekstrak sebagian. Ekstrak ulang sampai selesai. |
| "Python 3.12 masih belum terdeteksi" | Pemasangan Python dibatalkan atau diblokir. Jalankan `vendor\python-3.12.10-amd64.exe` sendiri, lalu ulangi `1-PASANG.bat`. |
| "Drive ... tidak ada di laptop ini" | Folder data yang diketik memakai drive yang tidak ada. Pakai `C:\MoM-IGD-Data`. |
| Aplikasi terbuka tetapi kosong/putih | Komponen WebView2 belum ada. Jarang terjadi di Windows 11. Pasang "Microsoft Edge WebView2 Runtime". |
| Tidak ada mikrofon di daftar | Cek colokannya, lalu *Settings → Privacy & security → Microphone* dan pastikan aplikasi desktop diizinkan. Klik **Refresh**. |
| Level suara terlalu pelan | Aplikasi **tidak pernah mengubah pengaturan perangkat audio Anda** — itu disengaja. Naikkan level di *Settings → System → Sound*, lalu tes ulang. |
| Transkripsi terasa sangat lama | Wajar: sekitar tiga perempat durasi rapat. Untuk mempercepat, buka `app\config\local.toml`, ubah `pass2_budget_ratio` dari `1.0` menjadi `0.25`, simpan, buka ulang aplikasi. Lebih cepat kira-kira dua kali lipat, dengan pilihan kata yang lebih kasar. |
| Pembuatan notulen gagal/laptop berat | Model notulen butuh sekitar 5,1 GB memori. Tutup aplikasi lain, terutama browser. |
| "MODEL_UNAVAILABLE" | Model tidak tersalin sempurna. Jalankan `3-PERIKSA.bat`; kalau model dilaporkan bermasalah, ulangi `1-PASANG.bat`. |
| Ingin memastikan semuanya sehat | Buka PowerShell di folder ini, jalankan `powershell -ExecutionPolicy Bypass -File .\scripts\periksa.ps1 -Lengkap`. Menjalankan seluruh uji otomatis, sekitar 10 menit. |

---

## H. Isi paket ini

| Bagian | Isi |
|---|---|
| `app\` | Kode aplikasi, 233 berkas. |
| `vendor\wheels\` | Seluruh dependensi Python, sudah dalam bentuk siap pasang. Inilah yang membuat pemasangan tidak butuh internet. |
| `vendor\python-3.12.10-amd64.exe` | Pemasang resmi dari python.org, kalau Python belum ada. |
| `bahan\models\` | Model AI, 4,3 GB: dua model pengubah suara ke teks dan satu model penyusun notulen. |
| `bahan\branding\` | Logo kop dokumen. |
| `bahan\participants.local.toml` | Daftar peserta. Hanya ada kalau paket ini dibangun dengan menyertakannya. |
| `bahan\local.toml.templat` | Templat konfigurasi. |
| `scripts\` | Isi dari ketiga berkas `.bat`. Boleh dibaca. |

**Yang sengaja tidak ada di paket ini:** rekaman rapat, transkrip, notulen, basis data,
dan kunci apa pun. Paket ini berisi aplikasi dan bahannya saja, tidak berisi data rapat
siapa pun.

> **Periksa ini sebelum membagikan berkas ZIP-nya.**
>
> Kalau `bahan\participants.local.toml` ada di dalam paket, isinya **nama-nama rekan
> kerja sungguhan** — disertakan supaya tim tidak perlu mengetik ulang daftar yang sama
> di setiap laptop. Itu data pribadi, meskipun ringan.
>
> Kalau berkas itu ada, bagikan ZIP ini **di dalam perusahaan saja**. Untuk memberikannya
> ke pihak luar, hapus dulu berkas tersebut — pemasangan tetap berjalan normal tanpanya,
> hanya direktori pesertanya kosong dan diisi lewat aplikasi.

---

## Ringkasan satu layar

```
PERTAMA KALI      1-PASANG.bat      -> jawab dua pertanyaan, tunggu, cari "FAIL: 0"
SETIAP HARI       2-JALANKAN.bat    -> Langkah 1 rekam, 2 peserta, 3 transkrip, 4 notulen
KALAU BERMASALAH  3-PERIKSA.bat     -> tidak mengubah apa pun

HASILNYA          <folder data>\exports\      contoh: C:\MoM-IGD-Data\exports\
YANG DICADANGKAN  <folder data>\db\
```

Notulen yang keluar adalah **draf**. Selalu dibaca dan dikoreksi manusia sebelum dikirim.
