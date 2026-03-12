"""
DriftWatch SaaS - Database Models
SQLAlchemy ORM models for users, prompts, and drift detection results
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, Boolean, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

Base = declarative_base()


class User(Base):
    """User account model"""
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    api_key = Column(String, unique=True, nullable=False, index=True)
    plan = Column(String, default="starter")  # starter or pro
    is_active = Column(Boolean, default=True)
    stripe_customer_id = Column(String, nullable=True, index=True)
    stripe_subscription_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    prompts = relationship("Prompt", back_populates="user", cascade="all, delete-orphan")
    baselines = relationship("DriftBaseline", back_populates="user", cascade="all, delete-orphan")
    results = relationship("DriftResult", back_populates="user", cascade="all, delete-orphan")
    runs = relationship("DriftRun", back_populates="user", cascade="all, delete-orphan")


class Prompt(Base):
    """Test prompt model"""
    __tablename__ = "prompts"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    prompt_id = Column(String, nullable=False)  # e.g., "json-01"
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)  # json, instruction, code, classification, safety, verbosity, extraction
    prompt_text = Column(Text, nullable=False)
    validators = Column(JSON, default=list)  # List of validator names
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="prompts")
    baselines = relationship("DriftBaseline", back_populates="prompt", cascade="all, delete-orphan")
    results = relationship("DriftResult", back_populates="prompt", cascade="all, delete-orphan")


class DriftBaseline(Base):
    """Baseline responses for drift comparison"""
    __tablename__ = "drift_baselines"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    prompt_id = Column(String, ForeignKey("prompts.id"), nullable=False, index=True)
    response_text = Column(Text, nullable=False)
    validators_result = Column(JSON)  # e.g., {"is_valid_json": true, "has_keys:name,email": true}
    model = Column(String, default="claude-3-haiku-20240307")
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="baselines")
    prompt = relationship("Prompt", back_populates="baselines")


class DriftRun(Base):
    """A drift check run (contains multiple results)"""
    __tablename__ = "drift_runs"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    model = Column(String, default="claude-3-haiku-20240307")
    run_at = Column(DateTime, default=datetime.utcnow)
    avg_drift = Column(Float)
    max_drift = Column(Float)
    alert_count = Column(Integer, default=0)
    alert_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="runs")
    results = relationship("DriftResult", back_populates="run", cascade="all, delete-orphan")


class DriftResult(Base):
    """Individual drift detection result for a prompt"""
    __tablename__ = "drift_results"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    run_id = Column(String, ForeignKey("drift_runs.id"), nullable=False, index=True)
    prompt_id = Column(String, ForeignKey("prompts.id"), nullable=False, index=True)
    
    drift_score = Column(Float, nullable=False)  # 0.0 to 1.0
    alert_level = Column(String)  # none, low, medium, high, critical
    regressions = Column(JSON, default=list)  # Validators that were passing but now fail
    
    baseline_response = Column(Text)
    current_response = Column(Text)
    validators_result = Column(JSON)  # Current validator results
    
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    user = relationship("User", back_populates="results")
    run = relationship("DriftRun", back_populates="results")
    prompt = relationship("Prompt", back_populates="results")
