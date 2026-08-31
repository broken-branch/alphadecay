# Setup

Replay is the quickest way to run alphadecay. The second section documents the requirements for the connected operator deployment used by this project.

## What you need

- Git
- Python 3.12.13
- uv 0.12.3
- Node.js 24.16.0
- npm 11.13.0

PostgreSQL is needed only for the connected agent and database tests. Docker or Podman is needed only to build the production image.

## A. Run Replay without credentials

Replay is the quickest way to try alphadecay. It uses four fixed examples, calls no outside service, and cannot send an order.

Clone the project and install the locked dependencies:

```bash
git clone https://github.com/broken-branch/alphadecay.git
cd alphadecay
uv sync --python 3.12 --frozen --all-groups
npm ci
```

Build the browser app and start the server:

```bash
npm run build
uv run --python 3.12 uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` and choose **Explore Demo**. No environment file or account is needed.

You can also call a Replay example from another terminal:

```bash
curl --fail --silent --show-error \
  --request POST \
  http://127.0.0.1:8000/api/replays/THETA_TAKEOVER
```

The four scenarios use repository fixtures and an execution path that is switched off. They are product examples, not market results.

## B. Review the operator deployment

The connected service runs a persisted agent rather than the fixed Replay examples. It is not a general strategy builder or a ready-made trading plan. It expects an operator-authored policy, calibration record, risk budget, and event plan. The repository validates and stores those records, but it does not choose them for you. Use Replay unless you already have that material.

A connected deployment needs:

- a PostgreSQL database
- Alpaca paper account credentials
- a Gemini key
- an HTTPS origin for the owner controls
- strong owner, session, provider settings, and scheduler secrets
- a reviewed policy, calibration record, and risk budget

The full environment contract is in [`.env.example`](../../.env.example). Copy it to an ignored file and replace every placeholder required for your role:

```bash
cp .env.example .env.local
```

The application does not load that file by itself.

Keep these settings fixed:

```text
APP_RUNTIME_CONFIG_REQUIRED=true
APP_ACCOUNT_ROLE=DEVELOPMENT
ALPACA_API_ENDPOINT=https://paper-api.alpaca.markets
ALPACA_PAPER_TRADE=true
APP_AUTONOMOUS_ENABLED=false
```

Use the `DEVELOPMENT` role for your own testing. The `SUBMISSION` role is reserved for the dedicated hackathon account and its sealed baseline.

For a development run, put the development account key and secret in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. The optional variables beginning with `ALPACA_DEV_` belong to separate operator tools; the connected server does not use them as its account credentials.

`APP_ALLOWED_ORIGIN` must be the exact HTTPS origin where you will open the connected app. Run the server behind an HTTPS proxy or deploy it to an HTTPS host before using the owner controls.

The policy hash, calibration hash, calibration times, and entry budget values come from the operator's trading plan. Do not invent values just to make the server start. The server binds new entries to that plan and checks it again before an order attempt and when a fill is recorded.

Two command-line tools support an existing development plan. `ops.launch.opportunity_baseline` captures a read-only account baseline for an already-authored plan. `ops.launch.opportunity_bootstrap` then validates and persists the complete plan and baseline. Neither command creates a strategy or decides its limits. Run each command with `--help` to see its file arguments.

The model and Alpaca keys belong only in the ignored environment file or your hosting provider's secret store. Never put them in browser settings, source files, screenshots, fixtures, logs, or Git history.

After PostgreSQL is running, load the file and start the server:

```bash
set -a
source .env.local
set +a
uv run --python 3.12 uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Startup applies the checked migrations and stops if the database or runtime settings do not match the expected contract.

With `APP_AUTONOMOUS_ENABLED=false`, you can inspect the connected product without allowing scheduled orders. Enabling that server setting still does not run the agent. The authenticated owner must also arm the account gate. The scheduler cannot arm itself, and the owner can disarm the account without choosing or changing an order.

The activation path has been rehearsed against a development paper account with no change to the broker book. This release does not include public proof of an autonomous broker write. Keep the account disarmed until you have a reviewed paper trading plan.

## Build the production image

With Podman:

```bash
podman build --tag alphadecay:local .
```

With Docker:

```bash
docker build --tag alphadecay:local .
```

The image runs as an unprivileged user. Running the full image also requires PostgreSQL and the server environment described above. Building the image does not prove that a deployment or provider connection works.

## Troubleshooting

- Use Python 3.12. Newer Python versions are intentionally excluded.
- If the page opens but Replay calls fail, confirm that FastAPI is serving the compiled `dist` directory from port 8000.
- In Replay mode, `/api/proof` returns `NOT_PUBLISHED` until an eligible account snapshot has been published.
- If connected startup fails, read the reported setting or database error. Do not change the Alpaca host or disable the paper guard to get around it.
