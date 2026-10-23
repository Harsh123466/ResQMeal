# AnnSetu AI

AnnSetu AI is a local full stack application for connecting surplus food donors with community organizations. The project files are in this folder.

## Run locally

Use Python 3.10+ and Node.js 20.19+ (or 22.12+). Configure the administrator before starting the API. In PowerShell:

```powershell
cd backend
$env:ANNSETU_ADMIN_EMAIL = "admin@example.org"
$env:ANNSETU_ADMIN_PASSWORD = "use-a-unique-password-at-least-10-characters"
# Set and keep a private signing key in deployment; the local development key is generated automatically.
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL. API docs are at `http://localhost:8000/docs`. Set `VITE_API_BASE_URL` in `frontend/.env` if the API uses another URL. By default data is stored in `backend/annsetu.db`; private recipient files are encrypted in the selected database.

## Use Supabase PostgreSQL

The backend can use Supabase PostgreSQL or local SQLite. To connect Supabase:

1. In your Supabase project, choose **Connect** and copy a PostgreSQL connection string. For this backend, choose the shared transaction pooler (port 6543), which is intended for short-lived request connections. If your existing shared pooler URL uses port 5432, the backend switches it to 6543 automatically.
2. Copy `backend/.env.example` to `backend/.env` and set `SUPABASE_DB_URL` to that connection string. Replace its password locally, and keep this file private. The database URI belongs only in the backend, never the frontend.
3. Install backend requirements and restart Uvicorn. The backend creates the required application tables on startup.

If `SUPABASE_DB_URL` is not set, the app continues to use `backend/annsetu.db`. Switching databases does not automatically copy existing SQLite data; existing accounts, donations, and encrypted documents remain in that local file until explicitly migrated. Keep the document encryption key (`backend/.annsetu_docs.key` or `ANNSETU_DOCUMENT_KEY`) backed up with any data containing encrypted documents.

## Backend structure

- `app/main.py` creates the FastAPI app, middleware, startup lifecycle, and mounts routers.
- `app/api/` contains workflow-focused HTTP routes for authentication, recipients and verification, donations and matching, handovers, notifications, and impact.
- `app/database.py` owns SQLite/Supabase PostgreSQL connection handling and schema initialization.
- `app/schemas.py` defines and validates API request payloads.
- `app/dependencies.py` provides authentication, role checks, and document encryption access.
- `app/services/common.py` contains shared workflow operations such as safety eligibility, event history, and donation views.
- `app/verification_agents.py` contains the LangGraph evidence-review workflow.

## User flows

- Donors and recipient organizations can create accounts and sign in. An administrator account is created from `ANNSETU_ADMIN_EMAIL` and `ANNSETU_ADMIN_PASSWORD` when the API initializes. Admin signup is intentionally disabled.
- Donors publish food with category, quantity, preparation and expiry times, temperature, storage, and pickup coordinates.
- Recipient organizations submit capacity, accepted categories, storage, service hours, location, evidence, and a PDF or TXT registration file (up to 8 MB). Files are encrypted at rest and private to the organization and administrators.
- The LangGraph verification workflow extracts searchable PDF/TXT text, checks name/reference consistency, and reports registry status. Admins still make the approval decision. Government status remains unconfirmed unless a trusted authoritative adapter is configured.
- Matching excludes unapproved organizations and checks food type, storage, freshness, temperature, distance, and remaining capacity. It scores candidates and can split large offers across organizations.
- Each donation listing receives one persistent 16-character delivery code when it is created. The code is retrieved through an authenticated, role-checked endpoint and is visible to its donor, an associated recipient after acceptance, and administrators. Donors verify pickup with the code; recipients confirm receipt. The backend records the authenticated donor and pickup timestamp. A listing card shows an OpenStreetMap map and an OSRM driving route, distance, travel duration, estimated arrival, and allocation status when recipient coordinates and routing data are available.
- Account notifications cover offers, verification review, and handover events. Impact includes meals (400 g estimate), rescued quantity, replacement rescues, at-risk listings, and illustrative emissions estimates.

## Configuration and verification limits

- Set a long random `ANNSETU_SECRET_KEY` in deployments and preserve it across restarts so tokens stay valid. The local default is generated into `backend/.annsetu_secret`.
- Set `ANNSETU_DOCUMENT_KEY` to a persistent Fernet key for encrypted files in deployments. If unset, a local key is generated at `backend/.annsetu_docs.key`; preserve the key and database together.
- Set `ANNSETU_FRONTEND_URL` for the allowed CORS origin. Default is `http://localhost:5173`.
- Set `VITE_OSRM_BASE_URL` in `frontend/.env` only if using a different OSRM-compatible routing service. The default is the public OSRM demo server; it requires no API key and is not intended to provide a production availability guarantee.
- `NGO_DARPAN_LOOKUP_URL` is an optional adapter endpoint. Its response must attest `source_verified: true`; otherwise the result stays unconfirmed. The application does not claim to query an official registry by default.
- `ANNSETU_ENABLE_LLM_REVIEW=true` together with `OPENAI_API_KEY` enables the optional structured OpenAI evidence-review agent. This sends the organization name, reference, evidence summary and extracted document text to OpenAI. It is off by default; verify your privacy and data-handling requirements before enabling it. Set `ANNSETU_REVIEW_MODEL` to choose an account-available model (default `gpt-4o-mini`). AI output is decision support and never approves an organization.
- `ANNSETU_OCR_URL` can point to a trusted OCR adapter for image-only PDFs. It receives the original PDF as `application/pdf`; configure `ANNSETU_OCR_TOKEN` if the adapter requires bearer authentication. OCR failures stay unconfirmed and require manual review.
- Official registry access remains an adapter because the app cannot assume a public API is available. External SMS/email delivery requires a notification provider; notifications currently appear inside the app.
- Temperature screening uses prototype thresholds (60°C hot-held and 5°C chilled) as an additional filter, not a safety guarantee. The [FSSAI surplus-food regulations](https://www.fssai.gov.in/upload/uploadfiles/files/Gazette_Notification_Surplus_Food_06_08_2019.pdf) cover hygienic handling, storage/transport, distribution within shelf life, and records. Follow current applicable requirements and use qualified food-safety judgment.

This is a development application; secure the host, database backups, secrets, and deployment configuration before handling real personal information.
