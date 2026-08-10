"""The prompts. Indonesian, because the meeting is.

Instructions are written in the language of the transcript deliberately. A model asked in
English to extract from Indonesian spends part of its answer translating, and a 4B model
translating is a 4B model paraphrasing -- which is precisely the operation the quote field
exists to prevent.

Three rules are repeated in every prompt because each of them is a failure mode observed
in practice with small models, not a hypothetical:

1. **Do not invent a person.** A model that has learned the shape of a minute knows an
   action item usually has an owner, and will supply a plausible name to complete the
   pattern. In a minute that assigns responsibility, an invented owner is the single most
   damaging output the system can produce -- more damaging than omitting the item, because
   omission is visible to whoever was in the room and a wrong name is not.
2. **Quote verbatim.** The quote is the only field a machine can check. A tidied quote is
   an unverifiable quote, and the verifier will mark the item ``UNVERIFIED`` -- so tidying
   loses the item rather than improving it.
3. **The transcript is machine-generated.** It contains misheard words. The model must not
   "correct" what it thinks it sees into something more sensible, because a plausible
   correction of a misheard number is a wrong number stated confidently.

**The worked example is a format demonstration, not a source of content.** A small model
follows a shown shape far more reliably than a described one -- the classification errors
this example fixes were observed, not anticipated: an agenda listing extracted as a
decision, and a schedule change filed as an action item because it was phrased actively.
The example uses invented names and dates that appear nowhere in any real transcript, so
if the model ever copies from it rather than from the meeting, the verifier will fail to
locate the quote and the item is marked ``UNVERIFIED`` rather than believed.

The prompt never contains a participant roster. Supplying one would hand the model a list
of names to attach to unattributed statements, which is exactly the mistake rule 1
forbids; the roster is matched against extracted owners **afterwards**, by
:mod:`mom_igd.mom.verify`, where a name either matches text in the transcript or does not.
"""

from __future__ import annotations

from typing import Final, Sequence

from mom_igd.mom.chunking import TranscriptChunk
from mom_igd.mom.schema import MinuteItem

__all__ = [
    "EXTRACTION_SYSTEM",
    "SUMMARY_SYSTEM",
    "build_extraction_user",
    "build_summary_user",
]


EXTRACTION_SYSTEM: Final[str] = """\
Anda adalah notulis rapat profesional. Tugas Anda hanya satu: membaca potongan transkrip \
rapat dan mendaftar hal-hal penting yang BENAR-BENAR diucapkan di dalamnya.

ATURAN WAJIB:
1. Jangan menambahkan informasi apa pun yang tidak ada di transkrip. Jangan menyimpulkan, \
jangan menebak, jangan melengkapi.
2. Jangan pernah menuliskan nama orang sebagai penanggung jawab kecuali nama itu benar-benar \
disebut di transkrip. Jika tidak ada nama yang disebut, isi "owner" dengan null.
3. Jangan pernah menuliskan tenggat waktu kecuali disebut di transkrip. Jika tidak ada, isi \
"due" dengan null.
4. Field "quote" harus disalin PERSIS kata demi kata dari transkrip, termasuk jika kalimatnya \
terdengar aneh. Jangan merapikan, jangan menerjemahkan, jangan meringkas di dalam "quote".
5. Field "segments" berisi nomor penanda [S...] tempat kutipan itu berada. Hanya gunakan \
nomor yang muncul di potongan transkrip yang diberikan.
6. Transkrip ini dibuat otomatis oleh mesin dan mengandung salah dengar. Jangan "memperbaiki" \
angka, nama, atau istilah menjadi sesuatu yang terdengar lebih masuk akal.
7. Jika potongan transkrip tidak berisi hal penting apa pun, kembalikan daftar kosong.

JENIS ITEM (pilih yang paling tepat, satu saja):
- "DECISION": rapat MEMUTUSKAN sesuatu. Ada kesepakatan atau penetapan. Ciri: "kita \
sepakat", "kita putuskan", "diputuskan", "kita batalkan", "jadinya", "kita pakai yang".
- "ACTION": ada ORANG atau TIM yang harus MENGERJAKAN sesuatu setelah rapat. Ciri: "tolong", \
"mohon", "tugasnya", "siapkan", "kirimkan", "follow up", "cek dulu".
- "DISCUSSION": informasi atau pokok bahasan penting, tanpa keputusan dan tanpa tugas.
- "ISSUE": masalah, risiko, kendala, atau pertanyaan yang belum terjawab.

Bedakan DECISION dan ACTION dengan hati-hati. "Jadwal mundur ke Oktober" adalah DECISION, \
bukan ACTION, karena tidak ada orang yang mengerjakan apa pun. "Andi memperbaiki modul \
pajak" adalah ACTION.

JANGAN DICATAT SEBAGAI ITEM:
- Basa-basi, sapaan, permintaan maaf terlambat, obrolan di luar topik.
- Pembukaan dan penutupan rapat.
- Pembacaan daftar agenda ("agenda hari ini ada tiga ...").
- Pertanyaan biasa yang langsung dijawab di kalimat berikutnya.
- Pengulangan kalimat yang sudah Anda catat sebagai item lain di potongan ini.

Field "text" adalah kalimat rapi dalam bahasa Indonesia baku yang menjelaskan item tersebut, \
maksimal dua kalimat. Isinya tidak boleh melebihi apa yang ada di "quote" dan di segmen yang \
dikutip.

CONTOH. Untuk potongan transkrip:
  [S4] (00:01:20) Kalau begitu jadwal go-live kita mundurkan ke tanggal 5 September.
  [S5] (00:01:31) Setuju, dicatat ya.
  [S6] (00:01:42) Bu Sinta, tolong siapkan dokumen requirement sebelum hari Jumat.
  [S7] (00:01:55) Selamat pagi juga, maaf saya baru gabung.
jawaban yang benar:
{"items":[\
{"kind":"DECISION","text":"Jadwal go-live dimundurkan ke tanggal 5 September.",\
"quote":"jadwal go-live kita mundurkan ke tanggal 5 September","segments":[4],\
"owner":null,"due":null},\
{"kind":"ACTION","text":"Menyiapkan dokumen requirement.",\
"quote":"Bu Sinta, tolong siapkan dokumen requirement sebelum hari Jumat","segments":[6],\
"owner":"Bu Sinta","due":"hari Jumat"}]}
Perhatikan: [S5] dan [S7] tidak menjadi item.

Jawab hanya dengan JSON sesuai format yang diminta."""


SUMMARY_SYSTEM: Final[str] = """\
Anda adalah notulis rapat profesional. Anda diberi daftar poin rapat yang SUDAH \
diverifikasi kebenarannya terhadap rekaman.

Tugas Anda: menuliskan judul rapat dan ringkasan eksekutif.

ATURAN WAJIB:
1. Ringkasan hanya boleh menggunakan informasi dari daftar poin yang diberikan. Dilarang \
menambah fakta, angka, nama, tanggal, atau kesimpulan baru.
2. Jangan menyebut angka yang tidak ada di daftar poin.
3. Jangan menyebut nama orang yang tidak ada di daftar poin.
4. Judul maksimal 12 kata, menggambarkan topik utama rapat, tanpa tanggal.
5. Ringkasan terdiri dari 3 sampai 6 kalimat, masing-masing satu baris, bahasa Indonesia baku.
6. Jika daftar poin sangat sedikit, tulis ringkasan yang pendek. Jangan mengarang untuk \
memenuhi jumlah kalimat.

Jawab hanya dengan JSON sesuai format yang diminta."""


def build_extraction_user(
    chunk: TranscriptChunk, *, chunk_count: int, meeting_title: str | None = None
) -> str:
    """The per-window extraction prompt.

    The window's position is stated ("bagian 2 dari 4") for one reason: without it, a
    model handed the middle of a meeting tends to write an opening or a conclusion, having
    inferred it is looking at a whole one.
    """
    header = [
        f"Potongan transkrip bagian {chunk.position + 1} dari {chunk_count}.",
    ]
    if meeting_title:
        header.append(f"Rapat: {meeting_title}")
    header.append(
        "Setiap baris diawali penanda segmen [S...] dan waktu. Gunakan penanda itu "
        'sebagai isi field "segments".'
    )
    if chunk.position > 0:
        header.append(
            "Bagian ini melanjutkan bagian sebelumnya. Jangan menulis pembukaan rapat "
            "kecuali memang ada di teks di bawah."
        )
    return "\n".join(header) + "\n\n--- TRANSKRIP ---\n" + chunk.body + "\n--- SELESAI ---"


def build_summary_user(
    items: Sequence[MinuteItem], *, meeting_title: str | None = None
) -> str:
    """The reduce prompt: built from verified items, never from the transcript.

    Only ``text`` is passed, not the quotes. The quotes are raw speech and would invite
    the model to write the summary from them -- reintroducing the unverified paraphrasing
    that extracting them separately was meant to remove.
    """
    from mom_igd.mom.schema import kind_label

    lines: list[str] = []
    if meeting_title:
        lines.append(f"Nama rapat menurut sistem: {meeting_title}")
        lines.append("")
    lines.append("--- POIN TERVERIFIKASI ---")
    for number, item in enumerate(items, start=1):
        suffix = f" (PIC: {item.owner})" if item.owner else ""
        if item.due:
            suffix += f" (tenggat: {item.due})"
        lines.append(f"{number}. [{kind_label(item.kind)}] {item.text}{suffix}")
    if not items:
        lines.append("(tidak ada poin yang terverifikasi)")
    lines.append("--- SELESAI ---")
    return "\n".join(lines)
