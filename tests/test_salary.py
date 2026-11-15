"""Shifokorga maosh va avans berish: davr, avans ushlanishi, jadval va Excel."""
import io
import sys
from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import salary  # noqa: E402
from db import DB  # noqa: E402
from report import Period  # noqa: E402
from test_hisobot import _client, svc  # noqa: F401,E402  (fixture)


def fake_earned(table):
    return lambda doc, p: table.get((doc, p.start.isoformat(), p.end.isoformat()), 0)


def test_period_avans_and_status():
    db = DB(":memory:")
    salary.set_start(db, date(2026, 11, 1), {})
    earned = fake_earned({("501", "2026-11-01", "2026-11-10"): 1_000_000,
                          ("501", "2026-11-11", "2026-11-20"): 600_000})
    p = salary.plan(db, "501", date(2026, 11, 10), earned)
    assert (p.start, p.end, p.earned, p.can, p.paid_up) == (date(2026, 11, 1), date(2026, 11, 10), 1_000_000, 1_000_000, False)
    with pytest.raises(salary.TooMuch) as e:
        salary.pay_salary(db, p, "Madina", 1_500_000, date(2026, 11, 10))
    assert e.value.can == 1_000_000
    # ishlagan 1 mln maosh + 500 ming avans
    salary.pay_with_avans(db, p, "Madina", 500_000, date(2026, 11, 10))
    rows = salary.payouts(db, "501")
    assert [(r["kind"], r["amount"], r["period_from"], r["period_to"]) for r in rows] == [
        ("maosh", 1_000_000, "2026-11-01", "2026-11-10"), ("avans", 500_000, None, None)]
    assert salary.avans_left(rows) == 500_000
    # bugungacha berib bo'lingan
    assert salary.plan(db, "501", date(2026, 11, 10), earned).paid_up
    # noyabr jadvalida: berilgan 1,5 mln, avans 500 ming, berilmagan −500 ming (oldindan olgan)
    st = salary.status(rows, Period.month("2026-11"), 1_000_000)
    assert (st.given, st.avans, st.avans_left, st.left) == (1_500_000, 500_000, 500_000, -500_000)
    # keyingi davr 11-kundan boshlanadi, avans ushlanadi
    p2 = salary.plan(db, "501", date(2026, 11, 20), earned)
    assert (p2.start, p2.earned, p2.avans, p2.can, p2.net) == (date(2026, 11, 11), 600_000, 500_000, 100_000, 100_000)
    salary.pay_salary(db, p2, "Madina", 100_000, date(2026, 11, 20))
    rows = salary.payouts(db, "501")
    assert salary.avans_left(rows) == 0
    # avans ikki marta hisoblanmaydi: butun oy bo'yicha berilmagan 0
    st = salary.status(rows, Period.month("2026-11"), 1_600_000)
    assert (st.given, st.left) == (1_600_000, 0)


def test_avans_only_when_paid_up_and_opening_balance():
    db = DB(":memory:")
    salary.set_start(db, date(2026, 11, 1), {"502": 300_000})
    earned = fake_earned({("502", "2026-11-01", "2026-11-15"): 250_000})
    p = salary.plan(db, "502", date(2026, 11, 15), earned)
    assert (p.opening, p.earned, p.can) == (300_000, 550_000, 550_000)   # boshlang'ich qoldiq birinchi maoshga qo'shiladi
    salary.pay_salary(db, p, "Bobur", 550_000, date(2026, 11, 15))
    p = salary.plan(db, "502", date(2026, 11, 15), earned)
    assert p.paid_up and p.can == 0
    salary.pay_with_avans(db, p, "Bobur", 200_000, date(2026, 11, 15))   # faqat avans yoziladi
    rows = salary.payouts(db, "502")
    assert [r["kind"] for r in rows] == ["maosh", "avans"]
    assert salary.plan(db, "502", date(2026, 11, 20), fake_earned({})).net == -200_000


def test_salary_web_flow(svc, monkeypatch):  # noqa: F811
    import salary_routes
    c = _client(svc, monkeypatch)
    monkeypatch.setattr(salary_routes, "today", lambda: date(2026, 11, 15))
    c.post("/login", data={"login": "admin", "parol": "maxfiy"})
    r = c.post("/maosh/boshlanish", data={"boshlanish": "2026-11-01", "qoldiq_502": "0", "qaytish": "/shifokorlar?oy=2026-11"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/shifokorlar?oy=2026-11"

    h = c.get("/maosh/501/hisob").json()
    assert (h["from"], h["to"], h["earned"], h["can"], h["name"]) == ("2026-11-01", "2026-11-15", 125000, 125000, "Karimova madina")
    r = c.post("/maosh/501/berish", data={"summa": "200000"})
    assert r.status_code == 409 and r.json() == {"error": "ortiq", "can": 125000}
    assert c.post("/maosh/501/avans", data={"avans": "50000"}).json() == {"ok": True}
    assert c.get("/maosh/501/hisob").json()["paid_up"] is True
    assert c.get("/maosh/0/hisob").status_code == 404 and c.get("/maosh/999/hisob").status_code == 404

    d = c.get("/shifokorlar?oy=2026-11").text
    assert "175 000" in d                     # berilgan: maosh 125 000 + avans 50 000
    assert "shundan avans 50 000" in d and "ushlanmagan 50 000" in d
    assert "−50 000" in d                     # berilmagan: shifokor oldindan olgan
    assert "/shifokor/501/maosh.xlsx?oy=2026-11" in d

    rows = c.get("/maosh/501/hisob").json()["history"]
    pay = next(x for x in rows if x["kind"] == "maosh")
    x = c.get(f"/maosh/{pay['id']}/excel")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    ws = load_workbook(io.BytesIO(x.content)).active
    vals = [[c.value for c in row] for row in ws.iter_rows()]
    assert vals[0][0] == "Shifokor Karimova madina 01.11–15.11 2026-yil uchun oylik maoshi"
    assert vals[2] == ["Sana", "F.I.SH.", "Summa", "Shifokor"]
    assert vals[3] == ["03.11.2026", "Usmonov Akmal", 500000, 125000]
    assert vals[-1] == ["Shifokor oldi", None, None, 125000]
    assert c.get("/shifokor/501/maosh.xlsx?oy=2026-11").content[:2] == b"PK"

    av = next(x for x in rows if x["kind"] == "avans")
    assert c.post(f"/maosh/ochirish/{av['id']}").json() == {"ok": True}
    assert c.get("/maosh/501/hisob").json()["avans_left"] == 0
