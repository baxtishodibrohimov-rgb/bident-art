"""Shifokor bo'lmagan xodimlar: maosh shabloni, davomat boti, grafik va 20/25/55 taqsimoti."""
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import staff  # noqa: E402
from bot import Bot, TelegramAPI  # noqa: E402
from db import DB, new_token, now  # noqa: E402
from test_sklad import FakeTG, btn, msg  # noqa: E402


def add_worker(db, name, daily, extra, position="Assistent"):
    return db.run("INSERT INTO workers(full_name, position, daily_rate, extra_rate, created_at) VALUES(?,?,?,?,?)",
                  (name, position, daily, extra, now()))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DAM_OLISH_KUNLARI", "6")  # yakshanba
    db = DB(":memory:")
    tg = FakeTG()
    bot = Bot(db, TelegramAPI("x", transport=httpx.MockTransport(tg)), "Biodent")
    bot.identify()
    return db, tg, bot


def test_payroll_rules(env):
    db, tg, bot = env
    a = add_worker(db, "Aziza", 150000, 50000)
    b = add_worker(db, "Bekzod", 120000, 40000, "Haydovchi")
    # 2026-11-13 juma (oddiy kun), 2026-11-15 yakshanba (dam olish kuni)
    staff.save_day(db, "2026-11-13", True, [a, b], [b], None)   # Bekzod normadan ko'p
    staff.save_day(db, "2026-11-15", True, [a], [], None)       # dam olish kuni ishladi → qo'shimcha
    staff.save_day(db, "2026-11-14", False, [], [], None)
    pays = {w.worker["full_name"]: w for w in staff.payroll(db, date(2026, 11, 1), date(2026, 11, 30))}
    assert pays["Aziza"].worked == ["2026-11-13", "2026-11-15"] and pays["Aziza"].extra_days == ["2026-11-15"]
    assert pays["Aziza"].total == 2 * 150000 + 50000
    assert pays["Bekzod"].total == 120000 + 40000
    assert staff.payroll_total(db, date(2026, 11, 1), date(2026, 11, 30)) == 510000
    # dam olish kunida "normadan ko'p" ham belgilansa, qo'shimcha baribir bir marta
    staff.set_mark(db, "2026-11-15", a, "qoshimcha")
    assert staff.payroll(db, date(2026, 11, 15), date(2026, 11, 15))[0].total == 200000
    staff.set_mark(db, "2026-11-15", a, "yoq")
    assert staff.payroll_total(db, date(2026, 11, 15), date(2026, 11, 15)) == 0
    # faol bo'lmagan va ishlamagan xodim ro'yxatdan chiqadi, ishlagani qoladi
    db.run("UPDATE workers SET active=0")
    assert [w.worker["full_name"] for w in staff.payroll(db, date(2026, 11, 1), date(2026, 11, 30))] == ["Aziza", "Bekzod"]
    assert staff.payroll(db, date(2026, 12, 1), date(2026, 12, 31)) == []


def test_attendance_bot(env, monkeypatch):
    db, tg, bot = env
    a, b, c = add_worker(db, "Aziza", 150000, 50000), add_worker(db, "Bekzod", 120000, 40000), add_worker(db, "Dilshod", 100000, 30000)
    tok, boss = new_token(), new_token()
    db.run("INSERT INTO people(name, is_attendance, token) VALUES(?,?,?)", ("Ma'mura", 1, tok))
    db.run("INSERT INTO people(name, is_admin, token) VALUES(?,?,?)", ("Bosh shifokor", 1, boss))
    bot.handle(msg(301, f"/start {tok}"))
    bot.handle(msg(302, f"/start {boss}"))
    assert "kunlik davomat" in tg.last(301)[1]
    monkeypatch.setattr("bot.today", lambda: "2026-11-13")
    assert bot.ask_attendance() == 1
    chat, text, buttons = tg.last(301)
    assert "klinika ishladimi" in text and buttons == ["att:w:2026-11-13:1", "att:w:2026-11-13:0"]
    bot.handle(btn(301, buttons[0]))
    chat, text, buttons = tg.last(301)
    assert "Kimlar keldi" in text and buttons == [f"att:t:{a}", f"att:t:{b}", f"att:t:{c}", "att:ok"]
    edits_before = tg.n
    bot.handle(btn(301, f"att:t:{c}"))           # Dilshod kelmadi
    assert db.state(301)["sel"] == [a, b]
    bot.handle(btn(301, "att:ok"))
    chat, text, buttons = tg.last(301)
    assert "normadan ko'p" in text and buttons == [f"att:t:{a}", f"att:t:{b}", "att:ok"]
    bot.handle(btn(301, f"att:t:{b}"))           # Bekzod normadan ko'p
    bot.handle(btn(301, "att:ok"))
    final = tg.last(301)[1]
    assert "Aziza — 150 000" in final and "Bekzod (+qo'shimcha) — 160 000" in final and "Jami: 310 000" in final
    assert "Davomat 13.11.2026" in tg.last(302)[1]       # adminga ham
    assert db.one("SELECT worked FROM clinic_days WHERE day='2026-11-13'")["worked"] == 1
    assert {r["worker_id"]: r["extra"] for r in db.q("SELECT * FROM attendance")} == {a: 0, b: 1}
    assert bot.ask_attendance() == 0                      # bugun allaqachon javob berilgan
    bot.handle(btn(301, "att:ok"))
    assert "allaqachon" in tg.last(301)[1]
    # boshqa kuni: klinika ishlamadi
    monkeypatch.setattr("bot.today", lambda: "2026-11-14")
    bot.ask_attendance()
    bot.handle(btn(301, tg.last(301)[2][1]))
    assert "ishlamadi" in tg.last(301)[1]
    assert db.one("SELECT worked FROM clinic_days WHERE day='2026-11-14'")["worked"] == 0
    # davomat vazifasi yo'q odam tugmani bossa — hech narsa
    bot.handle(btn(302, "att:w:2026-11-15:1"))
    assert not db.one("SELECT 1 FROM clinic_days WHERE day='2026-11-15'")


def test_pages_and_distribution(env, tmp_path, monkeypatch):
    db, tg, bot = env
    monkeypatch.setenv("HISOBOT_PAROL", "maxfiy")
    import app as webapp
    import sklad_routes
    from fastapi.testclient import TestClient
    from test_hisobot import fake_api
    from client import CliniccardsClient, RateLimiter
    from service import Percents, Service
    client = CliniccardsClient("test-token", base_url="https://cliniccards.test/api",
                               transport=httpx.MockTransport(fake_api), limiter=RateLimiter(1000, 1))
    p = Percents(tmp_path / "f.json")
    p.save({"501": 25, "502": 30})
    monkeypatch.setattr(webapp, "_service", Service(client, p, start_date=date(2015, 1, 1)))
    monkeypatch.setitem(sklad_routes._state, "db", db)
    monkeypatch.setitem(sklad_routes._state, "bot", bot)
    monkeypatch.setattr(webapp, "attempts", __import__("auth").Attempts())
    c = TestClient(webapp.app)
    c.post("/login", data={"login": "admin", "parol": "maxfiy"})

    r = c.post("/maosh-xodimlar", data={"full_name": "Aziza Karimova", "position": "Assistent",
                                        "daily_rate": "150000", "extra_rate": "50000"}, follow_redirects=True)
    assert "Aziza Karimova" in r.text
    wid = db.one("SELECT id FROM workers")["id"]
    assert c.post("/maosh-xodimlar", data={"full_name": "X", "daily_rate": "-5"}).status_code == 400
    for day, state in (("2026-11-13", "keldi"), ("2026-11-15", "keldi"), ("2026-11-10", "qoshimcha")):
        c.post("/grafik/belgi", data={"day": day, "worker_id": wid, "state": state, "back": "/grafik?oy=2026-11"})
    r = c.get("/grafik?oy=2026-11")
    assert r.status_code == 200 and "550 000" in r.text      # 3 × 150 000 + 2 × 50 000 (yakshanba va qo'shimcha)
    assert c.get("/grafik?dan=2026-01-01&gacha=2026-12-31").status_code == 400
    assert "550 000" in c.get("/maosh-xodimlar?oy=2026-11").text

    # bosh ekran: kassa 2 050 000 → klinika 410 000, shifokorlar 512 500, xarajatlar 1 127 500;
    # ishlatilgan = xodimlar 550 000 + Cliniccards xarajatlari 900 000 = 1 450 000 → qoldi −322 500
    r = c.get("/?oy=2026-11")
    for text in ("Kassaning taqsimoti", "410 000", "512 500", "1 127 500", "1 450 000", "-322 500", "55%"):
        assert text in r.text, text
    c.post("/sozlamalar", data={"klinika": "20", "shifokorlar": "25", "default": "0"})
    assert c.post("/sozlamalar", data={"klinika": "60", "shifokorlar": "50"}).status_code == 400

    # davomat roli va tugma
    c.post("/xodimlar", data={"name": "Ma'mura", "is_attendance": "1"})
    assert db.one("SELECT is_attendance FROM people WHERE name=?", ("Ma'mura",))["is_attendance"] == 1
    assert "Davomat so'rovini hozir yuborish" in c.get("/xodimlar").text


def test_diagnostics_page(env, monkeypatch, tmp_path):
    db, tg, bot = env
    monkeypatch.setenv("HISOBOT_PAROL", "maxfiy")
    import app as webapp
    import sklad_routes
    from fastapi.testclient import TestClient
    from test_sklad import fake_cc
    from client import CliniccardsClient, RateLimiter
    from service import Percents, Service
    client = CliniccardsClient("t", base_url="https://cc.test/api", transport=httpx.MockTransport(fake_cc),
                               limiter=RateLimiter(1000, 1))
    monkeypatch.setattr(webapp, "_service", Service(client, Percents(tmp_path / "f.json"), start_date=date(2015, 1, 1)))
    monkeypatch.setitem(sklad_routes._state, "db", db)
    monkeypatch.setitem(sklad_routes._state, "bot", bot)
    monkeypatch.setattr(webapp, "attempts", __import__("auth").Attempts())
    c = TestClient(webapp.app)
    c.post("/login", data={"login": "admin", "parol": "maxfiy"})
    r = c.get("/sklad/diagnostika?oy=36")
    assert r.status_code == 200
    for text in ("INCOME", "OUTCOME", "MOVE", "Dental Market", "Misol: INCOME", "Kompozit plomba A2"):
        assert text in r.text, text
