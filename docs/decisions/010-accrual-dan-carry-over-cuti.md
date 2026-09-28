# ADR 010: Accrual saldo cuti, carry-over, dan hangus

- Status: diterima
- Tanggal: 2026-09-28

## Konteks

ADR 008 menyimpan saldo per karyawan/tipe/tahun. Jatah awal dihitung saat baris saldo pertama
dibuat. Yang belum ada (SPEC: accrual dan reset saldo harian dan awal tahun, hangus carry-over
harian):

- kenaikan jatah di tengah tahun;
- sisa saldo tahun lalu;
- carry-over yang kedaluwarsa.

Acuan umum: UU 13/2003 pasal 79 memberi cuti tahunan minimal 12 hari setelah 12 bulan kerja terus
menerus. Aturan detail (grade, carry-over) diatur policy per tenant.

## Keputusan

1. **Job `leave_accrual`, harian 01:00 waktu tenant.** Untuk tanggal `as_of`, tiap karyawan aktif
   dan tiap tipe cuti bersaldo diproses:
   - saldo tahun berjalan dibuat kalau belum ada, dengan jatah per 1 Januari (atau tanggal
     masuk);
   - jatah dinaikkan kalau policy per `as_of` lebih besar (ulang tahun kerja, promosi, atau policy
     dinaikkan HR);
   - jatah tidak pernah diturunkan otomatis. Pengurangan lewat koreksi saldo HR yang tercatat di
     audit.
2. **Jatah penuh saat syarat terpenuhi, tanpa prorata.** Karyawan yang genap 12 bulan di
   pertengahan tahun langsung mendapat jatah penuh. Prorata bisa jadi opsi policy nanti.
3. **Carry-over diterapkan sekali per tahun** (ditandai `carry_over_applied_at`):
   - jumlahnya `min(sisa saldo tahun lalu, max_carry_over_days)`, dengan policy per 1 Januari;
   - tanggal hangus `1 Januari + carry_over_expiry_months`, dan 0 bulan berarti tidak hangus;
   - kalau saldo tahun lalu tidak ada di sistem, tidak ada carry-over. Saldo awal dari sistem lama
     dimasukkan lewat koreksi saldo, bukan ditebak.
4. **Job `leave_carry_over_expiry`, harian 01:30.** Carry-over dianggap terpakai lebih dulu.
   Yang hangus adalah `max(0, carried_over - used - pending)`, dicatat di bucket baru `expired`
   (ditandai `carry_over_expired_at`). Hari yang sedang diajukan ikut melindungi carry-over.
5. **Idempotent.** Menjalankan ulang untuk tanggal yang sama tidak mengubah apa-apa, dan setiap
   perubahan saldo tercatat di `audit_log` beserta `job_run_id`.
6. **Query per chunk, bukan per karyawan.**
   - Karyawan dan jabatan per dua tanggal (as_of dan acuan awal tahun) diambil lewat dua LATERAL
     dalam satu query.
   - Saldo tahun ini (`FOR UPDATE`) dan tahun lalu masing-masing satu query.
   - Policy dimuat sekali. Pemilihan policy memakai fungsi murni yang sama dengan API
     (`rules.pick_policy`).

Hasil uji di 7.000 karyawan (14 chunk, dijalankan berurutan):

| Job | Total | Per chunk |
| --- | --- | --- |
| Accrual (termasuk 2.595 carry-over) | 5,8 detik | ±0,4 detik |
| Accrual ulang (tanpa perubahan) | 1,2 detik | - |
| Hangus carry-over | 1,6 detik | - |

Query per chunk ≤ 5 ms.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Top-up harian, tidak pernah turun (dipilih) | Sederhana, aman, idempotent | Penurunan jatah harus manual |
| Hitung ulang jatah penuh tiap hari | Selalu sesuai policy terbaru | Bisa menurunkan saldo diam-diam, membingungkan karyawan |
| Carry-over dari saldo terhitung (tanpa baris tahun lalu) | Jalan untuk data lama | Menebak pemakaian yang tidak tercatat |
| Hangus dikurangkan langsung dari `carried_over` | Tanpa kolom baru | Riwayat berapa yang hangus hilang |

## Konsekuensi

- Perubahan saldo tahun lalu setelah carry-over diterapkan (misal cuti Desember dibatalkan di
  Januari) tidak ikut terbawa. HR mengoreksi manual.
- Lazy init saldo lewat API tetap memakai jatah awal tahun. Kenaikan di hari ulang tahun kerja
  baru terlihat setelah job malam itu.
- Prorata dan accrual bulanan (jatah bertambah per bulan) belum didukung. Kalau dibutuhkan,
  menjadi field policy baru dan job yang sama.
