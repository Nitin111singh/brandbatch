import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MEDIA = Path(__file__).resolve().parent / ".media"


def _ffmpeg():
    from app.engine import FFMPEG
    return FFMPEG


@pytest.fixture(scope="session")
def media():
    """Generate small test media once."""
    MEDIA.mkdir(exist_ok=True)
    F = _ffmpeg()
    items = {
        "reel.mp4": ["-f", "lavfi", "-i", "testsrc2=s=720x1280:r=30:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"],
        "land.webm": ["-f", "lavfi", "-i", "testsrc=s=640x360:r=25:d=3", "-c:v", "libvpx-vp9", "-b:v", "300k"],
        "reel60.mp4": ["-f", "lavfi", "-i", "testsrc2=s=720x1280:r=60:d=3", "-c:v", "libx264",
                       "-pix_fmt", "yuv420p"],
        "reel24.mp4": ["-f", "lavfi", "-i", "testsrc2=s=720x1280:r=24:d=3", "-c:v", "libx264",
                       "-pix_fmt", "yuv420p"],
        "intro.mp4": ["-f", "lavfi", "-i", "color=c=0x223344:s=640x360:r=30:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p"],
        "greenlogo.mp4": ["-f", "lavfi", "-i", "color=c=0x00ff00:s=400x200:r=30:d=2",
                          "-vf", "drawbox=x=40:y=40:w=320:h=120:color=white:t=fill", "-c:v", "libx264", "-pix_fmt", "yuv420p"],
    }
    for name, args in items.items():
        out = MEDIA / name
        if not out.exists():
            subprocess.run([F, "-v", "error", "-y", *args, str(out)], check=True)
    png = MEDIA / "logo.png"
    if not png.exists():
        from PIL import Image, ImageDraw
        im = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
        ImageDraw.Draw(im).rectangle([20, 20, 380, 180], fill=(255, 165, 0, 255))
        im.save(png)
    (MEDIA / "bad.mp4").write_bytes(b"not a video")
    (MEDIA / "notes.txt").write_text("hello")
    return MEDIA


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    for k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    db_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    from app import create_app
    from app.models import Base, SessionLocal, init_engine
    if db_url.startswith("postgres"):
        eng = init_engine(db_url)
        SessionLocal.remove()
        Base.metadata.drop_all(eng)
    application = create_app({"DATABASE_URL": db_url, "SECRET_KEY": "test-secret", "TESTING": True})
    from app import web
    for lim in (web.login_limiter, web.signup_limiter, web.report_limiter):
        lim.hits.clear()
    yield application
    SessionLocal.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def signup(client, email="a@example.com", password="password123", name="Agency A", verified=True):
    token = csrf(client, "/signup")
    r = client.post("/signup", data={"_csrf": token, "email": email, "password": password, "name": name, "terms": "1"})
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    if verified:
        verify(email)
    return token


def verify(email):
    """Mark an account confirmed, as clicking the emailed link would."""
    from sqlalchemy import select
    from app.models import SessionLocal, User, utcnow
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == email))
    u.email_verified_at, u.verify_token_hash = utcnow(), None
    db.commit()
    SessionLocal.remove()


def set_plan(email, plan):
    from sqlalchemy import select
    from app.models import SessionLocal, User
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == email))
    u.plan = plan
    db.commit()
    SessionLocal.remove()
    return u


def run_worker_until_idle(max_loops=200):
    from app.models import SessionLocal
    from app.services import claim_next_render, process_render
    n = 0
    for _ in range(max_loops):
        db = SessionLocal()
        rid = claim_next_render(db)
        SessionLocal.remove()
        if not rid:
            return n
        process_render(rid)
        n += 1
    raise AssertionError("worker did not become idle")
