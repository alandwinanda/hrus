# ADR 011: AI memakai API key milik tenant (BYOK), tanpa paket dan kredit

- Status: diterima
- Tanggal: 2026-09-28
- Mengganti sebagian ADR 004: poin 3 (provider dan key dari env) dan poin 5 (sistem kredit).

## Konteks

SPEC awal menjual AI sebagai paket (AI Mini, Pro, Enterprise) dengan kuota kredit. Artinya kita
yang membayar token LLM, lalu menagih lewat kredit, top-up, dan billing.

Infrastruktur untuk itu belum ada. Selama harga token berubah-ubah, biaya bisa lebih besar dari
pendapatan. Keputusan pemilik produk (2026-09-28):

- tidak ada paket;
- tenant memasukkan API key LLM sendiri;
- fitur AI cukup diaktifkan atau dinonaktifkan lewat setting.

## Keputusan

1. **Tanpa paket, kredit, top-up, dan billing.** Tabel `plan`, `tenant_subscription`,
   `credit_ledger` dari SPEC tidak dibuat.
2. **Setting AI per tenant (`tenant_ai_setting`), dikelola HR admin:**
   - provider;
   - model;
   - API key (terenkripsi);
   - status tes koneksi;
   - fitur AI yang aktif;
   - limit token bulanan (opsional);
   - persetujuan pemrosesan data.
3. **API key terenkripsi** dengan Fernet memakai `AI_SECRET_KEY` dari env. Bisa berisi beberapa key
   dipisah koma untuk rotasi, dan key pertama dipakai untuk enkripsi. Key tidak pernah dikembalikan
   API, log, atau audit; yang ditampilkan hanya 4 karakter terakhir.
4. **Provider dari daftar tetap** (`deepseek`, `openai`, `openrouter`), semuanya memakai endpoint
   format OpenAI. Provider `custom` hanya boleh memakai URL yang ada di allowlist operator
   (`AI_ALLOWED_BASE_URLS`, misal LLM privat di deployment dedicated). Tenant tidak bisa mengarahkan
   server ke URL sembarang (SSRF). Gateway mengecek allowlist yang sama.
5. **Fitur AI aktif untuk tenant kalau semua syarat terpenuhi:**
   - `AI_ENABLED=true` (kill switch deployment);
   - persetujuan pemrosesan data sudah diberikan;
   - key sudah lolos tes koneksi;
   - fiturnya di-toggle aktif;
   - limit bulanan belum habis.

   Dicek `require_feature()` di Core API. `GET /me` mengembalikan `ai_features` untuk UI dan
   filter MCP tools. Ganti key, provider, atau model berarti key harus dites ulang. Ganti provider
   juga berarti persetujuan harus diberikan ulang, karena data akan dikirim ke pihak lain.
6. **Kredensial dikirim per request ke ai-gateway.** Core API mendekripsi key lalu mengirimnya di
   body request internal. Gateway tetap stateless dan tidak pernah menyimpan atau me-log key.
   - Key dari env gateway (`DEEPSEEK_API_KEY`) hanya dipakai kalau request tidak membawa
     kredensial, yaitu untuk deployment dedicated yang key-nya dikelola operator.
   - Untuk chat AI (fase AI Assistant), orchestrator memanggil endpoint AI di Core API memakai JWT
     user. Core API mengecek fitur dan limit, meneruskan ke gateway, lalu mencatat pemakaian.
7. **Pemakaian tetap dicatat** di `ai_usage` (per panggilan, append-only) dan `ai_usage_monthly`
   (counter per tenant/bulan/fitur, di-update di transaksi yang sama).
   - Limit dihitung dari token input + output per bulan kalender (zona waktu tenant).
   - Kalau limit habis, fitur AI berhenti dan aplikasi kembali ke mode ERP. HR melihat pemakaian
     di `GET /settings/ai/usage`.
8. **Aturan data tidak berubah:** NIK, gaji, dan rekening tidak pernah dikirim ke LLM, nama di-mask,
   dan semua panggilan lewat ai-gateway.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| BYOK + toggle fitur (dipilih) | Tanpa risiko biaya token, tanpa billing, tenant pegang kontrak data dengan provider | Tenant harus punya akun provider, pengalaman awal sedikit lebih rumit |
| Paket + kredit (SPEC awal) | Pendapatan AI, pengalaman paling mudah | Butuh billing dan monitoring biaya, margin rawan saat harga token naik |
| Key dari env saja | Paling sederhana | Semua tenant memakai satu key milik kita, sama dengan menanggung biaya |
| Gateway membaca key dari database | Orchestrator bisa langsung ke gateway | Gateway butuh akses DB dan kunci dekripsi, permukaan serangan bertambah |

## Konsekuensi

- `AI_SECRET_KEY` wajib diisi sebelum tenant bisa menyimpan key. Kalau key ini hilang, semua tenant
  harus memasukkan ulang API key-nya. Rotasi: tambahkan key baru di depan, lalu jalankan re-enkripsi.
- SPEC bagian paket, kredit, dan billing diganti. Opsi "key dikelola kita" bisa ditambah lagi
  nanti sebagai provider lain tanpa mengubah fitur.
- Tenant bertanggung jawab atas kontrak dan lokasi data di provider pilihannya. Persetujuan HR
  admin dicatat beserta waktunya.
- `ai_usage` perlu partisi bulanan setelah volumenya besar (SPEC: performa jangka panjang).
