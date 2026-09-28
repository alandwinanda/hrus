# mcp-server

Belum di-scaffold. Dijadwalkan di fase AI Assistant (minggu 7–9, lihat `docs/SPEC.md`).

Isi nanti: MCP tools, masing-masing pembungkus tipis satu endpoint Core API, difilter per role,
dan selalu memakai token JWT user.

## Daftar tool

Spec tool yang sudah ditentukan per modul (diisi lewat skill `/new-module`). Acuan awal: tabel
"API endpoint dan MCP tools" di `docs/SPEC.md`.

| Tool | Endpoint | Role | Konfirmasi user |
| --- | --- | --- | --- |
| `get_my_profile` | `GET /me` | Semua | Tidak |
| `list_employees` | `GET /employees` | HR | Tidak |

Endpoint Core HR lain (`/org-units`, `POST/PATCH /employees`, `/employees/{id}/jobs`) sengaja
belum dijadikan MCP tool di MVP: perubahan data master tetap lewat form HR.
