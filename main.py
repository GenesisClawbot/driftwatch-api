"""
DriftWatch SaaS API
FastAPI backend for LLM drift detection monitoring with Stripe billing
"""
from fastapi import FastAPI, HTTPException, Depends, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta
from typing import Optional, List
import os, logging, stripe

from models import Base, User, Prompt, DriftBaseline, DriftResult, DriftRun
from auth import (hash_password, verify_password, create_access_token,
                  verify_token, generate_api_key, TokenData)
from scheduler import DriftCheckScheduler, run_drift_checks_for_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Stripe ────────────────────────────────────────────────────────────────────
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_STARTER_PRICE  = os.environ.get("STRIPE_STARTER_PRICE_ID", "price_1TAEMZ7dVu3KiOEDGuyO9mtF")
STRIPE_PRO_PRICE      = os.environ.get("STRIPE_PRO_PRICE_ID",     "price_1TAEMa7dVu3KiOEDEgg8hFWf")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://genesisclawbot.github.io/llm-drift")

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./driftwatch.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="DriftWatch API",
    description="LLM Behavioural Drift Detection SaaS",
    version="1.0.0"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

scheduler = DriftCheckScheduler(SessionLocal)


@app.on_event("startup")
async def startup():
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    scheduler.stop()

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    data = verify_token(token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(User).filter(User.id == data.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_active_plan(user: User = Depends(get_current_user)) -> User:
    """Gate endpoints that require an active paid plan"""
    if user.plan not in ("starter", "pro"):
        raise HTTPException(
            status_code=402,
            detail="Active subscription required. Visit /billing/checkout to subscribe."
        )
    return user


# ── Pydantic Schemas ──────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    plan: str

class PromptIn(BaseModel):
    prompt_id: str
    name: str
    category: str
    prompt_text: str
    validators: List[str] = []

class CheckoutRequest(BaseModel):
    plan: str = "starter"  # starter | pro


# ── Auth Endpoints ────────────────────────────────────────────────────────────
@app.post("/auth/register", response_model=TokenResponse, tags=["auth"])
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Create a new account. No payment required until monitoring activates."""
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(400, "Email already registered")
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        api_key=generate_api_key(),
        plan="free"       # free until Stripe payment confirmed
    )
    db.add(user); db.commit(); db.refresh(user)
    token = create_access_token(user.id, user.email)
    logger.info(f"Registered: {user.email}")
    return TokenResponse(access_token=token, user_id=user.id, plan=user.plan)


@app.post("/auth/login", response_model=TokenResponse, tags=["auth"])
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Invalid credentials")
    token = create_access_token(user.id, user.email)
    return TokenResponse(access_token=token, user_id=user.id, plan=user.plan)


# ── Billing Endpoints ─────────────────────────────────────────────────────────
@app.post("/billing/checkout", tags=["billing"])
def create_checkout_session(
    body: CheckoutRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a Stripe checkout session. Returns redirect URL."""
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured")

    price_id = STRIPE_STARTER_PRICE if body.plan == "starter" else STRIPE_PRO_PRICE

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=user.email,
            client_reference_id=user.id,
            success_url=f"{APP_BASE_URL}/onboard.html?session_id={{CHECKOUT_SESSION_ID}}&plan={body.plan}",
            cancel_url=f"{APP_BASE_URL}/?checkout=cancelled",
            metadata={"user_id": user.id, "plan": body.plan}
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except stripe.error.StripeError as e:
        raise HTTPException(400, str(e))


@app.post("/billing/webhook", tags=["billing"])
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Stripe webhook events to activate/deactivate plans."""
    payload  = await request.body()
    sig      = request.headers.get("stripe-signature", "")

    # Verify signature in production
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError:
            raise HTTPException(400, "Invalid webhook signature")
    else:
        import json
        event = json.loads(payload)

    event_type = event.get("type", "")
    data       = event.get("data", {}).get("object", {})

    logger.info(f"Stripe event: {event_type}")

    if event_type == "checkout.session.completed":
        user_id = data.get("client_reference_id") or data.get("metadata", {}).get("user_id")
        plan    = data.get("metadata", {}).get("plan", "starter")
        if user_id:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.plan = plan
                user.stripe_customer_id = data.get("customer")
                user.stripe_subscription_id = data.get("subscription")
                db.commit()
                logger.info(f"Activated {plan} plan for {user.email}")

    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        sub_id = data.get("id")
        if sub_id:
            user = db.query(User).filter(User.stripe_subscription_id == sub_id).first()
            if user:
                user.plan = "free"
                db.commit()
                logger.info(f"Deactivated plan for {user.email}")

    elif event_type == "invoice.payment_failed":
        customer_id = data.get("customer")
        if customer_id:
            user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
            if user:
                logger.warning(f"Payment failed for {user.email}")

    return {"received": True}


@app.get("/billing/portal", tags=["billing"])
def billing_portal(user: User = Depends(get_current_user)):
    """Return Stripe customer portal URL for subscription management."""
    if not user.stripe_customer_id:
        raise HTTPException(400, "No active subscription found")
    try:
        session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{APP_BASE_URL}/dashboard/"
        )
        return {"portal_url": session.url}
    except stripe.error.StripeError as e:
        raise HTTPException(400, str(e))


# ── Prompt Management ─────────────────────────────────────────────────────────
@app.get("/prompts", tags=["prompts"])
def list_prompts(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    prompts = db.query(Prompt).filter(Prompt.user_id == user.id, Prompt.active == True).all()
    return [
        {
            "id": p.id, "prompt_id": p.prompt_id, "name": p.name,
            "category": p.category, "validators": p.validators,
            "active": p.active, "created_at": p.created_at.isoformat()
        }
        for p in prompts
    ]


@app.post("/prompts", tags=["prompts"])
def create_prompt(
    body: PromptIn,
    user: User = Depends(require_active_plan),
    db: Session = Depends(get_db)
):
    """Add a test prompt. Requires active paid plan."""
    limit = 100 if user.plan == "starter" else 10_000
    count = db.query(Prompt).filter(Prompt.user_id == user.id, Prompt.active == True).count()
    if count >= limit:
        raise HTTPException(400, f"Prompt limit ({limit}) reached for {user.plan} plan")

    p = Prompt(
        user_id=user.id,
        prompt_id=body.prompt_id,
        name=body.name,
        category=body.category,
        prompt_text=body.prompt_text,
        validators=body.validators
    )
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id, "prompt_id": p.prompt_id, "name": p.name}


@app.delete("/prompts/{prompt_id}", tags=["prompts"])
def delete_prompt(
    prompt_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    p = db.query(Prompt).filter(Prompt.id == prompt_id, Prompt.user_id == user.id).first()
    if not p:
        raise HTTPException(404, "Prompt not found")
    p.active = False
    db.commit()
    return {"deleted": prompt_id}


# ── Baseline ──────────────────────────────────────────────────────────────────
@app.post("/baselines/run", tags=["monitoring"])
def run_baseline(
    user: User = Depends(require_active_plan),
    db: Session = Depends(get_db)
):
    """Run baseline for all user prompts. Must be done before drift checks."""
    from drift_runner import run_drift_check

    prompts = db.query(Prompt).filter(Prompt.user_id == user.id, Prompt.active == True).all()
    if not prompts:
        raise HTTPException(400, "No prompts configured. Add prompts first.")

    prompt_data = [
        {"prompt_id": p.prompt_id, "prompt_text": p.prompt_text,
         "validators": p.validators, "baseline_response": "", "baseline_validators": {}}
        for p in prompts
    ]

    result = run_drift_check(prompt_data, user_api_key=None)

    # Store baselines
    created = 0
    for r in result.get("results", []):
        if "error" not in r:
            prompt = next((p for p in prompts if p.prompt_id == r["prompt_id"]), None)
            if prompt:
                baseline = DriftBaseline(
                    user_id=user.id,
                    prompt_id=prompt.id,
                    response_text=r.get("current_response", ""),
                    validators_result=r.get("validators", {})
                )
                db.add(baseline)
                created += 1

    db.commit()
    return {"baselines_created": created, "total_prompts": len(prompts)}


# ── Results / Monitoring ──────────────────────────────────────────────────────
@app.get("/results", tags=["monitoring"])
def list_results(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get recent drift results across all runs."""
    runs = db.query(DriftRun).filter(DriftRun.user_id == user.id)\
              .order_by(DriftRun.created_at.desc()).limit(20).all()

    output = []
    for run in runs:
        results = db.query(DriftResult).filter(DriftResult.run_id == run.id).all()
        output.append({
            "run_id": run.id,
            "run_at": run.run_at.isoformat(),
            "avg_drift": run.avg_drift,
            "max_drift": run.max_drift,
            "alert_count": run.alert_count,
            "results": [
                {
                    "prompt_id": r.prompt_id,
                    "drift_score": r.drift_score,
                    "alert_level": r.alert_level,
                    "regressions": r.regressions
                }
                for r in results
            ]
        })
    return output


@app.post("/monitor/run", tags=["monitoring"])
def trigger_check(
    user: User = Depends(require_active_plan),
    db: Session = Depends(get_db)
):
    """Manually trigger a drift check. Returns summary."""
    run_drift_checks_for_user(user.id, db)

    last_run = db.query(DriftRun).filter(DriftRun.user_id == user.id)\
                 .order_by(DriftRun.created_at.desc()).first()
    if not last_run:
        return {"message": "No baselines found. Run POST /baselines/run first."}

    return {
        "run_id": last_run.id,
        "avg_drift": last_run.avg_drift,
        "max_drift": last_run.max_drift,
        "alerts": last_run.alert_count,
        "run_at": last_run.run_at.isoformat()
    }


# ── Status ────────────────────────────────────────────────────────────────────
@app.get("/status", tags=["account"])
def status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    active_prompts = db.query(Prompt).filter(
        Prompt.user_id == user.id, Prompt.active == True
    ).count()

    last_run = db.query(DriftRun).filter(DriftRun.user_id == user.id)\
                 .order_by(DriftRun.created_at.desc()).first()

    return {
        "user_id": user.id,
        "email": user.email,
        "plan": user.plan,
        "api_key": user.api_key,
        "active_prompts": active_prompts,
        "last_check_at": last_run.run_at.isoformat() if last_run else None,
        "billing_portal": "/billing/portal" if user.stripe_customer_id else None,
        "upgrade_url": "/billing/checkout"
    }


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/")
def root():
    return {
        "service": "DriftWatch API",
        "docs": "/docs",
        "health": "/health",
        "register": "POST /auth/register",
        "subscribe": "POST /billing/checkout"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
