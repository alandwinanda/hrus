# ADR 006: Auth JWT sendiri, tenant dari token, dan role DB non-superuser untuk RLS

- Status: diterima
- Tanggal: 2026-09-25

## Konteks

Semua modul butuh user login, role, dan tenant context. AI Orchestrator dan MCP server nanti
meneruskan token user (on-behalf-of), dan otorisasi selalu diputuskan Core API. Isolasi tenant
dijaga dua lapis: filter di query dan Row Level Security PostgreSQL.

Masalah di scaffold awal: aplikasi konek sebagai superuser, dan superuser selalu melewati RLS.

## Keputusan

1. **Auth dibuat sendiri di Core API** untuk MVP. Login memakai `tenant_slug` + email + password.
   Email unik per tenant, bukan global. Nanti slug bisa diambil dari subdomain (`acme.hrus.id`).
2. **Password argon2id.** Login gagal dibatasi per tenant+email di Redis (default 5x per 15 menit).
   Semua jenis kegagalan memberi respons yang sama, supaya email terdaftar tidak bisa ditebak.
3. **Access token JWT HS256, 15 menit.** Claim: `sub`, `tid` (tenant), `roles`, `eid` (employee),
   `type=access`, `iss`. `tenant_id` hanya diambil dari token, tidak pernah dari header atau body.
4. **Refresh token opaque, 7 hari**, di cookie httpOnly + SameSite=Strict (path `/api/auth`).
   Format `<tenant_id>.<acak>`, yang disimpan hanya hash SHA-256. Setiap refresh merotasi token.
   Kalau token lama dipakai ulang, semua refresh token user itu dicabut (deteksi pencurian).
5. **Role DB:**
   - Owner/superuser hanya untuk migrasi dan CLI admin (`MIGRATION_DATABASE_URL`).
   - Role grup `hrus_app` (NOLOGIN, tanpa BYPASSRLS) dibuat migrasi dan menerima GRANT per tabel.
   - User login aplikasi (`APP_DB_USER`) anggota `hrus_app`, dibuat `python -m app.cli create-db-user`.
     Backend dan worker selalu memakai user ini lewat PgBouncer.
6. **RLS fail closed:** policy `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`
   + `FORCE ROW LEVEL SECURITY`. Tanpa tenant context hasilnya 0 baris.
7. **`audit_log` append-only:** role aplikasi hanya SELECT dan INSERT.
8. **Tabel `tenant` tanpa RLS**, role aplikasi hanya SELECT (dibutuhkan saat login sebelum tenant
   context ada). Isinya hanya slug, nama, timezone.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Auth sendiri (dipilih) | Tanpa service tambahan, sederhana untuk MVP dan dedicated | SSO/MFA harus dibangun sendiri nanti |
| Keycloak / Zitadel (OIDC) | SSO, MFA, UI admin siap pakai | Service berat tambahan, integrasi lebih lama |
| Access token panjang tanpa refresh | Paling sederhana | Token bocor berlaku lama, tidak bisa logout sungguhan |
| JWT RS256/EdDSA | Service lain bisa verifikasi dengan public key | Manajemen key lebih rumit, belum dibutuhkan (hanya Core API yang verifikasi) |

## Konsekuensi

- Semua endpoint bisnis memakai `TenantSessionDep`: satu transaksi per request, tenant context dari token.
- Koneksi owner (CLI admin, test factory) melewati RLS, jadi query di sana wajib filter `tenant_id`
  eksplisit. Bug seperti ini sempat tertangkap test `seed-dev`.
- Access token tetap berlaku sampai kedaluwarsa setelah logout (maksimal 15 menit).
- Cross-tenant job (archiving, maintenance) nanti butuh role terpisah dengan BYPASSRLS, lewat ADR baru.
- Pindah ke OIDC/SSO nanti cukup mengganti penerbit token, selama claim `tid` dan `roles` tetap ada.
- Ditinjau ulang kalau ada klien yang mewajibkan SSO atau MFA.
