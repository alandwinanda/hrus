# ADR 004: AI sebagai add-on opsional lewat AI Gateway, default DeepSeek

- Status: diterima
- Tanggal: 2026-09-25

## Konteks

Produk dijual sebagai ERP HRIS dengan paket AI opsional (Core, AI Mini, AI Pro, AI Enterprise).
Pembeli mulai menolak vendor yang memaksakan AI ke paket lalu menaikkan harga, jadi AI harus
benar-benar opsional. Harga dan skema provider LLM juga berubah-ubah (DeepSeek sudah dua kali
mengubah skema harga di 2026).

Kebijakan privasi DeepSeek menyatakan data disimpan di server di Republik Rakyat Tiongkok, sementara
data karyawan tunduk pada UU No. 27 Tahun 2022 tentang Pelindungan Data Pribadi (SPEC, "Paket AI
dan monetisasi").

## Keputusan

1. **AI opsional.** Semua fitur ERP berjalan penuh dengan `AI_ENABLED=false`. Kalau paket AI tidak
   aktif, kuota habis, atau provider gagal, aplikasi kembali ke mode ERP (hard validation tetap jalan).
2. **Satu pintu ke LLM.** Semua panggilan LLM wajib lewat service `ai-gateway`. Service lain
   dilarang memanggil provider atau import SDK provider langsung.
3. **Default DeepSeek (`deepseek-flash`)** lewat endpoint format OpenAI. Provider dan model diatur
   lewat env, provider cadangan dan LLM privat (Enterprise/dedicated) ditambah di gateway tanpa
   mengubah kode fitur.
4. **Entitlement di backend.** Fitur AI dicek `require_feature()` di Core API, di daftar MCP
   tools, dan di UI. Menyembunyikan tombol saja tidak cukup.
5. **Sistem kredit.** Harga paket dipisah dari harga token. Setiap panggilan dicatat di `ai_usage`
   (tenant, fitur, model, token input/cache/output, jam peak atau tidak).
6. **Data minimal.** NIK, gaji, dan rekening tidak pernah dikirim ke LLM. Nama karyawan diganti
   token sebelum dikirim. Aktivasi AI butuh opt-in tertulis dari admin tenant.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Gateway sendiri, default DeepSeek | Biaya token rendah, bisa ganti provider tanpa ubah fitur, metering terpusat | Isu lokasi data harus dimitigasi dan transparan ke klien |
| Provider premium langsung di tiap service | Cepat dibuat | Terkunci ke satu vendor, metering dan masking tersebar, margin paket tergerus |
| LiteLLM sebagai gateway | Banyak adapter siap pakai | Tetap butuh lapisan entitlement, kredit, dan masking sendiri |
| AI wajib di semua paket | Pendapatan per user lebih tinggi | Bertentangan dengan posisi jual "AI opsional", ditolak klien sensitif data |

## Konsekuensi

- Test suite backend wajib lulus dengan `AI_ENABLED=false`.
- Gateway jadi komponen kritis untuk fitur AI: butuh circuit breaker, fallback provider, dan
  rate limit per tenant.
- Prompt dibuat cache-friendly (system prompt dan definisi tool stabil di awal) karena chat
  interaktif hampir selalu kena tarif jam peak.
- Kualitas tool calling `deepseek-flash` dalam Bahasa Indonesia wajib diuji dengan eval set
  sebelum go-live.
- Klien pemerintah/BUMN dan klien yang menolak transfer data ke luar negeri diarahkan ke
  LLM privat (Enterprise) atau tanpa paket AI. Klausul pemrosesan data perlu dicek legal.
