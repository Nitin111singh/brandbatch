from __future__ import annotations

import logging
import os
import secrets
import threading

from flask import Flask, abort, g, jsonify, request, session

try:  # optional: read .env in development
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from .models import SessionLocal, init_engine

log = logging.getLogger("brandbatch")


def create_app(test_config: dict | None = None) -> Flask:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = Flask(__name__)
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        if os.environ.get("APP_ENV") == "production":
            raise RuntimeError("SECRET_KEY must be set in production")
        secret = "dev-insecure-" + secrets.token_hex(8)
        log.warning("SECRET_KEY not set; using a random dev key (sessions reset on restart)")
    app.config.update(
        SECRET_KEY=secret,
        DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///brandbatch.db"),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", 2048)) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("APP_ENV") == "production",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
        PUBLIC_URL=os.environ.get("PUBLIC_URL", ""),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "support@example.com"),
        TRUST_PROXY=os.environ.get("TRUST_PROXY", "0") == "1",
        EMBEDDED_WORKER=os.environ.get("EMBEDDED_WORKER", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)

    init_engine(app.config["DATABASE_URL"])

    @app.teardown_appcontext
    def _remove_session(_exc=None):
        SessionLocal.remove()

    # ---------------------------------------------------------------- CSRF
    def csrf_token() -> str:
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return session["_csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _csrf_protect():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        if request.endpoint in app.config.get("CSRF_EXEMPT", {"web.razorpay_webhook"}):
            return
        sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            if request.path.startswith("/api/") or request.is_json:
                return jsonify(error="Your session expired. Reload the page and try again."), 400
            abort(400, description="Your session expired. Go back, reload the page and try again.")

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    if app.config["TRUST_PROXY"]:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # say plainly at startup whether payments will work, so a missing .env is obvious
    from . import billing
    if billing.configured():
        mode = "TEST" if (billing.key_id() or "").startswith("rzp_test") else "LIVE"
        log.info("Razorpay: configured (%s mode, key %s)", mode, billing.key_id())
    else:
        log.warning("Razorpay: NOT configured - set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET "
                    "(in .env, which needs python-dotenv installed). Payments stay disabled until then.")

    from .web import bp
    app.register_blueprint(bp)

    if app.config["EMBEDDED_WORKER"]:
        from .services import run_worker
        threading.Thread(target=run_worker, daemon=True, name="embedded-worker").start()
        log.info("embedded worker thread started")
    return app
