"""Admin CLI.

  python manage.py init-db
  python manage.py set-plan you@example.com agency
  python manage.py reset-password you@example.com
  python manage.py verify-user you@example.com
  python manage.py list-users
  python manage.py list-reports
  python manage.py cleanup
"""
import argparse
import getpass
import os
import sys

from sqlalchemy import select
from werkzeug.security import generate_password_hash

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app.models import AbuseReport, SessionLocal, User, init_engine
from app.plans import PLANS
from app.services import cleanup_expired, requeue_stale


def main(argv=None):
    p = argparse.ArgumentParser(description="BrandBatch admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sp = sub.add_parser("set-plan"); sp.add_argument("email"); sp.add_argument("plan", choices=list(PLANS))
    rp = sub.add_parser("reset-password"); rp.add_argument("email"); rp.add_argument("--password")
    vp = sub.add_parser("verify-user"); vp.add_argument("email")
    sub.add_parser("list-users")
    sub.add_parser("list-reports")
    sub.add_parser("cleanup")
    a = p.parse_args(argv)

    init_engine(os.environ.get("DATABASE_URL", "sqlite:///brandbatch.db"))
    db = SessionLocal()
    if a.cmd == "init-db":
        print("Database tables are ready.")
    elif a.cmd in ("set-plan", "reset-password", "verify-user"):
        user = db.scalar(select(User).where(User.email == a.email.strip().lower()))
        if not user:
            print(f"No user with email {a.email}", file=sys.stderr)
            return 1
        if a.cmd == "set-plan":
            user.plan, user.plan_status = a.plan, "active"
            print(f"{user.email} is now on {PLANS[a.plan].name}")
        elif a.cmd == "verify-user":
            from app.models import utcnow
            user.email_verified_at, user.verify_token_hash = utcnow(), None
            print(f"{user.email} is now verified")
        else:
            pw = a.password or getpass.getpass("New password: ")
            if len(pw) < 8:
                print("Password must be at least 8 characters", file=sys.stderr)
                return 1
            user.password_hash = generate_password_hash(pw)
            print(f"Password reset for {user.email}")
        db.commit()
    elif a.cmd == "list-users":
        for u in db.scalars(select(User).order_by(User.created_at)).all():
            print(f"{u.created_at:%Y-%m-%d}  {u.email:40}  {u.plan:10}  {u.plan_status}")
    elif a.cmd == "list-reports":
        for r in db.scalars(select(AbuseReport).order_by(AbuseReport.created_at.desc())).all():
            print(f"{r.created_at:%Y-%m-%d %H:%M}  [{r.status}]  {r.reporter_email}  {r.content_url}\n    {r.details[:300]}")
    elif a.cmd == "cleanup":
        print(f"requeued stale renders: {requeue_stale(db)}; expired jobs cleaned: {cleanup_expired(db)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
