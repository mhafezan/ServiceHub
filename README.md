<!-- Introduces ServiceHub's purpose, architecture, local setup, operations, and current maturity. -->

# ServiceHub

Agent-assisted community services delivered through Telegram.

ServiceHub is an extensible platform for coordinating local services from a Telegram channel and private bot conversations. Its first service is an Ontario ride marketplace: riders publish immediate or scheduled requests, drivers submit private CAD offers, riders choose an offer, and both participants manage pickup, live location, trip progress, and completion through a Telegram Mini App.

> **Development status:** ServiceHub is under active development. The core application and Mini App are present, but automated tests, container and cloud infrastructure, CI/CD workflows, and production deployment validation are not yet included in this repository.

## What ServiceHub provides

- A public Telegram channel entry point with **Need a Ride** and **Ask Lili** actions.
- Private rider and driver workflows backed by explicit confirmation before consequential actions.
- Immediate and scheduled Ontario rides using Google Places for address selection.
- Five-minute driver bidding followed by up to five minutes for the rider to select an offer.
- Transactional offer acceptance, one active ride commitment per user, stale-action protection, and idempotent commands.
- Address privacy: channel posts contain only street and municipality; exact addresses are available only to matched participants.
- Consent-based Telegram and Mini App location sharing.
- Proximity-gated trip start and completion, including rider confirmation of pickup.
- A supervisor that routes requests to the ride assistant or Lili, the read-only service-information assistant.
- A durable database-backed job queue for Telegram updates, notifications, deadlines, reminders, retries, and retention work.
- De-identified completed-ride samples containing measured GPS distance, agreed CAD price, and rider-supplied pickup municipality for future pricing research.

Payments, ratings, driver verification, rental transactions, public discussion, and model training are outside the current implementation.

## User journey

```mermaid
flowchart LR
    A[Telegram channel] -->|Need a Ride| B[Private bot]
    B --> C[Mini App ride form]
    C --> D[Channel request]
    D -->|Make an Offer| E[Private driver offer]
    E --> F[Rider selects offer]
    F --> G[Driver en route]
    G --> H[Driver requests pickup confirmation]
    H -->|Rider confirms| I[Trip started]
    I --> J[Trip completed]
```

The ride state machine is enforced by the backend:

```text
Draft → Open → Selecting → Matched → Driver_En_Route
      → Pickup_Confirmation_Pending → Trip_Started → Completed
```

Cancellation, expiry, rider-controlled reopening, and administrative closure are explicit additional states.

## Architecture

```mermaid
flowchart TB
    TG[Telegram channel and private bot] --> API[FastAPI application]
    UI[React Telegram Mini App] --> API
    API --> SUP[Supervisor]
    SUP --> RIDE[Ride assistant]
    SUP --> LILI[Lili information assistant]
    RIDE --> DOMAIN[Transactional ride domain]
    API --> DOMAIN
    DOMAIN --> DB[(MySQL / InnoDB)]
    DOMAIN --> JOBS[Durable job outbox]
    JOBS --> WORKER[Worker]
    WORKER --> TG
    API --> PLACES[Google Places]
    RIDE --> OPENAI[OpenAI Responses API]
    LILI --> OPENAI
```

The language model interprets requests and proposes actions. It does not control identity, authorization, prices, addresses, ride state, or user consent. Those decisions remain in authenticated application code and database transactions.

### Technology stack

| Area | Technology |
| --- | --- |
| Backend | Python 3.12+, FastAPI, SQLAlchemy, Alembic |
| Database | MySQL 8 with InnoDB; SQLite is available for limited local development |
| Telegram | Telegram Bot API and Telegram Mini Apps |
| Agents | OpenAI Responses API with strict function tools |
| Frontend | React 19, TypeScript, Vite |
| Addresses | Google Places API |
| Async work | Database outbox locally; Google Cloud Tasks integration for deployment |
| Intended cloud | Cloud Run, Cloud SQL, Cloud Tasks, Cloud Scheduler, Secret Manager |

## Repository layout

```text
.
├── backend/
│   ├── migrations/            # Alembic migration environment and schema versions
│   ├── servicehub/
│   │   ├── agents/            # Supervisor, ride assistant, and Lili orchestration
│   │   ├── api/               # Public, Telegram webhook, and internal HTTP routes
│   │   ├── core/              # Environment configuration and repository paths
│   │   ├── database/          # SQLAlchemy sessions and table definitions
│   │   ├── integrations/      # Telegram and external provider adapters
│   │   ├── operations/        # Setup, recovery, worker, and export commands
│   │   ├── rides/             # Ride state machine and role-filtered views
│   │   ├── security/          # Telegram verification and signed sessions
│   │   └── workers/           # Durable jobs and Cloud Tasks dispatch
│   ├── alembic.ini            # Migration configuration
│   └── pyproject.toml          # Python package and development tooling
├── frontend/                  # React Telegram Mini App
│   ├── public/                # Privacy and service terms
│   └── src/                   # Ride form, status, offers, and tracking UI
├── .env.example               # Non-secret configuration template
└── README.md
```

## Local development

### Prerequisites

- Python 3.12 or newer
- Node.js 20 or newer
- MySQL 8 for realistic transactional testing
- A Telegram bot and channel for live Telegram testing
- OpenAI and Google Places credentials for their respective features
- A public HTTPS URL when registering a Telegram webhook or opening the Mini App from Telegram

### 1. Clone and configure

```bash
git clone https://github.com/mhafezan/ServiceHub.git
cd ServiceHub
cp .env.example .env
```

Generate a random `SESSION_SECRET` of at least 32 characters, then fill the required values in `.env`. Do not commit `.env` or any credential file.

For MySQL, create an empty database and a least-privilege application user, then set:

```dotenv
DATABASE_URL=mysql+pymysql://servicehub:your-local-password@localhost:3306/servicehub
```

### 2. Install the backend

```bash
python -m venv .venv
```

Activate the environment:

```bash
# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS or Linux
source .venv/bin/activate
```

Install the package and development tools:

```bash
python -m pip install --upgrade pip
python -m pip install -e "./backend[dev]"
```

### 3. Create the schema

```bash
cd backend
alembic upgrade head
cd ..
```

### 4. Install and build the Mini App

```bash
cd frontend
npm install
npm run build
cd ..
```

The backend serves `frontend/dist` when that directory exists. For frontend-only development, run `npm run dev`; Vite proxies `/api` and `/guides` to `http://localhost:8000`.

### 5. Start the application and worker

Virtual-environment activation applies to one terminal at a time. Activate `.venv` separately in
each terminal opened for the API, worker, or operational commands. On Windows PowerShell, run:

```powershell
.\.venv\Scripts\Activate.ps1
```

Run the API:

```bash
uvicorn servicehub.api.app:app --app-dir backend --reload --host 0.0.0.0 --port 8000
```

Run the local worker in another terminal:

```bash
servicehub worker
```

On Windows, the worker can also be started without activation by invoking its executable directly:

```powershell
.\.venv\Scripts\servicehub.exe worker
```

Health endpoints:

- `GET /health` — process liveness
- `GET /ready` — database connectivity
- `GET /api/docs` — local-only API documentation

### 6. Prepare generated messages and Telegram

The deployed application requires validated LLM-generated message templates. With `OPENAI_API_KEY` configured, generate and store them:

```bash
servicehub generate-templates
```

Set `PUBLIC_URL` to the public HTTPS origin, configure the Telegram values, add the bot as a channel administrator, and then register the webhook and pinned entry message:

Telegram cannot send webhooks to `localhost`. For local development, keep the API running and expose
port 8000 through a public HTTPS tunnel. For example, after installing `cloudflared`, run this in a
separate terminal:

```powershell
cloudflared tunnel --url http://localhost:8000
```

Copy the generated `https://...trycloudflare.com` origin into `PUBLIC_URL` in `.env`, restart the API,
and keep the tunnel running. Quick Tunnel addresses change whenever the tunnel is restarted, so update
`PUBLIC_URL` and rerun `setup-telegram` after each change.

```bash
servicehub setup-telegram
```

The bot needs permission to post and edit channel messages and to inspect channel membership. Treat this command as an environment setup operation: running it again creates another pinned entry message.

## Configuration

The complete placeholder configuration is in [.env.example](.env.example). Important groups include:

| Variables | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy connection string |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` | Bot identity and API access |
| `TELEGRAM_CHANNEL_ID`, `TELEGRAM_CHANNEL_URL` | ServiceHub channel |
| `TELEGRAM_WEBHOOK_SECRET` | Authenticates incoming Telegram webhooks |
| `PUBLIC_URL` | Public HTTPS origin for webhook and Mini App links |
| `SESSION_SECRET` | Signs short-lived Mini App and address tokens |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Supervisor, assistants, and generated wording |
| `GOOGLE_PLACES_API_KEY` | Server-side address search and Ontario validation |
| `OPERATOR_IDS` | Telegram IDs allowed to perform exceptional recovery |
| `TASK_MODE` | `local` for polling or `gcp` for Cloud Tasks dispatch |
| `GCP_*`, `WORKER_*`, `INTERNAL_AUDIENCE` | Authenticated Cloud Tasks worker configuration |

Non-local startup fails closed when required secrets are missing or the database is not MySQL.

## Operational commands

```bash
servicehub dead-jobs
servicehub retry --id <job-id>
servicehub close-ride --id <ride-id> --actor <telegram-id> --reason "Detailed recovery reason"
servicehub export-training --output private-data/training.jsonl
```

- Dead jobs are listed without private payloads or credentials.
- Administrative closure requires an allowlisted operator and a descriptive audit reason.
- Training exports can only be written beneath the ignored `private-data/` directory.

## Privacy and security

- Telegram Mini App initialization data is verified and exchanged for a short-lived signed session.
- Commands are actor-bound, revision-bound, expire after five minutes, and execute once.
- Exact addresses and private instructions are omitted from channel messages.
- Location is accepted only from matched participants during active tracking states.
- Driver trip controls require a recent, sufficiently accurate location within 500 metres of the relevant endpoint.
- Credentials remain server-side and are excluded through `.gitignore`; `.env.example` contains placeholders only.
- Transactional state changes and notification jobs commit together.
- Raw location samples are removed after terminal rides; exact operational details, audit history, and de-identified training samples follow separate retention periods.
- Google Places data is used operationally and is not exported as model-training data.

The public [privacy notice](frontend/public/privacy.html) and [service terms](frontend/public/terms.html) should be reviewed for the operator's legal and policy requirements before launch.

## Verification

The project declares the following checks, although the corresponding test suite and CI workflows still need to be added:

```bash
ruff check .
mypy servicehub
pytest

cd frontend
npm run build
npm test
```

Production readiness should include MySQL concurrency tests, Telegram end-to-end testing with a separate bot/channel, mobile Mini App testing, provider-outage tests, migration checks, dependency and secret scanning, and staging deployment verification.

## Roadmap

- Add automated domain, API, provider, worker, and browser tests.
- Add Docker-based local development and reproducible MySQL integration testing.
- Add GitHub Actions CI/CD and infrastructure-as-code for the intended GCP deployment.
- Add the ride quick guide consumed by Lili and the public guide endpoint.
- Continue splitting provider adapters as additional external services are introduced.
- Add a rental service through the supervisor's service registry.
- Train and evaluate city-aware price guidance only after enough eligible first-party samples exist.

## Contributing

Before opening a pull request:

1. Keep authorization and state transitions in domain services rather than prompts or frontend code.
2. Add a concise module comment or docstring to authored files and purpose-focused documentation to named functions and components.
3. Do not commit credentials, `.env` files, private data, raw location exports, or Terraform state.
4. Add tests for changed business rules, especially concurrency, stale commands, privacy boundaries, and retries.
5. Run the backend checks and frontend build locally.

## License

ServiceHub is distributed under the [MIT License](LICENSE).
