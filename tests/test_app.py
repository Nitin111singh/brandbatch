import hashlib
import hmac
import io
import json
import re
import subprocess
import zipfile
from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import csrf, run_worker_until_idle, set_plan, signup


def _file(media, name, as_name=None):
    return (open(media / name, "rb"), as_name or name)


def create_kit(client, token, media, name="Client One", logo="logo.png", **extra):
    data = {"_csrf": token, "name": name, "position": "top-right", "scale": "30", "margin": "3",
            "opacity": "100", "chroma_color": "#00ff00", "similarity": "0.3", "blend": "0.08",
            "loop_logo": ["0", "1"], "chroma": ["0"], "logo": _file(media, logo)}
    data.update(extra)
    return client.post("/app/kits/new", data=data, content_type="multipart/form-data")


def kit_ids(email):
    from app.models import BrandKit, SessionLocal, User
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == email))
    ids = [k.id for k in db.scalars(select(BrandKit).where(BrandKit.user_id == u.id).order_by(BrandKit.created_at))]
    SessionLocal.remove()
    return ids


def probe(path):
    from app.engine import probe as p
    return p(path)


# ---------------------------------------------------------------- public + auth
def test_public_pages_and_headers(client):
    for path in ["/", "/pricing", "/terms", "/privacy", "/content-policy", "/report", "/login", "/signup"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["X-Frame-Options"] == "DENY"
    health = client.get("/api/health").get_json()
    assert health["ok"] is True and health["db"] is True
    assert health["razorpay"] in ("not configured", "test", "live")
    assert client.get("/nope").status_code == 404
    assert client.get("/app").status_code == 302
    assert client.get("/api/jobs/" + "a" * 32).status_code == 401
    body = client.get("/").get_data(as_text=True)
    assert "₹1,999" in body and "Agency" in body


def test_csrf_required(client):
    r = client.post("/signup", data={"email": "x@example.com", "password": "password123", "terms": "1"})
    assert r.status_code == 400


def test_signup_login_logout(client):
    token = csrf(client, "/signup")
    r = client.post("/signup", data={"_csrf": token, "email": "bad", "password": "short"})
    assert r.status_code == 400 and "valid email" in r.get_data(as_text=True)
    signup(client)
    assert client.get("/app").status_code == 200
    token = csrf(client, "/app")
    client.post("/logout", data={"_csrf": token})
    assert client.get("/app").status_code == 302
    # duplicate
    token = csrf(client, "/signup")
    r = client.post("/signup", data={"_csrf": token, "email": "A@example.com", "password": "password123", "terms": "1"})
    assert r.status_code == 400 and "already exists" in r.get_data(as_text=True)
    token = csrf(client, "/login")
    assert client.post("/login", data={"_csrf": token, "email": "a@example.com", "password": "wrongpass"}).status_code == 401
    r = client.post("/login?next=//evil.com", data={"_csrf": token, "email": "a@example.com", "password": "password123"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/app")


def test_login_rate_limit(client):
    signup(client)
    client.post("/logout", data={"_csrf": csrf(client, "/app")})
    token = csrf(client, "/login")
    codes = [client.post("/login", data={"_csrf": token, "email": "a@example.com", "password": "nope-nope"}).status_code
             for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_password_change_invalidates_other_sessions(app, media):
    c1, c2 = app.test_client(), app.test_client()
    signup(c1)
    token = csrf(c2, "/login")
    c2.post("/login", data={"_csrf": token, "email": "a@example.com", "password": "password123"})
    assert c2.get("/app").status_code == 200
    t1 = csrf(c1, "/app/account")
    r = c1.post("/app/account", data={"_csrf": t1, "current": "password123", "new": "newpassword456"})
    assert r.status_code == 302
    assert c1.get("/app").status_code == 200
    assert c2.get("/app").status_code == 302


# ---------------------------------------------------------------- brand kits
def test_kit_rules(client, media):
    token = signup(client)
    token = csrf(client, "/app/kits/new")
    r = client.post("/app/kits/new", data={"_csrf": token, "name": "No logo"}, content_type="multipart/form-data")
    assert r.status_code == 400 and "Upload a logo" in r.get_data(as_text=True)
    r = create_kit(client, token, media, name="Royal Casino")
    assert r.status_code == 400 and "Gambling" in r.get_data(as_text=True)
    r = create_kit(client, token, media, logo="notes.txt")
    assert r.status_code == 400
    r = create_kit(client, token, media, logo="bad.mp4")
    assert r.status_code == 400 and "readable" in r.get_data(as_text=True)
    r = create_kit(client, token, media, name="Alphabet Foods")  # 'bet' inside a word must not be blocked
    assert r.status_code == 302
    # free plan = 1 kit
    r = client.get("/app/kits/new")
    assert r.status_code == 302 and "/app/billing" in r.headers["Location"]
    assert len(kit_ids("a@example.com")) == 1


def test_kit_edit_replace_and_failed_edit(client, media, app):
    from app.models import BrandKit, SessionLocal
    from app.services import abs_path
    token = signup(client)
    set_plan("a@example.com", "agency")
    token = csrf(client, "/app/kits/new")
    assert create_kit(client, token, media).status_code == 302
    kid = kit_ids("a@example.com")[0]
    old_logo = SessionLocal().get(BrandKit, kid).logo_file
    SessionLocal.remove()
    # failed edit (bad intro) keeps old logo file + db value
    r = client.post(f"/app/kits/{kid}/edit", content_type="multipart/form-data", data={
        "_csrf": token, "name": "Client One", "position": "center", "logo": _file(media, "logo.png", "new.png"),
        "intro": _file(media, "bad.mp4")})
    assert r.status_code == 400
    kit = SessionLocal().get(BrandKit, kid)
    assert kit.logo_file == old_logo and abs_path(old_logo).exists() and kit.settings["position"] == "top-right"
    SessionLocal.remove()
    # successful edit replaces logo and removes old file, adds intro
    r = client.post(f"/app/kits/{kid}/edit", content_type="multipart/form-data", data={
        "_csrf": token, "name": "Client One v2", "position": "center", "chroma": ["0", "1"], "loop_logo": ["0"],
        "logo": _file(media, "greenlogo.mp4"), "intro": _file(media, "intro.mp4")})
    assert r.status_code == 302
    kit = SessionLocal().get(BrandKit, kid)
    assert kit.name == "Client One v2" and kit.settings["position"] == "center"
    assert kit.settings["chroma"] is True and kit.settings["loop_logo"] is False
    assert not abs_path(old_logo).exists() and abs_path(kit.logo_file).exists() and kit.intro_file
    assert client.get(f"/app/kits/{kid}/asset/logo").status_code == 200
    SessionLocal.remove()
    # remove intro
    r = client.post(f"/app/kits/{kid}/edit", content_type="multipart/form-data",
                    data={"_csrf": token, "name": "Client One v2", "position": "center", "remove_intro": "1"})
    assert r.status_code == 302 and SessionLocal().get(BrandKit, kid).intro_file is None
    SessionLocal.remove()
    # delete
    client.post(f"/app/kits/{kid}/delete", data={"_csrf": token})
    assert kit_ids("a@example.com") == []
    assert not abs_path(f"users/{kit.user_id}/kits/{kid}").exists()


# ---------------------------------------------------------------- jobs end to end
def submit(client, token, media, videos, kits, formats):
    data = {"videos": [_file(media, v) for v in videos], "kits": kits, "formats": formats}
    return client.post("/api/jobs", data=data, content_type="multipart/form-data", headers={"X-CSRF-Token": token})


def test_full_job_flow(client, media, app):
    from app.models import Job, SessionLocal
    from app.services import abs_path
    token = signup(client)
    set_plan("a@example.com", "agency")
    token = csrf(client, "/app/kits/new")
    assert create_kit(client, token, media, name="Client One", intro=_file(media, "intro.mp4")).status_code == 302
    assert create_kit(client, token, media, name="Client Two", logo="greenlogo.mp4", chroma=["0", "1"],
                      position="center").status_code == 302
    k1, k2 = kit_ids("a@example.com")
    assert submit(client, "wrong", media, ["reel.mp4"], [k1], ["9x16"]).status_code == 400  # csrf
    r = submit(client, token, media, ["reel.mp4", "land.webm"], [k1, k2], ["9x16", "16x9"])
    assert r.status_code == 201, r.get_json()
    job_id = r.get_json()["id"]
    j = client.get(f"/api/jobs/{job_id}").get_json()
    assert j["status"] == "queued" and len(j["renders"]) == 8
    # quota reserved while queued
    assert "reserved" in client.get("/app").get_data(as_text=True)

    assert run_worker_until_idle() == 8
    j = client.get(f"/api/jobs/{job_id}").get_json()
    assert j["status"] == "done", j
    assert all(x["status"] == "done" and x["download"] for x in j["renders"])
    names = [x["name"] for x in j["renders"]]
    assert len(set(names)) == 8 and "reel_Client_One_9x16.mp4" in names

    by_name = {x["name"]: x for x in j["renders"]}
    out = app.config  # noqa
    r916 = client.get(by_name["reel_Client_One_9x16.mp4"]["download"])
    assert r916.status_code == 200 and r916.headers["Content-Type"] == "video/mp4"
    tmp = abs_path("check916.mp4")
    tmp.write_bytes(r916.data)
    info = probe(tmp)
    assert (info.width, info.height) == (1080, 1920) and abs(info.duration - 5.0) < 0.2 and info.has_audio  # 1s intro + 4s
    r169 = client.get(by_name["land_Client_Two_16x9.mp4"]["download"])
    tmp.write_bytes(r169.data)
    info = probe(tmp)
    assert (info.width, info.height) == (1920, 1080) and abs(info.duration - 3.0) < 0.2
    assert client.get(by_name["land_Client_Two_16x9.mp4"]["download"] + "?inline=1").headers[
        "Content-Disposition"].startswith("inline")

    z = client.get(f"/app/jobs/{job_id}/zip")
    assert sorted(zipfile.ZipFile(io.BytesIO(z.data)).namelist()) == sorted(names)
    # usage: 2x(5+4) + 2x(3+1) intro for kit1; kit2 no intro: 2x4 + 2x3  => (5+5+4+4) + (4+4+3+3) = 32s
    from app.models import User
    from app.services import usage_seconds
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == "a@example.com"))
    usage = usage_seconds(db, u)
    assert abs(usage["used"] - 32) < 1 and usage["reserved"] == 0
    SessionLocal.remove()
    assert client.get("/app/jobs").status_code == 200
    assert client.get(f"/app/jobs/{job_id}").status_code == 200


def test_free_plan_limits_and_mark(client, media, app):
    from app.models import Render, SessionLocal, User
    from app.models import utcnow
    from app.services import abs_path
    token = signup(client)
    token = csrf(client, "/app/kits/new")
    create_kit(client, token, media)
    (kid,) = kit_ids("a@example.com")
    # too many files for free (5)
    r = submit(client, token, media, ["reel.mp4"] * 6, [kid], ["original"])
    assert r.status_code == 402 and "5 videos" in r.get_json()["error"]
    r = submit(client, token, media, ["reel.mp4"], [kid], ["original"])
    assert r.status_code == 201
    run_worker_until_idle()
    j = client.get(f"/api/jobs/{r.get_json()['id']}").get_json()
    data = client.get(j["renders"][0]["download"]).data
    tmp = abs_path("free.mp4")
    tmp.write_bytes(data)
    info = probe(tmp)
    assert (info.width, info.height) == (720, 1280)  # capped at 1280 long side
    # quota: fake heavy usage so a 4s job exceeds 10 min
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == "a@example.com"))
    base = db.scalars(select(Render)).first()
    db.add(Render(job_id=base.job_id, user_id=u.id, input_id=base.input_id, position=99, kit_name="x",
                  kit_snapshot={}, fmt="original", output_name="x.mp4", status="done", rendered_seconds=598,
                  finished_at=utcnow()))
    db.commit()
    SessionLocal.remove()
    r = submit(client, token, media, ["reel.mp4"], [kid], ["original"])
    assert r.status_code == 402 and "render minutes" in r.get_json()["error"]


def test_free_mark_is_rendered(media, tmp_path):
    from app import engine as eng
    F = eng.FFMPEG
    for mark in (False, True):
        out = tmp_path / f"m{mark}.mp4"
        eng.render(eng.RenderSpec(str(media / "reel.mp4"), str(out), eng.KitSpec(), fmt="original",
                                  max_long_side=1280, free_mark=mark))
        raw = subprocess.run([F, "-v", "error", "-ss", "1", "-i", str(out), "-vf", "format=gray,crop=iw:40:0:ih-40",
                              "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True).stdout
        (tmp_path / f"strip{mark}").write_bytes(raw)
    a, b = (tmp_path / "stripFalse").read_bytes(), (tmp_path / "stripTrue").read_bytes()
    assert len(a) == len(b) and sum(abs(x - y) for x, y in zip(a, b)) / len(a) > 5


def test_green_screen_keyed(media, tmp_path):
    from app import engine as eng
    out = tmp_path / "k.mp4"
    kit = eng.KitSpec(logo_path=str(media / "greenlogo.mp4"), chroma=True, position="top-left", scale=50, margin=0)
    eng.render(eng.RenderSpec(str(media / "reel.mp4"), str(out), kit, fmt="original"))

    def px(x, y):
        return tuple(subprocess.run([eng.FFMPEG, "-v", "error", "-ss", "1", "-i", str(out), "-vf",
                                     f"format=rgb24,crop=1:1:{x}:{y}", "-frames:v", "1", "-f", "rawvideo", "-"],
                                    capture_output=True).stdout)
    # logo 360x180 at 0,0; white box from (36,36); green border at (10,10) must be keyed out
    assert all(c > 230 for c in px(180, 90))
    g = px(10, 10)
    assert not (g[1] > 200 and g[0] < 60 and g[2] < 60)


def test_invalid_upload_cleans_up(client, media, app):
    from app.models import Job, SessionLocal
    from app.services import abs_path
    token = signup(client)
    token = csrf(client, "/app/kits/new")
    create_kit(client, token, media)
    (kid,) = kit_ids("a@example.com")
    r = submit(client, token, media, ["reel.mp4", "bad.mp4"], [kid], ["9x16"])
    assert r.status_code == 400 and "readable" in r.get_json()["error"]
    assert SessionLocal().scalars(select(Job)).all() == []
    jobs_dir = abs_path("jobs")
    assert not jobs_dir.exists() or list(jobs_dir.iterdir()) == []
    assert submit(client, token, media, ["notes.txt"], [kid], ["9x16"]).status_code == 400
    assert submit(client, token, media, ["reel.mp4"], ["f" * 32], ["9x16"]).status_code == 400
    assert submit(client, token, media, ["reel.mp4"], [kid], ["4x3"]).status_code == 400


def test_cancel_expire_and_stale(client, media, app):
    from app.models import Job, Render, SessionLocal, utcnow
    from app.services import abs_path, cleanup_expired, requeue_stale
    token = signup(client)
    set_plan("a@example.com", "creator")
    token = csrf(client, "/app/kits/new")
    create_kit(client, token, media)
    (kid,) = kit_ids("a@example.com")
    job_id = submit(client, token, media, ["reel.mp4", "reel.mp4"], [kid], ["9x16"]).get_json()["id"]
    j = client.post(f"/api/jobs/{job_id}/cancel", headers={"X-CSRF-Token": token}).get_json()
    assert j["status"] == "cancelled" and all(r["status"] == "cancelled" for r in j["renders"])
    assert run_worker_until_idle() == 0

    job2 = submit(client, token, media, ["reel.mp4"], [kid], ["original"]).get_json()["id"]
    # simulate a crashed worker: render stuck in running with old heartbeat
    db = SessionLocal()
    r = db.scalars(select(Render).where(Render.job_id == job2)).one()
    r.status, r.heartbeat_at = "running", utcnow() - timedelta(minutes=30)
    db.commit()
    assert requeue_stale(db) == 1
    SessionLocal.remove()
    assert run_worker_until_idle() == 1
    data = client.get(f"/api/jobs/{job2}").get_json()
    assert data["status"] == "done"
    dl = data["renders"][0]["download"]
    assert client.get(dl).status_code == 200
    # expire
    db = SessionLocal()
    db.get(Job, job2).expires_at = utcnow() - timedelta(hours=1)
    db.commit()
    assert cleanup_expired(db) >= 1
    SessionLocal.remove()
    assert not abs_path(f"jobs/{job2}").exists()
    assert client.get(dl).status_code == 404
    assert client.get(f"/api/jobs/{job2}").get_json()["status"] == "expired"


def test_users_cannot_access_each_other(app, media):
    a, b = app.test_client(), app.test_client()
    ta = signup(a, "a@example.com")
    ta = csrf(a, "/app/kits/new")
    create_kit(a, ta, media)
    (kid,) = kit_ids("a@example.com")
    job_id = submit(a, ta, media, ["reel.mp4"], [kid], ["original"]).get_json()["id"]
    run_worker_until_idle()
    rid = a.get(f"/api/jobs/{job_id}").get_json()["renders"][0]["id"]
    tb = signup(b, "b@example.com")
    tb = csrf(b, "/app/kits/new")
    for path in [f"/app/kits/{kid}/edit", f"/app/kits/{kid}/asset/logo", f"/app/jobs/{job_id}",
                 f"/api/jobs/{job_id}", f"/app/jobs/{job_id}/renders/{rid}", f"/app/jobs/{job_id}/zip"]:
        assert b.get(path).status_code == 404, path
    assert b.post(f"/app/kits/{kid}/delete", data={"_csrf": tb}).status_code == 404
    assert b.post(f"/api/jobs/{job_id}/cancel", headers={"X-CSRF-Token": tb}).status_code == 404
    # B can't use A's kit in a job
    assert submit(b, tb, media, ["reel.mp4"], [kid], ["original"]).status_code == 400
    assert a.get(f"/app/jobs/{job_id}/renders/{rid}").status_code == 200


# ---------------------------------------------------------------- billing
def _sig(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_flow(client, monkeypatch):
    from app.models import SessionLocal, User
    signup(client)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec")
    db = SessionLocal()
    uid = db.scalar(select(User).where(User.email == "a@example.com")).id
    SessionLocal.remove()

    def send(event, sub_id, plan="agency", event_id=None):
        body = json.dumps({"event": event, "created_at": 1, "payload": {"subscription": {"entity": {
            "id": sub_id, "notes": {"user_id": uid, "plan": plan}}}}}).encode()
        return client.post("/billing/webhook", data=body, content_type="application/json",
                           headers={"X-Razorpay-Signature": _sig("whsec", body), "X-Razorpay-Event-Id": event_id or f"{event}-{sub_id}"})

    def user():
        u = SessionLocal().scalar(select(User).where(User.email == "a@example.com"))
        SessionLocal.remove()
        return u

    bad = client.post("/billing/webhook", data=b"{}", headers={"X-Razorpay-Signature": "x"})
    assert bad.status_code == 400
    assert send("subscription.activated", "sub_1").status_code == 200
    assert user().plan == "agency" and user().razorpay_subscription_id == "sub_1"
    assert send("subscription.activated", "sub_1").get_json()["duplicate"] is True
    # upgrade to new sub, then old sub cancellation must NOT downgrade
    send("subscription.activated", "sub_2", plan="agency_pro")
    assert user().plan == "agency_pro"
    send("subscription.cancelled", "sub_1")
    assert user().plan == "agency_pro"
    send("subscription.halted", "sub_2")
    assert user().plan == "free" and user().plan_status == "payment_issue"
    send("subscription.charged", "sub_2", plan="agency_pro", event_id="charged-2")
    assert user().plan == "agency_pro" and user().plan_status == "active"
    send("subscription.cancelled", "sub_2")
    assert user().plan == "free" and user().razorpay_subscription_id is None


def test_checkout_verify(client, monkeypatch):
    from app import billing
    from app.models import SessionLocal, User
    token = signup(client)
    # not configured -> friendly redirect
    token = csrf(client, "/app/billing")
    r = client.post("/app/billing/subscribe/agency", data={"_csrf": token})
    assert r.status_code == 302
    assert "aren" in client.get("/app/billing").get_data(as_text=True)

    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "keysecret")
    monkeypatch.setenv("RAZORPAY_PLAN_AGENCY", "plan_abc")
    calls = {}

    def fake_create(plan_id, user_id, plan_key, email):
        calls["create"] = (plan_id, plan_key, email)
        return {"id": "sub_new"}
    monkeypatch.setattr(billing, "create_subscription", fake_create)
    r = client.post("/app/billing/subscribe/agency", data={"_csrf": token})
    assert r.status_code == 200 and "sub_new" in r.get_data(as_text=True) and "checkout.razorpay.com" in r.get_data(as_text=True)
    assert calls["create"] == ("plan_abc", "agency", "a@example.com")
    # forged signature
    r = client.post("/app/billing/verify", data={"_csrf": token, "razorpay_payment_id": "pay_1",
                                                  "razorpay_subscription_id": "sub_new", "razorpay_signature": "forged"})
    u = SessionLocal().scalar(select(User).where(User.email == "a@example.com")); SessionLocal.remove()
    assert u.plan == "free"
    sig = _sig("keysecret", b"pay_1|sub_new")
    r = client.post("/app/billing/verify", data={"_csrf": token, "razorpay_payment_id": "pay_1",
                                                  "razorpay_subscription_id": "sub_new", "razorpay_signature": sig})
    assert r.status_code == 302
    u = SessionLocal().scalar(select(User).where(User.email == "a@example.com")); SessionLocal.remove()
    assert u.plan == "agency" and u.razorpay_subscription_id == "sub_new" and u.pending_plan is None
    assert client.post("/app/billing/subscribe/free", data={"_csrf": token}).status_code == 404


def test_report_form(client):
    from app.models import AbuseReport, SessionLocal
    token = csrf(client, "/report")
    assert client.post("/report", data={"_csrf": token, "email": "x", "details": "short"}).status_code == 400
    r = client.post("/report", data={"_csrf": token, "email": "owner@example.com", "url": "https://x.com/v",
                                     "details": "This is my copyrighted video."})
    assert r.status_code == 302
    assert SessionLocal().scalars(select(AbuseReport)).one().reporter_email == "owner@example.com"


def test_manage_cli(client, capsys, app, monkeypatch):
    import manage
    signup(client)
    monkeypatch.setenv("DATABASE_URL", app.config["DATABASE_URL"])
    assert manage.main(["set-plan", "a@example.com", "agency_pro"]) == 0
    assert manage.main(["reset-password", "a@example.com", "--password", "brandnew123"]) == 0
    assert manage.main(["set-plan", "nobody@example.com", "agency"]) == 1
    assert manage.main(["list-users"]) == 0
    assert "agency_pro" in capsys.readouterr().out
    assert client.get("/app").status_code == 302  # reset password ends existing sessions
    r = client.post("/login", data={"_csrf": csrf(client, "/login"), "email": "a@example.com", "password": "brandnew123"})
    assert r.status_code == 302


def test_parallel_workers_finish_job(client, media, app):
    """Two workers finishing renders of the same job concurrently must leave the job 'done'."""
    import threading
    from app.models import SessionLocal
    from app.services import claim_next_render, process_render
    token = signup(client)
    set_plan("a@example.com", "agency")
    token = csrf(client, "/app/kits/new")
    create_kit(client, token, media)
    (kid,) = kit_ids("a@example.com")
    job_id = submit(client, token, media, ["reel.mp4", "land.webm"], [kid], ["original", "1x1"]).get_json()["id"]

    def worker():
        while True:
            db = SessionLocal()
            rid = claim_next_render(db)
            SessionLocal.remove()
            if not rid:
                return
            process_render(rid)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(180) for t in threads]
    j = client.get(f"/api/jobs/{job_id}").get_json()
    assert all(r["status"] == "done" for r in j["renders"]), j
    assert j["status"] == "done"


# ---------------------------------------------------------------- frame rate + quality
def test_source_frame_rate_is_kept(media, tmp_path):
    """60 fps stays 60, 24 fps stays 24, and the plan cap still applies."""
    from app import engine as eng
    kit = eng.KitSpec(logo_path=str(media / "logo.png"))
    cases = [("reel60.mp4", 60, 60), ("reel24.mp4", 60, 24), ("reel60.mp4", 30, 30)]
    for src, cap, expected in cases:
        out = tmp_path / f"{src}-{cap}.mp4"
        eng.render(eng.RenderSpec(str(media / src), str(out), kit, fmt="original", max_fps=cap))
        assert round(probe(out).fps) == expected, (src, cap, probe(out).fps)


def test_intro_outro_uses_fastest_clip_rate(media, tmp_path):
    kit = __import__("app.engine", fromlist=["x"]).KitSpec(
        logo_path=str(media / "logo.png"), intro_path=str(media / "intro.mp4"))  # intro is 30 fps
    from app import engine as eng
    out = tmp_path / "mixed.mp4"
    eng.render(eng.RenderSpec(str(media / "reel60.mp4"), str(out), kit, fmt="original", max_fps=60))
    info = probe(out)
    assert round(info.fps) == 60 and abs(info.duration - 4.0) < 0.2   # 1 s intro + 3 s main


def test_high_quality_is_bigger_and_better(media, tmp_path):
    from app import engine as eng
    sizes = {}
    for q, (_label, crf, preset) in eng.QUALITIES.items():
        out = tmp_path / f"{q}.mp4"
        eng.render(eng.RenderSpec(str(media / "reel.mp4"), str(out), eng.KitSpec(), fmt="original",
                                  crf=crf, preset=preset))
        sizes[q] = out.stat().st_size
    assert sizes["high"] > sizes["balanced"] * 1.1


def test_quality_is_gated_by_plan(client, media, app):
    token = signup(client)
    token = csrf(client, "/app/kits/new")
    create_kit(client, token, media)
    (kid,) = kit_ids("a@example.com")

    def post(quality):   # fresh file handles each time
        data = {"videos": [_file(media, "reel.mp4")], "kits": [kid], "formats": ["original"], "quality": quality}
        return client.post("/api/jobs", data=data, content_type="multipart/form-data",
                           headers={"X-CSRF-Token": token})

    r = post("high")
    assert r.status_code == 402 and "Agency plan" in r.get_json()["error"]
    assert post("nonsense").status_code == 400
    # free plan still renders at balanced
    job = post("balanced").get_json()["id"]
    assert client.get(f"/api/jobs/{job}").get_json()["renders"][0]["quality"] == "balanced"

    set_plan("a@example.com", "agency")
    r = post("high")
    assert r.status_code == 201
    job = r.get_json()["id"]
    assert client.get(f"/api/jobs/{job}").get_json()["renders"][0]["quality"] == "high"
    run_worker_until_idle()
    assert client.get(f"/api/jobs/{job}").get_json()["renders"][0]["status"] == "done"


def test_plan_limits_are_applied_to_output(client, media, app):
    """Free renders 720p/30fps with the mark; Creator keeps 1080p and 60 fps."""
    from app.services import abs_path
    token = signup(client)
    token = csrf(client, "/app/kits/new")
    create_kit(client, token, media)
    (kid,) = kit_ids("a@example.com")
    job = submit(client, token, media, ["reel60.mp4"], [kid], ["9x16"]).get_json()["id"]
    run_worker_until_idle()
    j = client.get(f"/api/jobs/{job}").get_json()
    out = abs_path("free60.mp4")
    out.write_bytes(client.get(j["renders"][0]["download"]).data)
    info = probe(out)
    assert (info.width, info.height) == (720, 1280) and round(info.fps) == 30

    set_plan("a@example.com", "creator")
    job = submit(client, token, media, ["reel60.mp4"], [kid], ["9x16"]).get_json()["id"]
    run_worker_until_idle()
    j = client.get(f"/api/jobs/{job}").get_json()
    out.write_bytes(client.get(j["renders"][0]["download"]).data)
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920) and round(info.fps) == 60


def test_pricing_page_lists_every_plan_detail(client):
    from app.plans import PLANS, comparison_rows
    html = client.get("/pricing").get_data(as_text=True)
    for label, _help, values in comparison_rows():
        assert label in html, label
        for key in PLANS:
            assert values[key] in html, (label, key, values[key])
    assert "Up to 720p" in html and "Up to 60 fps" in html and "Balanced + High" in html


# ---------------------------------------------------------------- anti-abuse
def test_disposable_emails_are_rejected(client):
    from app.models import SessionLocal, User
    token = csrf(client, "/signup")
    for bad in ["throwaway@mailinator.com", "x@sub.yopmail.com", "y@tempmail.org"]:
        r = client.post("/signup", data={"_csrf": token, "email": bad, "password": "password123", "terms": "1"})
        assert r.status_code == 400 and "permanent work email" in r.get_data(as_text=True), bad
    r = client.post("/signup", data={"_csrf": token, "email": "real@myagency.co.in",
                                     "password": "password123", "terms": "1"})
    assert r.status_code == 302
    assert SessionLocal().scalars(select(User)).all()[0].email == "real@myagency.co.in"


def test_signup_cap_per_ip(app):
    from app.services import MAX_ACCOUNTS_PER_IP_PER_DAY
    made = []
    for i in range(MAX_ACCOUNTS_PER_IP_PER_DAY + 1):
        c = app.test_client()
        token = csrf(c, "/signup")
        r = c.post("/signup", data={"_csrf": token, "email": f"u{i}@agency.test",
                                    "password": "password123", "terms": "1"})
        made.append(r.status_code)
    assert made[:MAX_ACCOUNTS_PER_IP_PER_DAY] == [302] * MAX_ACCOUNTS_PER_IP_PER_DAY
    assert made[-1] == 400
    # a different network is unaffected
    c = app.test_client()
    token = csrf(c, "/signup")
    r = c.post("/signup", data={"_csrf": token, "email": "other@agency.test", "password": "password123",
                                "terms": "1"}, environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    assert r.status_code == 302


def test_email_must_be_verified_before_rendering(client, media):
    from app.models import SessionLocal, User
    token = signup(client, verified=False)
    token = csrf(client, "/app/kits/new")
    create_kit(client, token, media)          # kits are allowed before verifying
    (kid,) = kit_ids("a@example.com")
    r = submit(client, token, media, ["reel.mp4"], [kid], ["original"])
    assert r.status_code == 403 and "Confirm your email" in r.get_json()["error"]
    assert "Confirm your email address" in client.get("/app").get_data(as_text=True)

    # the emailed link verifies the account
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == "a@example.com"))
    SessionLocal.remove()
    assert u.verify_token_hash and u.email_verified_at is None
    assert client.get("/verify/not-a-real-token").status_code == 302
    from app.services import new_verification_token
    db = SessionLocal()
    fresh = new_verification_token(db, db.get(User, u.id))
    SessionLocal.remove()
    assert client.get(f"/verify/{fresh}").status_code == 302
    assert submit(client, token, media, ["reel.mp4"], [kid], ["original"]).status_code == 201


def test_expired_verification_link_is_rejected(client):
    from datetime import timedelta as td
    from app.models import SessionLocal, User, utcnow
    from app.services import new_verification_token
    signup(client, verified=False)
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == "a@example.com"))
    token = new_verification_token(db, u)
    u.verify_sent_at = utcnow() - td(hours=49)
    db.commit()
    SessionLocal.remove()
    client.get(f"/verify/{token}")
    db = SessionLocal()
    assert db.scalar(select(User).where(User.email == "a@example.com")).email_verified_at is None
    SessionLocal.remove()


def test_free_minutes_capped_per_ip(client, media, app, monkeypatch):
    """Several free accounts from one network share a daily free-render budget."""
    from datetime import timedelta as td
    from app.models import Job, JobInput, Render, SessionLocal, User, utcnow
    import app.services as svc
    monkeypatch.setattr(svc, "FREE_MINUTES_PER_IP_PER_DAY", 1)   # 60 seconds for the test
    token = signup(client)
    token = csrf(client, "/app/kits/new")
    create_kit(client, token, media)
    (kid,) = kit_ids("a@example.com")
    # a sibling free account from the same IP already burned the shared budget
    db = SessionLocal()
    u = db.scalar(select(User).where(User.email == "a@example.com"))
    sibling = User(email="sib@agency.test", password_hash="x", plan="free",
                   signup_ip_hash=u.signup_ip_hash, email_verified_at=utcnow())
    db.add(sibling)
    db.flush()
    sib_job = Job(user_id=sibling.id, formats=["original"], expires_at=utcnow() + td(hours=48))
    db.add(sib_job)
    db.flush()
    sib_input = JobInput(job_id=sib_job.id, position=0, original_name="x.mp4", stored_file="x.mp4", duration=59)
    db.add(sib_input)
    db.flush()
    db.add(Render(job_id=sib_job.id, user_id=sibling.id, input_id=sib_input.id, position=0, kit_name="x",
                  kit_snapshot={}, fmt="original", output_name="x.mp4", status="done",
                  rendered_seconds=59, finished_at=utcnow()))
    db.commit()
    SessionLocal.remove()
    r = submit(client, token, media, ["reel.mp4"], [kid], ["original"])
    assert r.status_code == 402 and "your network" in r.get_json()["error"]


# ---------------------------------------------------------------- one-time top-ups (Standard Checkout)
def _order_sig(secret, order_id, payment_id):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def test_topup_order_and_verification(client, monkeypatch):
    from app import billing
    from app.models import MinuteTopup, SessionLocal, User
    from app.plans import TOPUP_PACKS
    from app.services import usage_seconds
    signup(client)
    token = csrf(client, "/app/billing")      # signup rotates the session, so take a fresh token

    # payments not configured yet
    r = client.post("/api/create-order", json={"pack": "pack_100"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 503

    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "keysecret")
    calls = {}

    def fake_order(amount_paise, receipt, notes=None):
        calls["amount"], calls["notes"] = amount_paise, notes
        return {"id": "order_TEST1", "amount": amount_paise, "currency": "INR"}
    monkeypatch.setattr(billing, "create_order", fake_order)

    assert client.post("/api/create-order", json={"pack": "nope"},
                       headers={"X-CSRF-Token": token}).status_code == 400
    r = client.post("/api/create-order", json={"pack": "pack_100"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 201
    body = r.get_json()
    assert body["order_id"] == "order_TEST1" and body["key_id"] == "rzp_test_x"
    assert calls["amount"] == TOPUP_PACKS["pack_100"].amount_paise >= 100
    assert "key_secret" not in r.get_data(as_text=True) and "keysecret" not in r.get_data(as_text=True)

    db = SessionLocal()
    user = db.scalar(select(User).where(User.email == "a@example.com"))
    before = usage_seconds(db, user)["limit"]
    SessionLocal.remove()

    # missing fields, then a forged signature: neither credits minutes
    assert client.post("/api/verify-payment", json={"razorpay_order_id": "order_TEST1"},
                       headers={"X-CSRF-Token": token}).status_code == 400
    r = client.post("/api/verify-payment", headers={"X-CSRF-Token": token}, json={
        "razorpay_order_id": "order_TEST1", "razorpay_payment_id": "pay_1", "razorpay_signature": "forged"})
    assert r.status_code == 400
    db = SessionLocal()
    assert db.scalar(select(MinuteTopup)).status == "failed"
    assert usage_seconds(db, db.scalar(select(User))) ["limit"] == before
    SessionLocal.remove()

    # the real signature credits exactly once
    sig = _order_sig("keysecret", "order_TEST1", "pay_1")
    r = client.post("/api/verify-payment", headers={"X-CSRF-Token": token}, json={
        "razorpay_order_id": "order_TEST1", "razorpay_payment_id": "pay_1", "razorpay_signature": sig})
    assert r.status_code == 200 and r.get_json()["minutes"] == 100
    r2 = client.post("/api/verify-payment", headers={"X-CSRF-Token": token}, json={
        "razorpay_order_id": "order_TEST1", "razorpay_payment_id": "pay_1", "razorpay_signature": sig})
    assert r2.get_json().get("already_credited") is True

    db = SessionLocal()
    user = db.scalar(select(User).where(User.email == "a@example.com"))
    assert usage_seconds(db, user)["limit"] == before + 100 * 60
    assert db.scalar(select(MinuteTopup)).payment_id == "pay_1"
    SessionLocal.remove()


def test_topup_requires_login_and_csrf_and_ownership(app, monkeypatch):
    from app import billing
    from app.models import MinuteTopup, SessionLocal
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "keysecret")
    monkeypatch.setattr(billing, "create_order",
                        lambda amount_paise, receipt, notes=None: {"id": "order_A", "amount": amount_paise})
    a, b = app.test_client(), app.test_client()
    signup(a, "a@example.com")
    ta = csrf(a, "/app/billing")
    assert app.test_client().post("/api/create-order", json={"pack": "pack_100"}).status_code in (400, 401)
    assert a.post("/api/create-order", json={"pack": "pack_100"}).status_code == 400   # no CSRF header
    assert a.post("/api/create-order", json={"pack": "pack_100"}, headers={"X-CSRF-Token": ta}).status_code == 201

    signup(b, "b@example.com")
    tb = csrf(b, "/app/billing")
    sig = _order_sig("keysecret", "order_A", "pay_x")
    r = b.post("/api/verify-payment", headers={"X-CSRF-Token": tb}, json={
        "razorpay_order_id": "order_A", "razorpay_payment_id": "pay_x", "razorpay_signature": sig})
    assert r.status_code == 404            # B cannot claim A's order
    db = SessionLocal()
    assert db.scalar(select(MinuteTopup)).status == "created"
    SessionLocal.remove()


def test_create_order_handles_razorpay_errors(client, monkeypatch):
    from app import billing
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "keysecret")
    signup(client)
    token = csrf(client, "/app/billing")

    def auth_fail(*a, **kw):
        raise billing.AuthError("bad keys")
    monkeypatch.setattr(billing, "create_order", auth_fail)
    assert client.post("/api/create-order", json={"pack": "pack_100"},
                       headers={"X-CSRF-Token": token}).status_code == 401

    def api_fail(*a, **kw):
        raise billing.BillingError("500 from razorpay")
    monkeypatch.setattr(billing, "create_order", api_fail)
    assert client.post("/api/create-order", json={"pack": "pack_100"},
                       headers={"X-CSRF-Token": token}).status_code == 500


def test_order_amount_floor_and_signature_helper():
    import os
    from app import billing
    os.environ["RAZORPAY_KEY_SECRET"] = "keysecret"
    try:
        billing.create_order(50, "r")
    except billing.BillingError as exc:
        assert "at least 100 paise" in str(exc)
    else:
        raise AssertionError("amounts below 100 paise must be rejected")
    sig = _order_sig("keysecret", "order_1", "pay_1")
    assert billing.verify_order_signature("order_1", "pay_1", sig)
    assert not billing.verify_order_signature("order_1", "pay_1", "x" * 64)
    assert not billing.verify_order_signature("", "pay_1", sig)
