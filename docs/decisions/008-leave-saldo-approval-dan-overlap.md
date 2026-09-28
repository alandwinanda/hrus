# ADR 008: Leave, saldo tersimpan, approval berjenjang, dan pencegahan overlap

- Status: diterima
- Tanggal: 2026-09-28

## Konteks

Leave Management adalah modul MVP yang paling sering dipakai: dari form, dari chat AI, dan oleh
atasan untuk approval (SPEC: Leave Management, AI Assistant, validasi dua lapis). Batasan:

- Saldo harus selalu benar walau ada pengajuan bersamaan, retry dari chat, dan approval paralel.
- Angka bisnis (jumlah hari, saldo) tidak boleh dihitung LLM, jadi API harus mengembalikannya.
- Form dan chat wajib memakai validasi yang sama (hard rules blokir, soft rules hanya warning).
- Skala: 7.000 karyawan, sekitar 70 ribu pengajuan per tahun per tenant, query tetap cepat
  setelah bertahun-tahun (SPEC: performa query jangka panjang).

## Keputusan

1. **Tabel sesuai SPEC:** `leave_type`, `leave_policy`, `holiday_calendar`, `leave_balance`,
   `leave_request`, `leave_approval`. Tipe cuti tidak dihapus, cukup dinonaktifkan. Policy dipilih
   yang paling spesifik: grade yang cocok dulu, lalu masa kerja minimum tertinggi.
2. **Saldo disimpan per karyawan/tipe/tahun** dengan bucket `entitled`, `carried_over`,
   `adjusted`, `used`, `pending`. `available = entitled + carried_over + adjusted - used - pending`.
   Baris dibuat saat pertama dibutuhkan (jatah dari policy per 1 Januari, atau per tanggal masuk).
   Submit, approve, reject, dan cancel mengubah saldo di transaksi yang sama dengan `FOR UPDATE`.
   Koreksi manual HR lewat `POST /leave/balances/adjustments`, selalu tercatat di `audit_log`.
3. **Jumlah hari = hari kerja (Senin-Jumat) dikurangi `holiday_calendar`**, dihitung di
   `app/rules/leave.py`. Satu pengajuan tidak boleh lintas tahun (saldo per tahun) dan maksimal
   184 hari kalender (cukup untuk cuti melahirkan sesuai UU KIA). Batas rentang juga dijaga check
   constraint karena query irisan tanggal bergantung padanya.
4. **Satu evaluasi untuk validate dan submit.** `POST /leave/requests/validate` (dry-run)
   mengembalikan semua error, warning soft (rekan satu atasan yang cuti di tanggal sama), dan
   preview saldo. Submit memakai evaluasi yang sama dan menolak di error pertama (422 + `code`).
5. **Overlap dicegah di database** dengan exclusion constraint
   `EXCLUDE USING gist (tenant_id =, employee_id =, daterange(start_date, end_date, '[]') &&)
   WHERE status IN ('pending', 'approved')`. Dua submit bersamaan yang lolos validasi: yang kedua
   ditolak database dan dijawab 409 `overlapping_request`.
6. **Idempotency-Key** (header, unik per karyawan): submit ulang dengan key yang sama
   mengembalikan pengajuan yang sudah ada. Wajib dipakai orchestrator untuk aksi tulis via chat.
7. **Approval 1-2 level, rantai disnapshot saat submit:** level 1 atasan langsung, level 2 atasan
   dari atasan. Level tanpa atasan (`approver_employee_id` NULL) masuk antrean HR. HR boleh
   memutuskan level mana pun (dicatat `as_hr_override`), tapi tidak boleh memutuskan pengajuan
   sendiri. Reject atau cancel menandai level sisanya `skipped`.
8. **Cancel:** pengajuan pending kapan saja, approved hanya sebelum tanggal mulai. Saldo
   `pending` atau `used` dikembalikan.
9. **Visibilitas:** pemohon, approver pengajuan itu, dan HR. Selain itu 404. Kalau user bisa
   melihat tapi tidak berhak bertindak, jawabannya 403 dengan `code` yang jelas
   (`not_current_approver`, `cannot_cancel_others`, `cannot_decide_own_request`).
10. **Query irisan tanggal memakai perbandingan tanggal biasa** (`start_date <= end`,
    `end_date >= start`, plus batas bawah `start_date > start - 184 hari`) dan btree
    `(tenant_id, start_date, end_date, id)`. Operator `&&` tidak leakproof, jadi di bawah RLS
    tidak bisa dipakai sebagai index condition dan planner jatuh ke seq scan.

Hasil `EXPLAIN ANALYZE` di seed 7.000 karyawan, 120 ribu pengajuan (3 tahun), sebagai role
aplikasi dengan RLS: saldo < 0,1 ms, validate < 3 ms, daftar milik sendiri 0,1 ms, inbox
approval < 1 ms, daftar semua (HR) per tahun 0,4 ms, kalender HR 2 ms (sebelumnya 119 ms dengan
GiST + sort), kalender atasan 3 ms, kalender per unit ~10 ms.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Saldo disimpan + bucket (dipilih) | Baca O(1), jelas asal angkanya, cocok untuk accrual/carry-over | Wajib update di transaksi yang sama, butuh lock |
| Saldo dihitung dari histori tiap dibaca | Tidak ada state yang bisa melenceng | Makin lambat tiap tahun, dilarang SPEC |
| Cek overlap hanya di aplikasi | Skema sederhana | Race dua request bersamaan lolos |
| Rantai approval dihitung saat keputusan | Ikut perubahan atasan terbaru | Approver bisa berubah di tengah jalan, sulit diaudit |
| Index GiST untuk kalender | Cocok untuk range query | Tidak terpakai di bawah RLS (operator tidak leakproof) |

## Konsekuensi

- Accrual ulang tahun kerja, reset awal tahun, dan carry-over (plus hangus) dikerjakan job Celery
  di Leave bagian 2. Perubahan policy hanya berlaku untuk saldo yang dibuat setelahnya.
- Kalau atasan berganti saat pengajuan masih pending, approver lama tetap tercatat. Untuk
  sementara HR yang memutuskan (override). Fitur reassign approver bisa ditambahkan nanti.
- Cuti yang melewati pergantian tahun (misal melahirkan Desember-Maret) harus dipecah dua.
- Partisi tahunan `leave_request` (SPEC: archiving) perlu meninjau ulang exclusion constraint,
  karena constraint di tabel berpartisi wajib memuat kolom kunci partisi.
- Setiap query irisan tanggal baru wajib memakai helper `_overlaps()` yang sama.
