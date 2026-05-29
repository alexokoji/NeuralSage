# NeuralTrade — AI Crypto Trading Platform

A hybrid-AI crypto trading platform with strict server-side risk management, Bybit + Bitget API integration, a 3D React Three Fiber assistant, and a FastAPI backend that does the actual trading. **API keys are stored encrypted at rest with AES-256-GCM, and keys carrying withdrawal permission are rejected at verification.**

> ⚠️ This software does not promise profits. Trading is risky. Use a testnet API key first.

---

## Architecture

```
┌──────────────────────────┐        ┌─────────────────────────────────────────┐
│ Next.js 13 frontend      │  HTTPS │ FastAPI gateway (Python 3.12)           │
│ – React Three Fiber UI   │ ─────▶ │ – JWT auth                              │
│ – Tailwind + shadcn      │        │ – AES-256-GCM key vault                 │
│ – Typed API client       │        │ – Strategy + Risk + AI Optimizer        │
└──────────────────────────┘        │ – Bybit + Bitget integration            │
                                    │ – APScheduler (in-process):             │
                                    │     • trading tick      (every 15s)     │
                                    │     • optimization sweep (every 6h)     │
                                    │     • daily rollover    (00:01 UTC)     │
                                    └─────────────┬───────────────────────────┘
                                                  │
                                                  ▼
                                    ┌──────────────────────────────┐
                                    │ Postgres 16                  │
                                    │ – users / agents / trades    │
                                    │ – strategy_observations      │
                                    │     (cross-agent learning KB)│
                                    └──────────────────────────────┘

  Redis is OPTIONAL. When set, swaps in for the rate limiter, market-data
  cache, and notification pubsub. When unset (default), those services use
  in-process fallbacks — fine for single-instance deployments.

  Celery is also OPTIONAL. Set ENABLE_IN_PROCESS_SCHEDULER=false to drive
  the jobs from a separate worker process via `celery worker -B` instead.
```

### Cross-agent learning

Every AI optimization run records its best parameter set as a `StrategyObservation` (strategy_type + symbol + timeframe + params + backtest score). Future optimization runs — from *any* user's agent on the same strategy — warm-start the Bayesian optimizer with the top-ranked observations from that pool. Trust ranking blends backtest performance with realized PnL once trades accumulate, so paper-only param sets carry less weight than ones that have actually made money. The result: each agent contributes to a collective knowledge base and benefits from what every other agent has discovered, without abandoning its own risk caps.

### Modules

| Module                   | Where it lives                                         |
| ------------------------ | ------------------------------------------------------ |
| API Gateway              | `backend/app/main.py` + `backend/app/api/v1/`          |
| Auth Service             | `backend/app/api/v1/auth.py`                           |
| Portfolio Service        | `backend/app/api/v1/portfolio.py`                      |
| Exchange Integration     | `backend/app/services/exchange/{bybit,bitget}.py`      |
| Trading Engine           | `backend/app/services/trading_engine.py`               |
| AI Optimization Engine   | `backend/app/services/ai_optimizer.py`                 |
| Risk Management Engine   | `backend/app/services/risk_engine.py`                  |
| Market Data              | `backend/app/services/market_data.py`                  |
| Trade Logger / Analytics | `backend/app/api/v1/trades.py` + Celery rollover task  |
| Notifications            | `backend/app/services/notifications.py` (Redis pubsub) |

---

## Security model

* **Authentication.** Bcrypt password hashes (cost 12), short-lived JWT access tokens (60 min default) + refresh tokens (7 days). All routes are bearer-protected; auth routes are rate-limited.
* **API key storage.** Each Bybit/Bitget API key is encrypted with AES-256-GCM under a single 32-byte server master key (`ENCRYPTION_KEY`). Each ciphertext carries its own random 96-bit nonce. The user's UUID is bound as additional-authenticated-data — tampering invalidates decryption.
* **Permission gate.** `factory.build_client` refuses to construct a client unless the row carries `trade` and not `withdraw`. Both the SQL `CHECK` constraint and `verify_permissions` reject withdrawal-capable keys; verifying detects exchange-side permission grants and disables the row immediately.
* **Risk overrides.** `RiskEngine.evaluate_entry` is called before *every* order placed by an agent. It enforces global hard caps (`MAX_RISK_PER_TRADE_PCT`, `MAX_DAILY_DRAWDOWN_PCT`, `MAX_CONCURRENT_TRADES`, `MAX_CONSECUTIVE_LOSSES`) and overrides agent-level settings if they're looser. AI optimization can only adjust *parameters*; it cannot bypass risk decisions.

---

## Running locally

### 1. Prerequisites

* Docker + Docker Compose
* Node 20+ (for the frontend dev server)
* Python 3.12+ (only if you want to run the backend without Docker)

### 2. One-time setup

```bash
# Backend env
cp backend/.env.example backend/.env

# Generate strong secrets
python -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_urlsafe(64))" >> backend/.env
python -c "import secrets,base64; print('ENCRYPTION_KEY=' + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" >> backend/.env
# After running the two lines above, edit backend/.env to remove the placeholder JWT_SECRET_KEY/ENCRYPTION_KEY values left from the example.
```

### 3. Bring up the stack

```bash
docker compose up --build
```

Services started:

| Service     | Port    | Description                            |
| ----------- | ------- | -------------------------------------- |
| `backend`   | `8000`  | FastAPI HTTP API (`/health`, `/api/v1/*`) |
| `worker`    | —       | Celery worker — runs trade ticks       |
| `beat`      | —       | Celery beat — schedules optimization   |
| `postgres`  | `5432`  | Database                               |
| `redis`     | `6379`  | Cache + Celery broker + pubsub         |

The backend container runs `alembic upgrade head` on start, then boots Uvicorn.

### 4. Frontend dev server

```bash
npm install        # if not already installed
npm run dev        # http://localhost:3000
```

Make sure `NEXT_PUBLIC_API_URL=http://localhost:8000` is set in `.env` (it is, by default).

---

## Day-1 walkthrough

1. Visit `http://localhost:3000/auth/register`, create an account.
2. Go to **Settings → API Keys → Add API Key**. Add a Bybit Testnet key (Read + Trade only). The platform immediately runs `verify_permissions` against Bybit; keys with withdrawal grants are deactivated.
3. Visit **Agents → New Agent**. Pick the verified key, choose a strategy (e.g. EMA Crossover), assign a small amount of testnet capital, set the trading pairs (e.g. `BTCUSDT`).
4. Click **Start** on the agent card. Within `TRADE_LOOP_INTERVAL_SECONDS` (default 15s) Celery dispatches a tick; you'll see opening trades populate in **Trade History**.
5. Inspect risk events at `GET /api/v1/notifications` (the Notifications drawer in the UI consumes the same data once wired).

---

## Strategy reference

Each strategy is a pure function over a candle DataFrame and a parameter dict.

| Strategy        | Default lookback | Tunable parameters (Bayesian opt)                               |
| --------------- | ---------------- | --------------------------------------------------------------- |
| `ema_crossover` | 9 / 21 EMA       | `stop_loss_pct`, `take_profit_pct`, `min_confidence`            |
| `rsi_entry`     | RSI(14) + EMA50  | `oversold`, `overbought`, `stop_loss_pct`, `take_profit_pct`    |
| `breakout`      | Donchian(20)     | `breakout_threshold_pct`, `volume_multiplier`, SL/TP            |
| `micro_scalping`| EMA(8) deviation | `deviation_pct`, `profit_target_pct`, `stop_loss_pct`           |

Add a new strategy by:

1. Subclassing `Strategy` in `backend/app/services/strategy/<name>.py`.
2. Registering it in `backend/app/services/strategy/registry.py`.
3. Adding a row to `backend/app/seed.py` (and a search space in `ai_optimizer.SEARCH_SPACES` if you want the optimizer to tune it).

---

## Background jobs

| Schedule                                  | Task                                              |
| ----------------------------------------- | ------------------------------------------------- |
| Every `TRADE_LOOP_INTERVAL_SECONDS` (15s) | `dispatch_active_agents` → per-agent `run_agent_tick` |
| Every `OPTIMIZATION_INTERVAL_HOURS` (6h)  | `optimize_active_agents` (Bayesian gp_minimize)   |
| Daily at 00:01 UTC                        | `rollover_daily_pnl` (snapshot + reset)           |

---

## API surface (curl examples)

```bash
# Register
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"trader@example.com","password":"a-strong-password","full_name":"Demo"}'

# List my agents
TOKEN=...   # access_token from register/login response
curl http://localhost:8000/api/v1/agents -H "Authorization: Bearer $TOKEN"

# Add a Bybit testnet key
curl -X POST http://localhost:8000/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"exchange":"bybit_testnet","label":"Testnet","api_key":"...","api_secret":"...","is_testnet":true}'
```

OpenAPI/Swagger UI: `http://localhost:8000/docs`.

---

## Project layout

```
project/
├── app/                       # Next.js pages (App Router)
├── components/                # UI components (shadcn + 3D assistant)
├── lib/
│   ├── api/                   # Typed FastAPI client + hooks
│   ├── auth-context.tsx       # JWT auth context
│   └── encryption.ts          # Helpers (`maskApiKey`, etc.)
├── backend/
│   ├── app/
│   │   ├── api/v1/            # Route modules
│   │   ├── core/              # Encryption, security, rate limiting
│   │   ├── models/            # SQLAlchemy ORM
│   │   ├── schemas/           # Pydantic request/response models
│   │   ├── services/          # Strategy, risk, AI optimizer, trading engine
│   │   └── workers/           # Celery tasks
│   ├── alembic/               # DB migrations
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Deploying to Render.com

The repo ships with a [`render.yaml`](render.yaml) Blueprint that provisions Postgres, the FastAPI backend (which drives trading + AI optimization in-process via APScheduler), and optionally the Next.js frontend. No paid Celery worker, no required Redis.

### 1. Generate two secrets locally

```powershell
python -c "import secrets; print(secrets.token_urlsafe(64))"
python -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Keep them — you'll paste them into the Render dashboard in step 4.

### 2. Push the repo to GitHub (or GitLab / Bitbucket)

Render's Blueprints deploy from a connected Git provider. There's nothing special to add to the repo — `render.yaml` lives at the project root and Render picks it up automatically.

### 3. Create the Blueprint

1. Render dashboard → **New ▸ Blueprint**.
2. Pick the repository, accept the default branch, click **Apply**.
3. Render reads `render.yaml` and previews three resources:
   - `neuraltrade-db` (Postgres, Starter)
   - `neuraltrade-backend` (Web Service, Starter, Python) — runs the HTTP API **and** the trading loop / AI optimization sweep / daily rollover via in-process APScheduler. No separate worker needed.
   - `neuraltrade-frontend` (Web Service, Starter, Node) — comment out if you're hosting the frontend on Vercel instead.

Approve the preview. The first build takes ~5 minutes; the database is provisioned in parallel.

### 4. Set the manual secrets

While the build runs, open the **Environment** tab on `neuraltrade-backend`. The env group `neuraltrade-shared` is attached to it:

| Key                  | Value                                                                  |
| -------------------- | ---------------------------------------------------------------------- |
| `JWT_SECRET_KEY`     | The first secret you generated above                                   |
| `ENCRYPTION_KEY`     | The second secret (must be 32 raw bytes after urlsafe-b64 decode)      |
| `APP_CORS_ORIGINS`   | The frontend URL — set after step 5, e.g. `https://neuraltrade-frontend.onrender.com` |

Save. Render rebuilds the backend automatically.

### 5. Wire the frontend → backend URL

Once the `neuraltrade-backend` service is healthy:

1. Copy its public URL from the dashboard (looks like `https://neuraltrade-backend.onrender.com`).
2. On the `neuraltrade-frontend` service, set `NEXT_PUBLIC_API_URL` to that URL (no trailing slash). Save → triggers a redeploy.
3. Go back to the env group and set `APP_CORS_ORIGINS` to the **frontend's** URL. Save → backend picks it up.

### 6. Verify

```bash
curl https://neuraltrade-backend.onrender.com/health
# {"status":"ok","name":"NeuralTrade","env":"production"}
```

Open the frontend URL, register an account, and confirm the dashboard loads. Add a Bybit testnet API key from **Settings → API Keys** to exercise the encryption + verification path before pointing real keys at the worker.

### What's actually different from local Docker?

| Thing                         | Local                              | Render                                        |
| ----------------------------- | ---------------------------------- | --------------------------------------------- |
| Postgres URL                  | `postgresql+asyncpg://...`         | `postgresql://...` — auto-rewritten by config |
| SSL on Postgres               | off                                | required; `?sslmode=require` translated to asyncpg `?ssl=require` |
| Trading loop / optimization   | separate `worker` + `beat` containers | runs **in the FastAPI process** via APScheduler |
| Rate limiter                  | Redis-backed                       | in-memory (single instance)                   |
| Market data cache             | Redis-backed                       | in-memory (per-process, 5–30s TTL)            |
| Notification pubsub           | Redis fan-out                      | DB-only (frontend polls `/notifications`)     |
| Egress IP for exchange APIs   | your laptop                        | Render's shared egress; **not static on Starter** |

### Cost (USD/month)

| Service              | Plan      | Price |
| -------------------- | --------- | ----- |
| Postgres             | Starter   | $7    |
| Backend Web          | Starter   | $7    |
| Frontend Web         | Starter   | $7    |
| **Total**            |           | **$21** |

Skip the frontend on Render (use Vercel free tier) to bring it down to **$14/mo**. Postgres is free for the first 90 days on new accounts, so day-one cost is **$7** (or **$0** if you also host the frontend elsewhere).

When you outgrow a single instance, add Render Key Value ($10/mo) and set `REDIS_URL` — the in-memory implementations will swap to Redis automatically. If you ever want to scale the backend horizontally, also set `ENABLE_IN_PROCESS_SCHEDULER=false` and run a separate $7 worker so jobs don't double-fire.

### Things to watch on Render

- **Single backend instance only** (with `ENABLE_IN_PROCESS_SCHEDULER=true`). Setting `numInstances > 1` would double-fire every trading tick and double-charge fees. Stay at 1 unless you've migrated to external Redis + Celery.
- **Don't deploy to the free tier.** Free Web Services sleep after 15 min idle — your trading loop dies with them. Starter ($7) stays awake.
- **Egress IP changes.** Bybit / Bitget keys with IP allowlists need a static outbound IP. Starter doesn't provide one — either remove IP restrictions on the exchange key, or upgrade to a plan with static egress.
- **First deploy is also a migration.** `alembic upgrade head` runs in `startCommand`. If a migration fails, the backend crash-loops; check **Logs**, fix the migration, push.
- **TLS.** Render terminates HTTPS for you; the Uvicorn process listens on plain HTTP behind their proxy. Don't set TLS env vars in the app.
- **Cross-agent learning.** Every optimization run writes a `StrategyObservation` row. Future runs warm-start from the top observations across **all** agents — your fleet gets smarter as more agents run. To exempt an agent from contributing, set `ai_optimization_enabled=false` on it.

---

## Production checklist

* Generate **distinct** `JWT_SECRET_KEY` and `ENCRYPTION_KEY` per environment. Rotating `ENCRYPTION_KEY` requires re-encrypting `api_keys` rows; the easiest path is to expire all existing keys.
* Restrict `APP_CORS_ORIGINS` to your frontend host.
* Put the API behind TLS (Caddy/Nginx). Set `APP_ENV=production`, `APP_DEBUG=false`.
* Run Postgres with backups + a hot replica before allowing mainnet keys.
* Configure exchange-side IP allowlists on every API key. The `BACKEND` worker should be on a fixed egress IP.
* Add monitoring on `risk_events` (severity = `critical`) and a dead-letter queue for Celery.

---

## License & disclaimer

Trading involves significant risk. Past performance does not guarantee future results. The hard-coded risk caps in this codebase are defaults — **review them and set your own** before pointing this at real capital.
