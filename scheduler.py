"""
Background scheduler for hourly drift checks
Uses APScheduler to run drift detection for all active users
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_
import os

from models import User, Prompt, DriftBaseline, DriftRun, DriftResult
from drift_runner import run_drift_check

logger = logging.getLogger(__name__)


def run_drift_checks_for_user(user_id: str, db: Session):
    """
    Run drift checks for a specific user's prompts
    This is called by the scheduler every hour
    """
    try:
        user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
        if not user:
            return
        
        # Get user's active prompts
        prompts = db.query(Prompt).filter(
            and_(Prompt.user_id == user_id, Prompt.active == True)
        ).all()
        
        if not prompts:
            return
        
        # Get baselines for each prompt
        prompt_data = []
        for prompt in prompts:
            baseline = db.query(DriftBaseline).filter(
                and_(
                    DriftBaseline.prompt_id == prompt.id,
                    DriftBaseline.user_id == user_id
                )
            ).order_by(DriftBaseline.created_at.desc()).first()
            
            if baseline:
                prompt_data.append({
                    "prompt_id": prompt.prompt_id,
                    "prompt_text": prompt.prompt_text,
                    "validators": prompt.validators,
                    "baseline_response": baseline.response_text,
                    "baseline_validators": baseline.validators_result
                })
        
        if not prompt_data:
            return  # No baselines yet
        
        # Run drift check
        check_result = run_drift_check(
            prompt_data,
            user_api_key=None,  # Will use ANTHROPIC_API_KEY from env
            model="claude-3-haiku-20240307"
        )
        
        # Store results in database
        run = DriftRun(
            user_id=user_id,
            avg_drift=check_result["summary"].get("avg_drift", 0),
            max_drift=check_result["summary"].get("max_drift", 0),
            alert_count=check_result["summary"].get("alerts", 0)
        )
        db.add(run)
        db.flush()
        
        # Store individual results
        for result in check_result["results"]:
            if "error" not in result:
                prompt = db.query(Prompt).filter(
                    and_(Prompt.user_id == user_id, Prompt.prompt_id == result["prompt_id"])
                ).first()
                
                if prompt:
                    drift_result = DriftResult(
                        user_id=user_id,
                        run_id=run.id,
                        prompt_id=prompt.id,
                        drift_score=result.get("drift_score", 0),
                        alert_level=result.get("alert_level", "none"),
                        regressions=result.get("regressions", []),
                        baseline_response=result.get("baseline_response", ""),
                        current_response=result.get("current_response", ""),
                        validators_result=result.get("validators", {})
                    )
                    db.add(drift_result)
        
        # Check if we need to send alerts
        if run.alert_count > 0:
            send_alert(user, check_result)
        
        run.alert_sent = True
        db.commit()
        logger.info(f"Drift check completed for user {user_id}: avg={run.avg_drift}, alerts={run.alert_count}")
        
    except Exception as e:
        logger.error(f"Error running drift checks for user {user_id}: {str(e)}")
        db.rollback()


def send_alert(user: User, check_result: dict):
    """
    Send alert email/notification when drift detected
    Placeholder - implement with email service in production
    """
    try:
        summary = check_result["summary"]
        alerts = summary.get("alert_details", [])
        
        message = f"""
DriftWatch Alert for {user.email}

Drift check detected {summary.get('alerts', 0)} issues:

Average drift: {summary.get('avg_drift', 0):.3f}
Maximum drift: {summary.get('max_drift', 0):.3f}

Issues:
"""
        for alert in alerts[:5]:  # Show top 5 alerts
            message += f"\n- [{alert.get('alert_level')}] {alert.get('name')}: {alert.get('drift_score'):.3f}"
        
        message += f"\n\nView details: https://driftwatch.example.com/results"
        
        # TODO: Send via email service (e.g., SendGrid, AWS SES)
        logger.info(f"Alert for {user.email}: {summary.get('alerts')} issues detected")
        
    except Exception as e:
        logger.error(f"Error sending alert: {str(e)}")


class DriftCheckScheduler:
    """Background scheduler for drift detection"""
    
    def __init__(self, db_session_maker):
        self.scheduler = BackgroundScheduler()
        self.db_session_maker = db_session_maker
        self.is_running = False
    
    def start(self):
        """Start the background scheduler"""
        if self.is_running:
            return
        
        # Run drift checks every hour at minute 5
        self.scheduler.add_job(
            self.run_all_checks,
            CronTrigger(minute="5"),
            id="drift_check_hourly",
            name="Hourly drift check for all users"
        )
        
        self.scheduler.start()
        self.is_running = True
        logger.info("Drift check scheduler started")
    
    def stop(self):
        """Stop the background scheduler"""
        if self.is_running:
            self.scheduler.shutdown()
            self.is_running = False
            logger.info("Drift check scheduler stopped")
    
    def run_all_checks(self):
        """Run drift checks for all active users"""
        db = self.db_session_maker()
        try:
            users = db.query(User).filter(User.is_active == True).all()
            for user in users:
                run_drift_checks_for_user(user.id, db)
        finally:
            db.close()
