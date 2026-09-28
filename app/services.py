"""Storage, quota accounting, job creation and the render worker."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, update

from . import engine as eng
from .models import BrandKit, Job, JobInput, MinuteTopup, Render, SessionLocal, User, utcnow
from .plans import get_plan

log = logging.getLogger("brandbatch.worker")

RETENTION_HOURS = int(os.environ.get("RETENTION_HOURS", 48))
STALE_RENDER_MINUTES = 10
BLOCKED_BRAND_WORDS = re.compile(
    r"\b(casino|casinos|betting|bet|bets|sportsbook|satta|matka|rummy|teen\s*patti|poker|jackpot|slots?)\b", re.I)


class QuotaError(Exception):
    pass


class VerificationError(Exception):
    """Raised when an account must confirm its email address first."""


# ------------------------------------------------------------------ anti-abuse
MAX_ACCOUNTS_PER_IP_PER_DAY = int(os.environ.get("MAX_ACCOUNTS_PER_IP_PER_DAY", 3))
FREE_MINUTES_PER_IP_PER_DAY = int(os.environ.get("FREE_MINUTES_PER_IP_PER_DAY", 30))
VERIFY_TOKEN_HOURS = 48
_IP_SALT = os.environ.get("IP_HASH_SALT", "brandbatch-ip-salt")


def _load_disposable_domains() -> set[str]:
    path = Path(__file__).resolve().parent / "disposable_domains.txt"
    domains = set()
    if path.exists():
        domains = {d.strip().lower() for d in path.read_text().split() if d.strip()}
    extra = os.environ.get("DISPOSABLE_EXTRA", "")
    domains |= {d.strip().lower() for d in extra.split(",") if d.strip()}
    return domains


DISPOSABLE_DOMAINS = _load_disposable_domains()


def is_disposable_email(email: str) -> bool:
    domain = (email or "").rsplit("@", 1)[-1].lower().strip()
    if not domain:
        return False
    parts = domain.split(".")
    # match the domain and its parent (mail.tempmail.com -> tempmail.com)
    candidates = {domain} | {".".join(parts[i:]) for i in range(1, max(1, len(parts) - 1))}
    return bool(candidates & DISPOSABLE_DOMAINS)


def hash_ip(ip: str) -> str:
    return hashlib.sha256(f"{_IP_SALT}|{ip or ''}".encode()).hexdigest()


def accounts_from_ip_today(db, ip_hash: str) -> int:
    return db.scalar(select(func.count(User.id)).where(
        User.signup_ip_hash == ip_hash, User.created_at >= utcnow() - timedelta(days=1))) or 0


def free_seconds_from_ip_today(db, ip_hash: str) -> float:
    """Render seconds used in the last 24h by all free accounts that signed up from one IP."""
    return float(db.scalar(
        select(func.coalesce(func.sum(Render.rendered_seconds), 0.0))
        .join(User, User.id == Render.user_id)
        .where(User.signup_ip_hash == ip_hash, User.plan == "free",
               Render.status == "done", Render.finished_at >= utcnow() - timedelta(days=1))) or 0.0)


def new_verification_token(db, user: User) -> str:
    token = secrets.token_urlsafe(32)
    user.verify_token_hash = hashlib.sha256(token.encode()).hexdigest()
    user.verify_sent_at = utcnow()
    db.commit()
    return token


def verify_email_token(db, token: str) -> User | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    user = db.scalar(select(User).where(User.verify_token_hash == digest))
    if not user or not user.verify_sent_at:
        return None
    if user.verify_sent_at < utcnow() - timedelta(hours=VERIFY_TOKEN_HOURS):
        return None
    user.email_verified_at = utcnow()
    user.verify_token_hash = None
    db.commit()
    return user


# ------------------------------------------------------------------ storage
def storage_root() -> Path:
    root = Path(os.environ.get("STORAGE_DIR", Path(__file__).resolve().parent.parent / "storage"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def abs_path(rel: str) -> Path:
    root = storage_root().resolve()
    p = (root / rel).resolve()
    if root not in p.parents and p != root:
        raise ValueError("Invalid storage path")
    return p


def save_upload(file_storage, rel_dir: str, basename: str) -> str:
    ext = Path(file_storage.filename or "").suffix.lower()
    rel = f"{rel_dir}/{basename}{ext}"
    dest = abs_path(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    file_storage.save(dest)
    return rel


def delete_rel(rel: str | None):
    if not rel:
        return
    try:
        abs_path(rel).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ quota
def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage_seconds(db, user: User) -> dict:
    used = db.scalar(select(func.coalesce(func.sum(Render.rendered_seconds), 0.0)).where(
        Render.user_id == user.id, Render.status == "done", Render.finished_at >= month_start())) or 0.0
    reserved = db.scalar(select(func.coalesce(func.sum(Render.estimated_seconds), 0.0)).where(
        Render.user_id == user.id, Render.status.in_(("queued", "running")))) or 0.0
    topup = db.scalar(select(func.coalesce(func.sum(MinuteTopup.minutes), 0)).where(
        MinuteTopup.user_id == user.id, MinuteTopup.status == "paid",
        MinuteTopup.created_at >= month_start())) or 0
    limit = get_plan(user.plan).render_minutes * 60 + int(topup) * 60
    return {"used": float(used), "reserved": float(reserved), "limit": float(limit),
            "topup": float(int(topup) * 60), "remaining": max(0.0, limit - used - reserved)}


def check_brand_name(name: str):
    if BLOCKED_BRAND_WORDS.search(name or ""):
        raise ValueError("Gambling and betting brands aren't allowed under our content policy.")


def kit_to_spec(settings: dict, logo: str | None, intro: str | None, outro: str | None) -> eng.KitSpec:
    s = settings or {}
    return eng.KitSpec(
        logo_path=str(abs_path(logo)) if logo else None,
        position=s.get("position", "bottom-right"),
        scale=float(s.get("scale", 22)), margin=float(s.get("margin", 3)),
        opacity=float(s.get("opacity", 100)), chroma=bool(s.get("chroma", False)),
        chroma_color=s.get("chroma_color", "00ff00"), similarity=float(s.get("similarity", 0.3)),
        blend=float(s.get("blend", 0.08)), loop_logo=bool(s.get("loop_logo", True)),
        intro_path=str(abs_path(intro)) if intro else None,
        outro_path=str(abs_path(outro)) if outro else None,
    )


def safe_stem(name: str, fallback: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_")[:60]
    return stem or fallback


def create_job(db, user: User, files, kit_ids: list[str], formats: list[str],
               quality: str = "balanced", ip_hash: str | None = None) -> Job:
    """Validate everything, store inputs, snapshot kits, reserve quota, queue renders."""
    plan = get_plan(user.plan)
    if not user.email_verified_at:
        raise VerificationError("Confirm your email address before rendering. "
                                "We sent you a link when you signed up.")
    quality = quality or "balanced"
    if quality not in eng.QUALITIES:
        raise ValueError("Unknown render quality.")
    if quality not in plan.qualities:
        raise QuotaError(f"{eng.QUALITIES[quality][0].split(' (')[0]} quality is available on the "
                         f"Agency plan and above. Your {plan.name} plan renders at Balanced quality.")
    if not files:
        raise ValueError("Add at least one video.")
    if len(files) > plan.max_files_per_job:
        raise QuotaError(f"Your {plan.name} plan allows up to {plan.max_files_per_job} videos per job.")
    formats = [f for f in dict.fromkeys(formats) if f in eng.FORMATS]
    if not formats:
        raise ValueError("Choose at least one output format.")
    kits = db.scalars(select(BrandKit).where(BrandKit.user_id == user.id, BrandKit.id.in_(kit_ids or []))).all()
    if not kits or len(kits) != len(set(kit_ids)):
        raise ValueError("Choose at least one of your brand kits.")
    for f in files:
        if Path(f.filename or "").suffix.lower() not in eng.VIDEO_EXT:
            raise ValueError(f"Unsupported video type: {f.filename}")

    job = Job(user_id=user.id, formats=formats,
              expires_at=utcnow() + timedelta(hours=min(RETENTION_HOURS, plan.retention_hours)))
    db.add(job)
    db.flush()
    job_dir = f"jobs/{job.id}"
    try:
        # snapshot kit assets so later kit edits/deletes don't affect this job
        snapshots = []
        for kit in kits:
            snap = {"settings": dict(kit.settings or {})}
            for field in ("logo_file", "intro_file", "outro_file"):
                src = getattr(kit, field)
                if src and abs_path(src).exists():
                    dest = f"{job_dir}/kits/{kit.id}/{field}{Path(src).suffix}"
                    abs_path(dest).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(abs_path(src), abs_path(dest))
                    snap[field] = dest
                else:
                    snap[field] = None
            extra = sum(eng.probe(abs_path(snap[f])).duration or 0 for f in ("intro_file", "outro_file") if snap[f])
            snapshots.append((kit, snap, extra))

        inputs, total_est = [], 0.0
        for i, f in enumerate(files):
            rel = save_upload(f, f"{job_dir}/in", f"{i:04d}")
            try:
                info = eng.probe(abs_path(rel))
            except eng.EngineError:
                raise ValueError(f"'{f.filename}' is not a readable video.")
            if not info.duration:
                raise ValueError(f"Could not read the duration of '{f.filename}'.")
            ji = JobInput(job_id=job.id, position=i, original_name=f.filename[:255], stored_file=rel,
                          duration=info.duration)
            db.add(ji)
            inputs.append(ji)
        db.flush()

        pos, used_names = 0, set()
        renders = []
        for ji in inputs:
            for kit, snap, extra in snapshots:
                for fmt in formats:
                    est = ji.duration + extra
                    total_est += est
                    base = f"{safe_stem(ji.original_name, 'video')}_{safe_stem(kit.name, 'kit')}_{fmt}"
                    name, n = f"{base}.mp4", 2
                    while name in used_names:
                        name, n = f"{base}_{n}.mp4", n + 1
                    used_names.add(name)
                    renders.append(Render(job_id=job.id, user_id=user.id, input_id=ji.id, position=pos,
                                          kit_name=kit.name, kit_snapshot=snap, fmt=fmt, output_name=name,
                                          quality=quality, priority=plan.priority, estimated_seconds=est))
                    pos += 1

        # lock the user row while checking quota so parallel submissions can't overspend
        db.execute(select(User.id).where(User.id == user.id).with_for_update())
        if plan.price_inr == 0 and FREE_MINUTES_PER_IP_PER_DAY and user.signup_ip_hash:
            # stop one person farming the free plan across many accounts from the same network
            ip_used = free_seconds_from_ip_today(db, user.signup_ip_hash)
            if ip_used + total_est > FREE_MINUTES_PER_IP_PER_DAY * 60:
                raise QuotaError(
                    f"Free accounts from your network have used {ip_used / 60:.0f} of "
                    f"{FREE_MINUTES_PER_IP_PER_DAY} free render minutes today. "
                    "Upgrade to a paid plan or try again tomorrow.")
        usage = usage_seconds(db, user)
        if total_est > usage["remaining"]:
            raise QuotaError(
                f"This job needs about {total_est / 60:.1f} render minutes but you have "
                f"{usage['remaining'] / 60:.1f} left this month. Upgrade your plan or remove some videos.")
        db.add_all(renders)
        job.estimated_seconds = total_est
        db.commit()
        return job
    except Exception:
        db.rollback()
        shutil.rmtree(abs_path(job_dir), ignore_errors=True)
        raise


# ------------------------------------------------------------------ worker
def refresh_job_status(db, job_id: str):
    """Recompute a job's status from its renders.

    Several workers can finish renders of the same job at once, so lock the job row and read render
    statuses straight from the database (never from objects cached in this session).
    """
    db.commit()  # end any open transaction so the lock + read below see the latest committed state
    job = db.scalars(select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True)).first()
    if not job or job.status == "expired":
        db.commit()
        return
    states = list(db.scalars(select(Render.status).where(Render.job_id == job_id)))
    if any(s in ("queued", "running") for s in states):
        job.status = "running" if any(s != "queued" for s in states) else "queued"
    else:
        if job.cancel_requested:
            job.status = "cancelled"
        elif any(s == "done" for s in states):
            job.status = "done"  # full or partial success; per-render errors are shown
        else:
            job.status = "failed"
        job.finished_at = job.finished_at or utcnow()
    db.commit()


def claim_next_render(db) -> str | None:
    candidates = db.scalars(select(Render.id).where(Render.status == "queued")
                            .order_by(Render.priority.desc(), Render.created_at, Render.position).limit(5)).all()
    for rid in candidates:
        now = utcnow()
        res = db.execute(update(Render).where(Render.id == rid, Render.status == "queued")
                         .values(status="running", started_at=now, heartbeat_at=now, progress=0))
        db.commit()
        if res.rowcount == 1:
            return rid
    return None


def process_render(render_id: str):
    db = SessionLocal()
    try:
        r = db.get(Render, render_id)
        job = db.get(Job, r.job_id)
        user = db.get(User, r.user_id)
        ji = db.get(JobInput, r.input_id)
        refresh_job_status(db, job.id)
        if job.cancel_requested:
            r.status, r.error = "cancelled", "Cancelled"
            db.commit()
            return
        plan = get_plan(user.plan if user else "free")
        snap = r.kit_snapshot
        out_rel = f"jobs/{job.id}/out/{r.output_name}"
        quality = r.quality if r.quality in eng.QUALITIES else "balanced"
        if quality not in plan.qualities:      # plan downgraded after the job was queued
            quality = "balanced"
        _label, crf, preset = eng.QUALITIES[quality]
        spec = eng.RenderSpec(
            input_path=str(abs_path(ji.stored_file)), output_path=str(abs_path(out_rel)),
            kit=kit_to_spec(snap.get("settings"), snap.get("logo_file"), snap.get("intro_file"), snap.get("outro_file")),
            fmt=r.fmt, max_long_side=plan.max_long_side, free_mark=plan.free_mark,
            crf=crf, preset=preset, max_fps=plan.max_fps,
        )
        last = [0.0]

        def on_progress(pct):
            if time.monotonic() - last[0] > 1.0:
                last[0] = time.monotonic()
                db.execute(update(Render).where(Render.id == render_id)
                           .values(progress=pct, heartbeat_at=utcnow()))
                db.commit()

        def should_cancel():
            return bool(db.scalar(select(Job.cancel_requested).where(Job.id == job.id)))

        try:
            seconds = eng.render(spec, on_progress, should_cancel)
            r = db.get(Render, render_id)
            db.refresh(r)
            r.status, r.progress, r.output_file = "done", 100.0, out_rel
            r.output_size = abs_path(out_rel).stat().st_size
            r.rendered_seconds = seconds
            r.estimated_seconds = 0
        except eng.EngineError as exc:
            db.refresh(r)
            cancelled = str(exc) == "Cancelled"
            r.status = "cancelled" if cancelled else "failed"
            r.error = "Cancelled" if cancelled else str(exc)[-800:]
            r.estimated_seconds = 0
            delete_rel(out_rel)
        finally:
            Path(spec.output_path).with_suffix(".mark.png").unlink(missing_ok=True)
        r.finished_at = utcnow()
        db.commit()
        refresh_job_status(db, job.id)
    except Exception as exc:  # never let one render kill the worker
        log.exception("render %s crashed", render_id)
        db.rollback()
        r = db.get(Render, render_id)
        if r:
            r.status, r.error, r.estimated_seconds, r.finished_at = "failed", f"Internal error: {exc}", 0, utcnow()
            db.commit()
            refresh_job_status(db, r.job_id)
    finally:
        SessionLocal.remove()


def cancel_job(db, job: Job):
    job.cancel_requested = True
    for r in job.renders:
        if r.status == "queued":
            r.status, r.error, r.estimated_seconds, r.finished_at = "cancelled", "Cancelled", 0, utcnow()
    db.commit()
    refresh_job_status(db, job.id)


def requeue_stale(db):
    cutoff = utcnow() - timedelta(minutes=STALE_RENDER_MINUTES)
    res = db.execute(update(Render).where(Render.status == "running", Render.heartbeat_at < cutoff)
                     .values(status="queued", progress=0))
    db.commit()
    return res.rowcount


def cleanup_expired(db) -> int:
    jobs = db.scalars(select(Job).where(Job.expires_at < utcnow(), Job.status != "expired")).all()
    for job in jobs:
        if any(r.status in ("queued", "running") for r in job.renders):
            continue
        shutil.rmtree(abs_path(f"jobs/{job.id}"), ignore_errors=True)
        job.status = "expired"
        for r in job.renders:
            r.output_file = None
    db.commit()
    return len(jobs)


def run_worker(stop_event=None, poll_seconds: float = 1.0):
    log.info("worker started")
    last_maint = 0.0
    while not (stop_event and stop_event.is_set()):
        db = SessionLocal()
        try:
            if time.monotonic() - last_maint > 300:
                last_maint = time.monotonic()
                n = requeue_stale(db)
                c = cleanup_expired(db)
                if n or c:
                    log.info("maintenance: requeued=%s expired=%s", n, c)
            rid = claim_next_render(db)
        except Exception:
            log.exception("worker loop error")
            rid = None
        finally:
            SessionLocal.remove()
        if rid:
            process_render(rid)
        else:
            time.sleep(poll_seconds)
