from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, text, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, scoped_session, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(32), default="free")
    plan_status: Mapped[str] = mapped_column(String(32), default="active")
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    pending_plan: Mapped[str | None] = mapped_column(String(32))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    # anti-abuse: verified email, hashed signup IP (never the raw address)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    verify_token_hash: Mapped[str | None] = mapped_column(String(64))
    verify_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    signup_ip_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    kits: Mapped[list["BrandKit"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class BrandKit(Base):
    __tablename__ = "brand_kits"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    logo_file: Mapped[str | None] = mapped_column(String(255))
    intro_file: Mapped[str | None] = mapped_column(String(255))
    outro_file: Mapped[str | None] = mapped_column(String(255))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="kits")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|failed|cancelled|expired
    formats: Mapped[list] = mapped_column(JSON, default=list)
    estimated_seconds: Mapped[float] = mapped_column(Float, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    inputs: Mapped[list["JobInput"]] = relationship(cascade="all, delete-orphan", order_by="JobInput.position")
    renders: Mapped[list["Render"]] = relationship(cascade="all, delete-orphan", order_by="Render.position")


class JobInput(Base):
    __tablename__ = "job_inputs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_file: Mapped[str] = mapped_column(String(255))
    duration: Mapped[float] = mapped_column(Float)


class Render(Base):
    __tablename__ = "renders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    input_id: Mapped[str] = mapped_column(ForeignKey("job_inputs.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    kit_name: Mapped[str] = mapped_column(String(80))
    kit_snapshot: Mapped[dict] = mapped_column(JSON)  # settings + copied asset paths at job time
    fmt: Mapped[str] = mapped_column(String(16))
    quality: Mapped[str] = mapped_column(String(16), default="balanced")
    output_name: Mapped[str] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|failed|cancelled
    progress: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    output_file: Mapped[str | None] = mapped_column(String(255))
    output_size: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated_seconds: Mapped[float] = mapped_column(Float, default=0)
    rendered_seconds: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class MinuteTopup(Base):
    """A one-time Razorpay order that buys extra render minutes."""
    __tablename__ = "minute_topups"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pack_key: Mapped[str] = mapped_column(String(32))
    minutes: Mapped[int] = mapped_column(Integer)
    amount_paise: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    payment_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="created", index=True)  # created|paid|failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


class AbuseReport(Base):
    __tablename__ = "abuse_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    reporter_email: Mapped[str] = mapped_column(String(254))
    content_url: Mapped[str] = mapped_column(String(1000), default="")
    details: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # provider event id
    event: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- session plumbing
engine = None
SessionLocal = scoped_session(sessionmaker(expire_on_commit=False))


def init_engine(url: str):
    global engine
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragma(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    SessionLocal.remove()
    SessionLocal.configure(bind=engine)
    if engine.dialect.name == "postgresql":
        # several processes (web workers + render workers) may boot at once: serialize schema creation
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(72451901)"))
            Base.metadata.create_all(conn)
    else:
        Base.metadata.create_all(engine)
    return engine
