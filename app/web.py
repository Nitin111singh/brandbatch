from __future__ import annotations

import json
import logging
import re
import shutil
import time
import zipfile
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from . import billing
from . import engine as eng
from .models import AbuseReport, BrandKit, Job, Render, SessionLocal, User, WebhookEvent, utcnow
from .models import MinuteTopup
from .plans import PAID_PLANS, PLANS, TOPUP_PACKS, comparison_rows, get_plan
from . import mailer
from .services import (MAX_ACCOUNTS_PER_IP_PER_DAY, QuotaError, VerificationError, abs_path,
                       accounts_from_ip_today, cancel_job, check_brand_name, create_job, delete_rel,
                       hash_ip, is_disposable_email, new_verification_token, save_upload, usage_seconds,
                       verify_email_token)

bp = Blueprint("web", __name__)
log = logging.getLogger("brandbatch.web")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


# ------------------------------------------------------------------ helpers
@bp.app_context_processor
def inject():
    return {"current_user": getattr(g, "user", None), "PLANS": PLANS, "PAID_PLANS": PAID_PLANS,
            "FORMATS": eng.FORMATS, "POSITIONS": eng.POSITIONS, "QUALITIES": eng.QUALITIES,
            "TOPUP_PACKS": TOPUP_PACKS,
            "comparison_rows": comparison_rows,
            "support_email": current_app.config["SUPPORT_EMAIL"]}


@bp.before_app_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        user = SessionLocal().get(User, uid)
        if user and session.get("pwv") == user.password_hash[-12:]:
            g.user = user
        else:
            session.pop("uid", None)


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not g.user:
            if request.path.startswith("/api/"):
                return jsonify(error="Please log in."), 401
            return redirect(url_for("web.login", next=request.path))
        return view(*a, **kw)
    return wrapper


def valid_id(value: str):
    if not ID_RE.match(value or ""):
        abort(404)


def own_or_404(model, obj_id: str):
    valid_id(obj_id)
    obj = SessionLocal().get(model, obj_id)
    if not obj or obj.user_id != g.user.id:
        abort(404)
    return obj


class RateLimiter:
    def __init__(self, limit: int, window: int):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


login_limiter = RateLimiter(10, 15 * 60)
signup_limiter = RateLimiter(5, 60 * 60)
report_limiter = RateLimiter(5, 60 * 60)
verify_limiter = RateLimiter(3, 30 * 60)
topup_limiter = RateLimiter(10, 60)


def client_ip() -> str:
    # Only trust X-Forwarded-For behind a known proxy (TRUST_PROXY=1), otherwise it can be spoofed.
    if current_app.config.get("TRUST_PROXY"):
        fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if fwd:
            return fwd
    return request.remote_addr or "?"


def start_session(user: User):
    session.clear()
    session.permanent = True
    session["uid"] = user.id
    session["pwv"] = user.password_hash[-12:]


# ------------------------------------------------------------------ public pages
@bp.get("/")
def landing():
    return render_template("landing.html")


@bp.get("/pricing")
def pricing():
    return render_template("pricing.html")


@bp.get("/terms")
def terms():
    return render_template("legal/terms.html")


@bp.get("/privacy")
def privacy():
    return render_template("legal/privacy.html")


@bp.get("/content-policy")
def content_policy():
    return render_template("legal/content_policy.html")


@bp.route("/report", methods=["GET", "POST"])
def report():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        details = request.form.get("details", "").strip()
        url = request.form.get("url", "").strip()[:1000]
        if not EMAIL_RE.match(email) or len(details) < 10:
            flash("Please enter a valid email and describe the issue (at least 10 characters).", "error")
            return render_template("report.html", form=request.form), 400
        if not report_limiter.allow(client_ip()):
            flash("Too many reports from your network. Please try again later.", "error")
            return render_template("report.html", form=request.form), 429
        db = SessionLocal()
        db.add(AbuseReport(reporter_email=email, content_url=url, details=details[:5000]))
        db.commit()
        flash("Thanks. We received your report and will respond within 72 hours.", "ok")
        return redirect(url_for("web.report"))
    return render_template("report.html", form={})


@bp.get("/api/health")
def health():
    try:
        SessionLocal().execute(select(1))
        db_ok = True
    except Exception:
        db_ok = False
    key = billing.key_id() or ""
    razorpay = "not configured" if not billing.configured() else (
        "test" if key.startswith("rzp_test") else "live")
    return jsonify(ok=db_ok, db=db_ok, razorpay=razorpay), (200 if db_ok else 503)


# ------------------------------------------------------------------ auth
@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("web.dashboard"))
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        name = f.get("name", "").strip()[:120]
        pw = f.get("password", "")
        errors = []
        if not EMAIL_RE.match(email):
            errors.append("Enter a valid email address.")
        if len(pw) < 8:
            errors.append("Password must be at least 8 characters.")
        if not f.get("terms"):
            errors.append("Please accept the Terms and Content Policy.")
        db = SessionLocal()
        if not errors and db.scalar(select(User).where(User.email == email)):
            errors.append("An account with this email already exists. Log in instead.")
        if not errors and is_disposable_email(email):
            errors.append("Please use a permanent work email address. Disposable addresses aren't accepted.")
        ip_hash = hash_ip(client_ip())
        if not errors and accounts_from_ip_today(db, ip_hash) >= MAX_ACCOUNTS_PER_IP_PER_DAY:
            errors.append("Too many accounts have been created from your network today. "
                          "Log in to your existing account, or contact support.")
        if not errors and not signup_limiter.allow(client_ip()):
            errors.append("Too many signups from your network. Try again later.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/signup.html", form=f), 400
        user = User(email=email, name=name, password_hash=generate_password_hash(pw),
                    terms_accepted_at=utcnow(), signup_ip_hash=ip_hash)
        db.add(user)
        db.commit()
        start_session(user)
        send_verification(user)
        flash("Welcome! Check your inbox and confirm your email address to start rendering.", "ok")
        return redirect(url_for("web.kit_new"))
    return render_template("auth/signup.html", form={})


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("web.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not login_limiter.allow(f"{client_ip()}|{email}"):
            flash("Too many login attempts. Wait 15 minutes and try again.", "error")
            return render_template("auth/login.html", form=request.form), 429
        user = SessionLocal().scalar(select(User).where(User.email == email))
        if not user or not check_password_hash(user.password_hash, pw):
            flash("Incorrect email or password.", "error")
            return render_template("auth/login.html", form=request.form), 401
        start_session(user)
        nxt = request.args.get("next", "")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = url_for("web.dashboard")
        return redirect(nxt)
    return render_template("auth/login.html", form={})


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.landing"))


@bp.route("/app/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        db = SessionLocal()
        user = db.get(User, g.user.id)
        if not check_password_hash(user.password_hash, request.form.get("current", "")):
            flash("Current password is incorrect.", "error")
        elif len(request.form.get("new", "")) < 8:
            flash("New password must be at least 8 characters.", "error")
        else:
            user.password_hash = generate_password_hash(request.form["new"])
            db.commit()
            start_session(user)
            flash("Password updated.", "ok")
        return redirect(url_for("web.account"))
    return render_template("app/account.html")


def send_verification(user: User) -> bool:
    db = SessionLocal()
    user = db.get(User, user.id)
    token = new_verification_token(db, user)
    base = (current_app.config.get("PUBLIC_URL") or request.url_root).rstrip("/")
    link = f"{base}{url_for('web.verify_email', token=token)}"
    return mailer.send(user.email, "Confirm your email for BrandBatch",
                       f"Hi{' ' + user.name if user.name else ''},\n\n"
                       f"Confirm your email address to start rendering on BrandBatch:\n{link}\n\n"
                       f"The link works for 48 hours. If you didn't create this account, ignore this email.\n")


@bp.get("/verify/<token>")
def verify_email(token):
    user = verify_email_token(SessionLocal(), token)
    if not user:
        flash("That confirmation link is invalid or has expired. Send yourself a new one.", "error")
        return redirect(url_for("web.dashboard") if g.user else url_for("web.login"))
    flash("Email confirmed. You're ready to render.", "ok")
    return redirect(url_for("web.dashboard") if g.user else url_for("web.login"))


@bp.post("/app/resend-verification")
@login_required
def resend_verification():
    if g.user.email_verified_at:
        flash("Your email is already confirmed.", "ok")
    elif not verify_limiter.allow(g.user.id):
        flash("We just sent a link. Check your inbox, and look in spam too.", "error")
    elif send_verification(g.user):
        flash(f"Confirmation link sent to {g.user.email}.", "ok")
    else:
        flash("Email isn't set up on this server yet. Ask support to confirm your account.", "error")
    return redirect(request.referrer or url_for("web.dashboard"))


# ------------------------------------------------------------------ dashboard
@bp.get("/app")
@login_required
def dashboard():
    db = SessionLocal()
    jobs = db.scalars(select(Job).where(Job.user_id == g.user.id).order_by(Job.created_at.desc()).limit(10)).all()
    kits = db.scalars(select(BrandKit).where(BrandKit.user_id == g.user.id).order_by(BrandKit.created_at)).all()
    return render_template("app/dashboard.html", jobs=jobs, kits=kits, usage=usage_seconds(db, g.user),
                           plan=get_plan(g.user.plan))


# ------------------------------------------------------------------ brand kits
def _kit_settings_from_form(f) -> dict:
    def num(name, default, lo, hi):
        raw = f.get(name, "")
        try:
            v = float(raw) if raw != "" else default
        except ValueError:
            raise ValueError(f"{name} must be a number")
        if not lo <= v <= hi:
            raise ValueError(f"{name} must be between {lo} and {hi}")
        return v

    position = f.get("position", "bottom-right")
    if position not in eng.POSITIONS:
        raise ValueError("Invalid position")
    color = f.get("chroma_color", "#00ff00")
    if not HEX_RE.match(color):
        raise ValueError("Invalid key color")
    return {
        "position": position,
        "scale": num("scale", 22, 3, 100),
        "margin": num("margin", 3, 0, 30),
        "opacity": num("opacity", 100, 5, 100),
        "chroma": "1" in f.getlist("chroma"),
        "chroma_color": color.lstrip("#").lower(),
        "similarity": num("similarity", 0.3, 0.01, 1),
        "blend": num("blend", 0.08, 0, 1),
        "loop_logo": "1" in f.getlist("loop_logo"),
    }


def _save_kit_assets(kit: BrandKit, files) -> tuple[list[str], list[str]]:
    """Store uploaded assets. Returns (new files, replaced files). Caller deletes replaced files only
    after commit, and new files on failure."""
    rules = {"logo": IMAGE_EXT | eng.LOGO_VIDEO_EXT, "intro": eng.VIDEO_EXT, "outro": eng.VIDEO_EXT}
    new, replaced = [], []
    try:
        for field, allowed in rules.items():
            fs = files.get(field)
            if fs and fs.filename:
                ext = Path(fs.filename).suffix.lower()
                if ext not in allowed:
                    raise ValueError(f"Unsupported {field} file type '{ext}'.")
                stamp = str(int(time.time() * 1000))
                rel = save_upload(fs, f"users/{kit.user_id}/kits/{kit.id}", f"{field}_{stamp}")
                new.append(rel)
                try:
                    info = eng.probe(abs_path(rel))
                except eng.EngineError:
                    raise ValueError(f"The {field} file isn't a readable image or video.")
                if field != "logo":
                    if not info.duration:
                        raise ValueError(f"The {field} file isn't a readable video.")
                    if info.duration > 30:
                        raise ValueError(f"The {field} clip must be 30 seconds or shorter.")
                if getattr(kit, f"{field}_file"):
                    replaced.append(getattr(kit, f"{field}_file"))
                setattr(kit, f"{field}_file", rel)
            elif request.form.get(f"remove_{field}") == "1" and field != "logo" and getattr(kit, f"{field}_file"):
                replaced.append(getattr(kit, f"{field}_file"))
                setattr(kit, f"{field}_file", None)
    except ValueError:
        for rel in new:
            delete_rel(rel)
        raise
    return new, replaced


@bp.get("/app/kits")
@login_required
def kits():
    db = SessionLocal()
    items = db.scalars(select(BrandKit).where(BrandKit.user_id == g.user.id).order_by(BrandKit.created_at)).all()
    return render_template("app/kits.html", kits=items, plan=get_plan(g.user.plan))


@bp.route("/app/kits/new", methods=["GET", "POST"])
@login_required
def kit_new():
    db = SessionLocal()
    plan = get_plan(g.user.plan)
    count = len(db.scalars(select(BrandKit.id).where(BrandKit.user_id == g.user.id)).all())
    if count >= plan.brand_kits:
        flash(f"Your {plan.name} plan includes {plan.brand_kits} brand kit(s). Upgrade to add more.", "error")
        return redirect(url_for("web.billing_page"))
    if request.method == "POST":
        kit = BrandKit(user_id=g.user.id, name=request.form.get("name", "").strip()[:80])
        try:
            if not kit.name:
                raise ValueError("Give the brand kit a name.")
            check_brand_name(kit.name)
            kit.settings = _kit_settings_from_form(request.form)
            if not (request.files.get("logo") and request.files["logo"].filename):
                raise ValueError("Upload a logo.")
            db.add(kit)
            db.flush()
            _save_kit_assets(kit, request.files)
            db.commit()
        except ValueError as exc:
            db.rollback()
            if kit.id:
                shutil.rmtree(abs_path(f"users/{g.user.id}/kits/{kit.id}"), ignore_errors=True)
            flash(str(exc), "error")
            return render_template("app/kit_form.html", kit=None, form=request.form), 400
        flash(f"Brand kit '{kit.name}' saved.", "ok")
        return redirect(url_for("web.kits"))
    return render_template("app/kit_form.html", kit=None, form={})


@bp.route("/app/kits/<kit_id>/edit", methods=["GET", "POST"])
@login_required
def kit_edit(kit_id):
    kit = own_or_404(BrandKit, kit_id)
    db = SessionLocal()
    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()[:80]
            if not name:
                raise ValueError("Give the brand kit a name.")
            check_brand_name(name)
            kit.name = name
            kit.settings = _kit_settings_from_form(request.form)
            new, replaced = _save_kit_assets(kit, request.files)
            try:
                db.commit()
            except Exception:
                for rel in new:
                    delete_rel(rel)
                raise
            for rel in replaced:
                delete_rel(rel)
        except ValueError as exc:
            db.rollback()
            db.refresh(kit)
            flash(str(exc), "error")
            return render_template("app/kit_form.html", kit=kit, form=request.form), 400
        flash("Brand kit updated.", "ok")
        return redirect(url_for("web.kits"))
    return render_template("app/kit_form.html", kit=kit, form={})


@bp.post("/app/kits/<kit_id>/delete")
@login_required
def kit_delete(kit_id):
    kit = own_or_404(BrandKit, kit_id)
    db = SessionLocal()
    shutil.rmtree(abs_path(f"users/{kit.user_id}/kits/{kit.id}"), ignore_errors=True)
    db.delete(kit)
    db.commit()
    flash("Brand kit deleted.", "ok")
    return redirect(url_for("web.kits"))


@bp.get("/app/kits/<kit_id>/asset/<field>")
@login_required
def kit_asset(kit_id, field):
    kit = own_or_404(BrandKit, kit_id)
    if field not in ("logo", "intro", "outro"):
        abort(404)
    rel = getattr(kit, f"{field}_file")
    if not rel or not abs_path(rel).exists():
        abort(404)
    return send_file(abs_path(rel), conditional=True, max_age=0)


# ------------------------------------------------------------------ jobs
@bp.get("/app/jobs")
@login_required
def jobs():
    db = SessionLocal()
    items = db.scalars(select(Job).where(Job.user_id == g.user.id).order_by(Job.created_at.desc()).limit(100)).all()
    return render_template("app/jobs.html", jobs=items)


@bp.get("/app/jobs/new")
@login_required
def job_new():
    db = SessionLocal()
    kits_ = db.scalars(select(BrandKit).where(BrandKit.user_id == g.user.id).order_by(BrandKit.created_at)).all()
    if not kits_:
        flash("Create a brand kit first.", "error")
        return redirect(url_for("web.kit_new"))
    return render_template("app/job_new.html", kits=kits_, usage=usage_seconds(db, g.user), plan=get_plan(g.user.plan))


@bp.post("/api/jobs")
@login_required
def api_job_create():
    db = SessionLocal()
    user = db.get(User, g.user.id)
    try:
        job = create_job(db, user, [f for f in request.files.getlist("videos") if f and f.filename],
                         request.form.getlist("kits"), request.form.getlist("formats"),
                         request.form.get("quality", "balanced"), ip_hash=hash_ip(client_ip()))
    except VerificationError as exc:
        return jsonify(error=str(exc), verify=url_for("web.resend_verification")), 403
    except QuotaError as exc:
        return jsonify(error=str(exc), upgrade=url_for("web.billing_page")), 402
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(id=job.id, url=url_for("web.job_detail", job_id=job.id)), 201


def job_json(job: Job) -> dict:
    return {
        "id": job.id, "status": job.status, "formats": job.formats,
        "created_at": job.created_at.isoformat() + "Z", "expires_at": job.expires_at.isoformat() + "Z",
        "renders": [{
            "id": r.id, "name": r.output_name, "kit": r.kit_name, "format": r.fmt,
            "quality": r.quality, "status": r.status,
            "progress": round(r.progress, 1), "error": r.error, "size": r.output_size,
            "download": url_for("web.render_download", job_id=job.id, render_id=r.id)
            if r.status == "done" and r.output_file else None,
        } for r in job.renders],
    }


@bp.get("/app/jobs/<job_id>")
@login_required
def job_detail(job_id):
    job = own_or_404(Job, job_id)
    return render_template("app/job_detail.html", job=job, data=job_json(job))


@bp.get("/api/jobs/<job_id>")
@login_required
def api_job(job_id):
    job = own_or_404(Job, job_id)
    SessionLocal().refresh(job)
    return jsonify(job_json(job))


@bp.post("/api/jobs/<job_id>/cancel")
@login_required
def api_job_cancel(job_id):
    job = own_or_404(Job, job_id)
    cancel_job(SessionLocal(), job)
    return jsonify(job_json(job))


@bp.get("/app/jobs/<job_id>/renders/<render_id>")
@login_required
def render_download(job_id, render_id):
    job = own_or_404(Job, job_id)
    valid_id(render_id)
    r = SessionLocal().get(Render, render_id)
    if not r or r.job_id != job.id or r.status != "done" or not r.output_file or not abs_path(r.output_file).exists():
        abort(404)
    return send_file(abs_path(r.output_file), mimetype="video/mp4", conditional=True,
                     as_attachment=request.args.get("inline") != "1", download_name=r.output_name)


@bp.get("/app/jobs/<job_id>/zip")
@login_required
def job_zip(job_id):
    job = own_or_404(Job, job_id)
    done = [r for r in job.renders if r.status == "done" and r.output_file and abs_path(r.output_file).exists()]
    if not done:
        abort(404)
    zip_path = abs_path(f"jobs/{job.id}/brandbatch_{job.id[:8]}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for r in done:
            zf.write(abs_path(r.output_file), arcname=r.output_name)
    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name=zip_path.name)


# ------------------------------------------------------------------ billing
@bp.get("/app/billing")
@login_required
def billing_page():
    db = SessionLocal()
    return render_template("app/billing.html", plan=get_plan(g.user.plan), usage=usage_seconds(db, g.user),
                           billing_ready=billing.configured())


@bp.post("/app/billing/subscribe/<plan_key>")
@login_required
def billing_subscribe(plan_key):
    plan = PLANS.get(plan_key)
    if not plan or plan.price_inr == 0:
        abort(404)
    if not billing.configured() or not plan.razorpay_plan_id:
        flash("Online payments aren't set up yet. Contact support to upgrade.", "error")
        return redirect(url_for("web.billing_page"))
    db = SessionLocal()
    user = db.get(User, g.user.id)
    try:
        sub = billing.create_subscription(plan.razorpay_plan_id, user.id, plan.key, user.email)
    except billing.BillingError as exc:
        log.error("subscription create failed: %s", exc)
        flash("We couldn't start checkout. Please try again in a minute.", "error")
        return redirect(url_for("web.billing_page"))
    user.pending_plan = plan.key
    db.commit()
    session["pending_sub"] = sub["id"]
    return render_template("app/checkout.html", plan=plan, subscription_id=sub["id"], key_id=billing.key_id())


@bp.post("/app/billing/verify")
@login_required
def billing_verify():
    f = request.form
    sub_id = f.get("razorpay_subscription_id", "")
    if sub_id != session.get("pending_sub") or not billing.verify_payment_signature(
            f.get("razorpay_payment_id", ""), sub_id, f.get("razorpay_signature", "")):
        flash("Payment verification failed. If money was deducted, contact support.", "error")
        return redirect(url_for("web.billing_page"))
    db = SessionLocal()
    user = db.get(User, g.user.id)
    if user.pending_plan in PLANS:
        old_sub = user.razorpay_subscription_id
        if old_sub and old_sub != sub_id:
            try:
                billing.cancel_subscription(old_sub, at_cycle_end=False)
            except billing.BillingError as exc:
                log.error("could not cancel previous subscription %s: %s", old_sub, exc)
        user.plan, user.plan_status = user.pending_plan, "active"
        user.razorpay_subscription_id, user.pending_plan = sub_id, None
        db.commit()
    session.pop("pending_sub", None)
    flash(f"You're on the {get_plan(user.plan).name} plan. Thank you!", "ok")
    return redirect(url_for("web.dashboard"))


@bp.post("/app/billing/cancel")
@login_required
def billing_cancel():
    db = SessionLocal()
    user = db.get(User, g.user.id)
    if not user.razorpay_subscription_id:
        flash("You don't have an active subscription.", "error")
        return redirect(url_for("web.billing_page"))
    try:
        billing.cancel_subscription(user.razorpay_subscription_id, at_cycle_end=True)
    except billing.BillingError as exc:
        log.error("cancel failed: %s", exc)
        flash("We couldn't cancel right now. Please try again or contact support.", "error")
        return redirect(url_for("web.billing_page"))
    user.plan_status = "cancelling"
    db.commit()
    flash("Your subscription will end at the close of this billing period.", "ok")
    return redirect(url_for("web.billing_page"))


# ---- one-time top-up purchases (Razorpay Standard Checkout, Orders API)
@bp.post("/api/create-order")
@login_required
def create_order():
    """Create a Razorpay order for a render-minute pack and return its id to the browser."""
    pack = TOPUP_PACKS.get((request.get_json(silent=True) or request.form).get("pack", ""))
    if not pack:
        return jsonify(error="Unknown pack."), 400
    if not billing.configured():
        return jsonify(error="Online payments aren't set up on this server yet."), 503
    if pack.amount_paise < 100:
        return jsonify(error="Amount must be at least Rs 1."), 400
    if not topup_limiter.allow(g.user.id):
        return jsonify(error="Too many payment attempts. Wait a minute and try again."), 429

    db = SessionLocal()
    topup = MinuteTopup(user_id=g.user.id, pack_key=pack.key, minutes=pack.minutes,
                        amount_paise=pack.amount_paise, order_id="pending")
    try:
        order = billing.create_order(
            pack.amount_paise, receipt=f"topup-{g.user.id[:8]}-{int(time.time())}",
            notes={"user_id": g.user.id, "pack": pack.key, "minutes": str(pack.minutes)})
    except billing.AuthError as exc:
        log.error("razorpay auth failed: %s", exc)
        return jsonify(error="Payment provider rejected our credentials. Please contact support."), 401
    except billing.BillingError as exc:
        log.error("order create failed: %s", exc)
        return jsonify(error="We couldn't start the payment. Please try again."), 500

    topup.order_id = order["id"]
    db.add(topup)
    db.commit()
    return jsonify(order_id=order["id"], amount=order["amount"], currency=order.get("currency", "INR"),
                   key_id=billing.key_id(), minutes=pack.minutes), 201


@bp.post("/api/verify-payment")
@login_required
def verify_payment():
    """Verify the Standard Checkout signature and credit the minutes exactly once."""
    data = request.get_json(silent=True) or request.form
    order_id = data.get("razorpay_order_id", "")
    payment_id = data.get("razorpay_payment_id", "")
    signature = data.get("razorpay_signature", "")
    if not (order_id and payment_id and signature):
        return jsonify(error="Missing payment details."), 400

    db = SessionLocal()
    topup = db.scalar(select(MinuteTopup).where(MinuteTopup.order_id == order_id))
    if not topup or topup.user_id != g.user.id:
        return jsonify(error="Unknown order."), 404
    if topup.status == "paid":                      # replayed call: already credited
        return jsonify(ok=True, minutes=topup.minutes, already_credited=True)

    if not billing.verify_order_signature(order_id, payment_id, signature):
        topup.status = "failed"
        db.commit()
        log.warning("signature mismatch for order %s (user %s)", order_id, g.user.id)
        return jsonify(error="Payment could not be verified. If money was deducted, contact support."), 400

    topup.status, topup.payment_id, topup.paid_at = "paid", payment_id[:64], utcnow()
    db.commit()
    log.info("topup paid: %s minutes for user %s", topup.minutes, g.user.id)
    return jsonify(ok=True, minutes=topup.minutes)


@bp.post("/billing/webhook")
def razorpay_webhook():
    body = request.get_data()
    if not billing.verify_webhook_signature(body, request.headers.get("X-Razorpay-Signature", "")):
        return jsonify(error="invalid signature"), 400
    try:
        payload = json.loads(body)
    except ValueError:
        return jsonify(error="invalid json"), 400
    event_id = request.headers.get("X-Razorpay-Event-Id") or f"{payload.get('event')}:{payload.get('created_at')}"
    db = SessionLocal()
    if db.get(WebhookEvent, event_id):
        return jsonify(ok=True, duplicate=True)
    event = payload.get("event", "")
    sub = (payload.get("payload", {}).get("subscription", {}) or {}).get("entity", {}) or {}
    notes = sub.get("notes") or {}
    user = None
    if sub.get("id"):
        user = db.scalar(select(User).where(User.razorpay_subscription_id == sub["id"]))
    if not user and notes.get("user_id") and ID_RE.match(str(notes["user_id"])):
        user = db.get(User, notes["user_id"])
    if user:
        plan_key = notes.get("plan") if notes.get("plan") in PLANS else user.plan
        if event in ("subscription.activated", "subscription.charged", "subscription.resumed"):
            user.plan, user.plan_status, user.razorpay_subscription_id = plan_key, "active", sub.get("id")
            user.pending_plan = None
        elif sub.get("id") != user.razorpay_subscription_id:
            pass  # event for an old/replaced subscription: ignore
        elif event in ("subscription.pending", "subscription.halted"):
            user.plan_status = "payment_issue"
            if event == "subscription.halted":
                user.plan = "free"
        elif event in ("subscription.cancelled", "subscription.completed", "subscription.expired"):
            user.plan, user.plan_status, user.razorpay_subscription_id = "free", "active", None
    db.add(WebhookEvent(id=event_id[:128], event=event[:64]))
    db.commit()
    return jsonify(ok=True)


@bp.app_errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify(error="Not found"), 404
    return render_template("error.html", code=404, message="We couldn't find that page."), 404


@bp.app_errorhandler(400)
def bad_request(e):
    if request.path.startswith("/api/"):
        return jsonify(error=getattr(e, "description", "Bad request")), 400
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request")), 400


@bp.app_errorhandler(413)
def too_large(e):
    msg = "Upload too large."
    if request.path.startswith("/api/"):
        return jsonify(error=msg), 413
    return render_template("error.html", code=413, message=msg), 413


@bp.app_errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify(error="Something went wrong on our side."), 500
    return render_template("error.html", code=500, message="Something went wrong on our side."), 500
