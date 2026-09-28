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
| `get_leave_balance` | `GET /leave/balances` | Semua | Tidak |
| `list_leave_requests` | `GET /leave/requests` | Semua | Tidak |
| `validate_leave_request` | `POST /leave/requests/validate` | Semua | Tidak (dry-run) |
| `submit_leave_request` | `POST /leave/requests` + header `Idempotency-Key` | Semua | Ya |
| `cancel_leave_request` | `POST /leave/requests/{id}/cancel` | Semua | Ya |
| `get_team_calendar` | `GET /leave/team-calendar` | Atasan, HR | Tidak |
| `decide_leave_request` | `POST /leave/requests/{id}/decision` | Atasan, HR | Ya |
| `run_job` | `POST /jobs/runs` + header `Idempotency-Key` | HR | Ya (tawarkan dry-run dulu) |
| `get_job_status` | `GET /jobs/runs/{id}` | HR | Tidak |

Endpoint Core HR lain (`/org-units`, `POST/PATCH /employees`, `/employees/{id}/jobs`) sengaja
belum dijadikan MCP tool di MVP: perubahan data master tetap lewat form HR. Sama untuk konfigurasi
cuti (`/leave/types`, `/leave/policies`, `/leave/holidays`) dan koreksi saldo
(`/leave/balances/adjustments`).

Catatan Leave: jumlah hari dan saldo selalu diambil dari respons API, tidak pernah dihitung LLM.
Sebelum `submit_leave_request`, orchestrator memanggil `validate_leave_request` dan menampilkan
hasilnya (termasuk warning) untuk dikonfirmasi user. Idempotency-Key dibuat sekali per konfirmasi.

Catatan job: untuk accrual cuti, orchestrator menjalankan `run_job` dengan `dry_run=true`,
merangkum `output.counts` dari `get_job_status`, lalu minta konfirmasi user sebelum run sungguhan.
