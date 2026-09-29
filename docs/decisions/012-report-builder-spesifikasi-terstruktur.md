# ADR 012: Report builder memakai spesifikasi query terstruktur di atas semantic views

- Status: diterima
- Tanggal: 2026-09-29

## Konteks

SPEC mewajibkan report builder manual (pilih kolom, filter, export, tanpa AI) untuk semua tenant.
Nanti Reporting Agent membuat laporan dari bahasa biasa. SPEC menyebut:

- semantic views `v_employee`, `v_org_unit`, `v_leave_balance`, `v_leave_request`, dengan
  deskripsi bisnis per kolom;
- SQL Guard (hanya SELECT, view whitelist, limit, timeout);
- `report_template` menyimpan SQL.

Risikonya: SQL mentah dari client (atau dari LLM) sulit dijaga aman dan sulit diaudit, walau
sudah ada SQL Guard.

## Keputusan

1. **Semantic views sebagai satu-satunya sumber data laporan.** Dibuat di migrasi dengan
   `WITH (security_invoker = true)`, supaya RLS tabel asal tetap berlaku untuk role aplikasi.
   Tanpa opsi ini, view berjalan dengan hak owner dan melewati RLS. Kolom sensitif (NIK KTP,
   gaji, rekening) tidak ada di view.
2. **Client mengirim spesifikasi terstruktur, bukan SQL:** `dataset`, `columns`, `filters`
   (operator per tipe kolom), `aggregates` (count/sum/avg/min/max, dikelompokkan per `columns`),
   `sort`, dan `limit`.
   - Backend memetakan spesifikasi ke SQLAlchemy Core berdasarkan katalog
     (`app/reports/catalog.py`).
   - Nama kolom, operator, dan fungsi hanya boleh dari katalog; nilai selalu jadi bind parameter.
   - Katalog juga menyimpan label dan deskripsi bisnis tiap kolom sebagai data dictionary.
3. **Batas eksekusi:** filter `tenant_id` eksplisit di setiap query (selain RLS),
   `statement_timeout` 10 detik per query laporan, preview maksimal 500 baris, export sinkron
   maksimal 10.000 baris. Laporan lebih besar menunggu job + object storage.
4. **Template menyimpan spesifikasi**, bukan SQL (`report_template.query` JSONB), dan divalidasi
   ulang setiap dijalankan. Katalog bisa berubah, jadi template lama yang tidak valid ditolak
   dengan pesan jelas.
5. **Export CSV dan XLSX** dibuat di memori, lalu di-stream. Sel teks yang diawali `= + - @`
   diberi awalan `'` supaya tidak dieksekusi sebagai formula (CSV/formula injection). Setiap
   export dicatat di `audit_log` (dataset, kolom, filter, jumlah baris), karena export data
   karyawan relevan untuk UU PDP.
6. **Hanya HR** (sesuai tabel role di SPEC).
7. **Arah untuk Reporting Agent:** LLM menghasilkan spesifikasi yang sama, bukan SQL bebas.
   Pengaman yang sama berlaku otomatis, template dari chat dan dari form identik, dan
   "definisi yang dipakai" bisa ditampilkan dari spesifikasi. SQL Guard untuk SQL bebas hanya
   dibuat kalau spesifikasi ternyata tidak cukup.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Spesifikasi terstruktur (dipilih) | Tidak ada SQL dari luar, mudah divalidasi dan dijelaskan, sama untuk form dan AI | Laporan yang sangat kompleks (join bebas, window) belum bisa |
| SQL mentah + SQL Guard | Paling fleksibel | Parser/guard rawan celah, sulit dijelaskan ke user, sulit diaudit |
| View tanpa `security_invoker` | Sederhana | Melewati RLS karena view berjalan sebagai owner |
| pandas untuk export | Banyak fitur | Dependency berat; openpyxl + csv sudah cukup |

## Konsekuensi

- Dataset atau kolom baru = ubah view di migrasi baru + tambah entri katalog. Test memastikan
  semua kolom katalog ada di view.
- Laporan historis yang butuh jabatan per tanggal tertentu belum didukung; view memakai jabatan
  yang berlaku hari ini (zona waktu tenant).
- Read replica untuk laporan belum ada; sementara query ke primary dengan timeout ketat.
- Parameter template (misal "tahun berjalan") belum ada; filter template berisi nilai tetap.
