# DriftWatch SaaS API

FastAPI backend for managing LLM drift detection monitoring at scale.

## What It Does

- **User management**: Registration, login, API key generation
- **Prompt management**: Upload and manage test prompts (100 per Starter, unlimited for Pro)
- **Baseline setting**: Establish baseline responses for each prompt
- **Automated monitoring**: Hourly drift checks against all user prompts via APScheduler
- **Result storage**: Full audit trail of drift detection results with regressions

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Set environment variables
export SECRET_KEY="your-secret-key"
export ANTHROPIC_API_KEY="sk-ant-..."

# Run locally
python3 main.py

# API will be at http://localhost:8000
# Docs at http://localhost:8000/docs
```

## Architecture

```
main.py — FastAPI app + all routes
├── models.py — SQLAlchemy ORM (User, Prompt, Baseline, Result, Run)
├── auth.py — JWT tokens + password hashing
├── drift_runner.py — Drift detection logic (reuses llm-drift core)
└── scheduler.py — APScheduler background tasks (hourly monitoring)
```

## API Endpoints

### Auth
- `POST /auth/register` — Create account, get token
- `POST /auth/login` — Login with email/password

### Prompts
- `GET /prompts` — List user's test prompts
- `POST /prompts` — Add a test prompt
- `DELETE /prompts/{id}` — Remove a prompt

### Baselines
- `POST /baselines/{prompt_id}` — Set baseline for a prompt (run once to establish reference)

### Monitoring
- `GET /results` — Get recent drift check results
- `POST /monitor/run` — Trigger manual drift check (else runs hourly)
- `GET /status` — Account info + monitoring status

### Health
- `GET /health` — Health check

## Deployment

### Railway (recommended)
```bash
# Deploy in 2 minutes
railway up

# Set environment variables in Railway dashboard
# - SECRET_KEY
# - ANTHROPIC_API_KEY
```

### Docker
```bash
docker build -t driftwatch-api .
docker run -e SECRET_KEY="..." -e ANTHROPIC_API_KEY="..." -p 8000:8000 driftwatch-api
```

### Manual
```bash
# Linux/Mac
pip install -r requirements.txt
SECRET_KEY=xyz ANTHROPIC_API_KEY=sk-ant-... python3 main.py

# Production: use Gunicorn
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:8000 main:app
```

## Database

Default: SQLite (dev-friendly, zero infra)
- Automatically creates schema on startup
- File: `driftwatch.db`

Production: Postgres
- Set `DATABASE_URL=postgresql://user:pass@host/driftwatch`

## Monitoring Scheduler

APScheduler runs drift checks every hour at minute 5:

```
Every hour at :05 →
  Fetch all active users
  For each user, run their test prompts
  Store results in DriftResult table
  Send email alert if drift > threshold or regression detected
```

Alerts can be sent via:
- SMTP email (configure SMTP_HOST, SMTP_USER, SMTP_PASS env vars)
- Placeholder for Slack, PagerDuty, webhooks (extend `send_alert()`)

## Integration with llm-drift CLI

The backend reuses drift detection logic from `/workspace/llm-drift/core/`:
- Same validator system
- Same drift scoring algorithm
- Same 20-prompt test suite as default

Users can:
1. Use the managed service (API)
2. Run the CLI locally (no account needed)
3. Both simultaneously (API monitors managed prompts, CLI monitors custom ones)

## Stripe Integration (TODO)

Plans are defined on Stripe:
- Starter £99/mo: 100 prompts, hourly checks
- Pro £249/mo: unlimited prompts, 15-min checks

Next: Add webhook to validate payment and set user.plan in database.

## Development

```bash
# Run tests (when added)
pytest

# Format
black *.py

# Type check
mypy main.py
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app + routes (878 lines total across all files) |
| `models.py` | SQLAlchemy ORM |
| `auth.py` | JWT + password auth |
| `drift_runner.py` | Drift detection + scoring |
| `scheduler.py` | APScheduler background tasks |
| `Dockerfile` | Container image |
| `railway.json` | Railway.app deployment config |
| `requirements.txt` | Python dependencies |

## Status

**MVP complete**. Core functionality:
- ✅ User accounts + auth
- ✅ Prompt management
- ✅ Baseline setting
- ✅ Drift detection
- ✅ Hourly scheduling
- ✅ Result storage
- ⏳ Email alerts (placeholder)
- ⏳ Slack/webhook integration
- ⏳ Stripe payment webhook

## Next Steps

1. Deploy to Railway
2. Test end-to-end with real user
3. Add Stripe payment webhook
4. Email alert integration
5. Frontend dashboard for results
