"""Soxta Cliniccards API bilan sinovlar (haqiqiy kalit kerak emas)."""
import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client import CliniccardsClient, CliniccardsError, RateLimiter  # noqa: E402
from report import build_report, month_ranges, parse_month  # noqa: E402
from service import Percents, Service  # noqa: E402
from datetime import date  # noqa: E402

STAFF = [
    {"doctor_id": "501", "lastname": "Karimova", "firstname": "madina", "role": "DOCTOR"},
    {"doctor_id": "502", "lastname": "Aliyev", "firstname": "Bobur", "role": "DOCTOR"},
]
ITEMS = {
    "DONE": [],
    "INVOICED": [
        # madina: oktyabrda qilingan ish, to'lov noyabrda (xulosadagi tekshiruv)
        {"plan_item_id": "1", "patient_id": "900", "name": "Plomba", "price_with_discount": "500000",
         "done_by_doctor_id": "501", "marked_done_date": "2026-10-20 00:00:00", "paid": "500000",
         "payments": [{"date": "2026-11-03 10:00:00", "amount": 500000, "payment_id": 1}]},
        # Bobur: martda breket, har oy to'lov; noyabrdagisi hisobga kiradi
        {"plan_item_id": "2", "patient_id": "901", "name": "Breket", "price_with_discount": "10000000",
         "done_by_doctor_id": "502", "marked_done_date": "2026-03-10 00:00:00", "paid": "9000000",
         "payments": [{"date": "2026-10-05 12:00:00", "amount": "1000000", "payment_id": 2},
                      {"date": "2026-11-05 12:00:00", "amount": "1000000", "payment_id": 3}]},
        # Shifokor belgilanmagan ish
        {"plan_item_id": "3", "patient_id": "900", "name": "Rentgen", "price_with_discount": "50000",
         "done_by_doctor_id": None, "marked_done_date": "2026-11-01 00:00:00", "paid": "50000",
         "payments": [{"date": "2026-11-01 09:00:00", "amount": 50000, "payment_id": 4}]},
        # 2017-yilgi eski ish, to'lov 2026-noyabrda: butun davr bo'yicha qidirilgani uchun topilishi kerak
        {"plan_item_id": "5", "patient_id": "901", "name": "Protez", "price_with_discount": "300000",
         "done_by_doctor_id": "503", "done_by": "Eski Shifokor", "marked_done_date": "2017-05-10 00:00:00",
         "paid": "300000", "payments": [{"date": "2026-11-10 10:00:00", "amount": 300000, "payment_id": 5}]},
        # pul tushmagan ish — hisobotda chiqmasligi kerak
        {"plan_item_id": "4", "patient_id": "901", "name": "Konsultatsiya", "price_with_discount": "100000",
         "done_by_doctor_id": "501", "marked_done_date": "2026-11-02 00:00:00", "paid": "0", "payments": None},
    ],
}
PATIENTS = {"900": {"patient_id": "900", "lastname": "Usmonov", "firstname": "Akmal"},
            "901": {"patient_id": "901", "lastname": "Rasulova", "firstname": "Dilnoza"}}


def fake_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["Token"] == "test-token"
    path = request.url.path.removeprefix("/api")
    ok = lambda data: httpx.Response(200, json={"data": data, "result": "success", "error": None})  # noqa: E731
    if path == "/staff":
        return ok(STAFF)
    if path.startswith("/planItems/"):
        status = path.split("/")[2]
        # haqiqiy API kabi: ish sanasi so'rov oralig'iga tushgan ishlarni qaytaramiz
        frm, to = request.url.params["from"], request.url.params["to"]
        return ok([it for it in ITEMS[status] if frm <= it["marked_done_date"][:10] <= to])
    if path.startswith("/patients/"):
        return ok([PATIENTS[i] for i in path.split("/")[2].split(",") if i in PATIENTS])
    if path == "/spendings/":
        return ok([
            {"id": "1", "amount": "400000", "paid": "400000", "payment_status": "Paid", "date_issued": "2026-11-02",
             "supplier": {"id": 1, "name": "Dental Market"}, "spendings_category": {"id": 10, "name": "Materiallar"},
             "spendings_subcategory": {"id": 11, "name": "Plomba materiali"}, "note": ""},
            {"id": "2", "amount": "600000", "paid": "600000", "payment_status": "Paid", "date_issued": "2026-11-05",
             "supplier": None, "spendings_category": {"id": 20, "name": "Ijara"},
             "spendings_subcategory": {"id": 21, "name": "Bino ijarasi"}, "note": "noyabr"},
            {"id": "3", "amount": "-100000", "paid": "-100000", "payment_status": "Paid", "date_issued": "2026-11-06",
             "supplier": {"id": 1, "name": "Dental Market"}, "spendings_category": {"id": 10, "name": "Materiallar"},
             "spendings_subcategory": {"id": 11, "name": "Plomba materiali"}, "note": "qaytarildi"},
        ])
    if path == "/payments":
        desk = {"id": "1", "name": "Naqd", "currency": "UZS"}
        card = {"id": "2", "name": "Terminal", "currency": "UZS"}
        return ok([{"amount": "500000", "patient_id": "900", "date": "2026-11-03 10:00:00", "cash_desk": desk, "cash_desk_id": "1"},
                   {"amount": "1000000", "patient_id": "901", "date": "2026-11-05 12:00:00", "cash_desk": card, "cash_desk_id": "2"},
                   {"amount": "50000", "patient_id": "900", "date": "2026-11-01 09:00:00", "cash_desk": desk, "cash_desk_id": "1"},
                   {"amount": "200000", "patient_id": "900", "date": "2026-11-07 09:00:00", "cash_desk": desk, "cash_desk_id": "1"},
                   {"amount": "300000", "patient_id": "901", "date": "2026-11-10 10:00:00", "cash_desk": card, "cash_desk_id": "2"}])
    return httpx.Response(404, json={"data": None, "result": "fail", "error": "not found"})


@pytest.fixture
def svc(tmp_path):
    client = CliniccardsClient("test-token", base_url="https://cliniccards.test/api",
                               transport=httpx.MockTransport(fake_api), limiter=RateLimiter(1000, 1))
    p = Percents(tmp_path / "foizlar.json")
    p.save({"501": 25, "502": 30})
    return Service(client, p, start_date=date(2015, 1, 1))


def test_madina_november(svc):
    rep = svc.month_report("2026-11")
    m = rep.doctor("501")
    assert m.total == Decimal("500000")
    assert m.salary == Decimal("125000")
    assert [r.work for r in m.rows] == ["Plomba"]  # pul tushmagan konsultatsiya chiqmaydi
    assert m.rows[0].patient == "Usmonov Akmal"


def test_october_has_no_madina_salary(svc):
    rep = svc.month_report("2026-10")
    assert rep.doctor("501") is None
    assert rep.doctor("502").total == Decimal("1000000")


def test_installments_and_unassigned(svc):
    rep = svc.month_report("2026-11")
    assert rep.doctor("502").total == Decimal("1000000")
    assert rep.doctor("502").salary == Decimal("250000")  # o'zgarmas 25%
    assert rep.doctor("0").total == Decimal("50000")
    assert rep.doctor("0").salary == 0
    assert rep.doctors[-1].doctor_id == "0"
    assert rep.total == Decimal("1850000")
    assert rep.kassa_total == Decimal("2050000")
    assert rep.difference == Decimal("200000")


def test_debts(svc):
    groups = svc.debts("2026-11")
    left = {doc: sum(d.left for d in items) for doc, _, items in groups}
    assert left == {"502": Decimal("1000000"), "501": Decimal("100000")}


def test_duplicates_counted_once():
    item = ITEMS["INVOICED"][0]
    rep = build_report([item, dict(item)], "2026-11", STAFF, {"501": 25}, {})
    assert rep.doctor("501").total == Decimal("500000")


def test_month_ranges_cover_period():
    start, end = date(2024, 11, 1), date(2026, 11, 30)
    ranges = month_ranges(start, end)
    assert ranges[0][0] == start and ranges[-1][1] == end
    for (a, b), (c, _) in zip(ranges, ranges[1:]):
        assert (c - b).days == 1
    assert parse_month("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))


def test_api_fail_raises():
    client = CliniccardsClient("test-token", base_url="https://cliniccards.test/api",
                               transport=httpx.MockTransport(fake_api), limiter=RateLimiter(1000, 1))
    with pytest.raises(CliniccardsError):
        client.get("/nomalum")


def test_excel(svc):
    from excel import report_xlsx
    from openpyxl import load_workbook
    import io
    wb = load_workbook(io.BytesIO(report_xlsx(svc.month_report("2026-11"))))
    assert wb.sheetnames[0] == "Jami" and len(wb.sheetnames) == 5


def _client(svc, monkeypatch):
    monkeypatch.setenv("HISOBOT_PAROL", "maxfiy")
    monkeypatch.setenv("HISOBOT_LOGIN", "admin")
    import app as webapp
    from fastapi.testclient import TestClient
    monkeypatch.setattr(webapp, "_service", svc)
    monkeypatch.setattr(webapp, "attempts", __import__("auth").Attempts())
    import sklad_routes
    from db import DB
    monkeypatch.setitem(sklad_routes._state, "db", DB(":memory:"))
    return TestClient(webapp.app)


def test_login_flow(svc, monkeypatch):
    c = _client(svc, monkeypatch)
    r = c.get("/shifokor/501?oy=2026-11", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    r = c.get("/login")
    assert r.status_code == 200 and "Kirish" in r.text and "WWW-Authenticate" not in r.headers
    r = c.post("/login", data={"login": "admin", "parol": "xato", "next": "/"})
    assert r.status_code == 401 and "noto&#39;g&#39;ri" in r.text
    r = c.post("/login", data={"login": "admin", "parol": "maxfiy", "next": "/shifokor/501?oy=2026-11"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/shifokor/501?oy=2026-11"
    assert "httponly" in r.headers["set-cookie"].lower()
    r = c.get("/shifokor/501?oy=2026-11")   # alohida sahifa yo'q: jadvalda o'sha qator ochiladi
    assert "Karimova madina" in r.text and 'data-sub="501" >' in r.text.replace("  ", " ")
    c.post("/logout")
    assert c.get("/", follow_redirects=False).status_code == 303


def test_login_open_redirect_and_bruteforce(svc, monkeypatch):
    c = _client(svc, monkeypatch)
    r = c.post("/login", data={"login": "admin", "parol": "maxfiy", "next": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    c.cookies.clear()
    for _ in range(5):
        c.post("/login", data={"login": "admin", "parol": "xato"})
    r = c.post("/login", data={"login": "admin", "parol": "maxfiy"})
    assert r.status_code == 429


def test_forged_cookie_rejected(svc, monkeypatch):
    import auth
    c = _client(svc, monkeypatch)
    c.cookies.set(auth.COOKIE, "admin|9999999999|deadbeef")
    assert c.get("/", follow_redirects=False).status_code == 303


def test_web(svc, tmp_path, monkeypatch):
    c = _client(svc, monkeypatch)
    c.post("/login", data={"login": "admin", "parol": "maxfiy"})
    r = c.get("/shifokorlar?oy=2026-11")
    assert r.status_code == 200 and "125 000" in r.text and "Chiqish" in r.text
    r = c.get("/shifokor/501?oy=2026-11", follow_redirects=False)
    assert r.headers["location"] == "/shifokorlar?oy=2026-11&ochiq=501#d-501"
    assert c.get("/excel?oy=2026-11&shifokor=501").content[:2] == b"PK"
    assert "Qarzdorlar" not in c.get("/").text and "Hamshiralar" in c.get("/").text
    # foizlar o'zgarmas: sozlama sahifasi yo'q
    assert c.get("/sozlamalar", follow_redirects=False).headers["location"] == "/"
    assert "Foizlar" not in c.get("/").text
    assert c.get("/?oy=2026-13").status_code == 400



def test_full_history_finds_old_work(svc):
    rep = svc.month_report("2026-11")
    old = rep.doctor("503")
    assert old is not None and old.name == "Eski Shifokor"
    assert old.rows[0].done_date == "2017-05-10" and old.total == Decimal("300000")


def test_start_date_env(monkeypatch):
    from service import start_date_from_env
    monkeypatch.delenv("BOSHLANISH_SANA", raising=False)
    assert start_date_from_env() == date(2015, 1, 1)
    monkeypatch.setenv("BOSHLANISH_SANA", "2012-03-01")
    assert start_date_from_env() == date(2012, 3, 1)
    monkeypatch.setenv("BOSHLANISH_SANA", "xato")
    with pytest.raises(ValueError):
        start_date_from_env()


def test_date_range(svc):
    from report import Period
    # 1-4 noyabr: madina (3-noyabr) va belgilanmagan rentgen (1-noyabr), Bobur (5-noyabr) kirmaydi
    rep = svc.month_report(Period(date(2026, 11, 1), date(2026, 11, 4)))
    assert rep.doctor("501").total == Decimal("500000")
    assert rep.doctor("502") is None
    assert rep.oy == "01.11.2026 – 04.11.2026"
    # oktabr-noyabr: Bobur ikki to'lovi
    rep = svc.month_report(Period(date(2026, 10, 1), date(2026, 11, 30)))
    assert rep.doctor("502").total == Decimal("2000000")
    assert Period.month("2026-11").label == "noyabr 2026" and Period.month("2026-11").query == "oy=2026-11"


def test_web_date_range(svc, monkeypatch):
    c = _client(svc, monkeypatch)
    c.post("/login", data={"login": "admin", "parol": "maxfiy"})
    r = c.get("/shifokorlar?dan=2026-10-01&gacha=2026-11-30")
    assert r.status_code == 200 and "01.10.2026 – 30.11.2026" in r.text
    assert "/shifokor/502/maosh.xlsx" not in r.text   # hali maosh berilmagan
    assert r.text.count('type="date"') == 3          # davr + «hisob qachondan boshlanadi»
    assert "2 000 000" in r.text
    r = c.get("/shifokor/502?dan=2026-10-01&gacha=2026-11-30", follow_redirects=False)
    assert r.headers["location"] == "/shifokorlar?dan=2026-10-01&gacha=2026-11-30&ochiq=502#d-502"
    x = c.get("/excel?dan=2026-10-01&gacha=2026-11-30")
    assert x.content[:2] == b"PK" and "2026-10-01_2026-11-30" in x.headers["content-disposition"]
    assert c.get("/?dan=2026-11-30&gacha=2026-11-01").status_code == 400
    assert c.get("/?dan=xato&gacha=2026-11-01").status_code == 400


def test_overview_numbers(svc):
    o = svc.overview("2026-11", staff_salary=200000)
    assert o.done_total == Decimal("150000") and o.done_count == 2   # Rentgen 50 000 + Konsultatsiya 100 000
    assert o.kassa_total == Decimal("2050000") and o.clinic_share == Decimal("410000")
    assert o.doctors_salary == Decimal("450000")   # 125 000 + 250 000 + 75 000 (har bir shifokor 25%)
    assert o.material_total == Decimal("900000") and o.material_count == 3
    assert o.expenses_total == Decimal("410000") + Decimal("450000") + Decimal("200000") + Decimal("900000")
    assert o.profit == Decimal("2050000") - o.expenses_total   # sof foyda kassadan hisoblanadi



def test_details(svc):
    done = svc.done_detail("2026-11")
    assert {g.name: g.total for g in done} == {"Karimova madina": Decimal("100000"),
                                              "Shifokor belgilanmagan": Decimal("50000")}
    assert done[0].left_total == Decimal("100000")
    kassa = svc.kassa_detail("2026-11")
    assert {g.name: g.total for g in kassa} == {"Naqd": Decimal("750000"), "Terminal": Decimal("1300000")}
    assert kassa[0].rows[0]["patient"] == "Rasulova Dilnoza"
    exp = svc.expenses_detail("2026-11")
    assert {g.name: g.total for g in exp} == {"Ijara": Decimal("600000"), "Materiallar": Decimal("300000")}


def test_salary_spendings_split_and_payouts():
    import finance
    staff = [{"doctor_id": "501", "lastname": "Karimova", "firstname": "madina"},
             {"doctor_id": "502", "lastname": "Aliyev", "firstname": "Bobur"}]
    spend = [
        {"amount": "100000", "paid": "100000", "member": {"id": 501, "lastname": "Karimova", "firstname": "madina"}},
        {"amount": "80000", "paid": "50000", "member": "Bobur Aliyev"},
        {"amount": "30000", "spendings_category": {"name": "Зарплата"}},
        {"amount": "400000", "spendings_category": {"name": "Materiallar"}},
    ]
    salary, material = finance.split_spendings(spend)
    assert len(salary) == 3 and [m["amount"] for m in material] == ["400000"]
    assert finance.payouts_by_doctor(salary, staff) == {"501": Decimal("100000"), "502": Decimal("50000")}



def test_dashboard_pages(svc, monkeypatch):
    c = _client(svc, monkeypatch)
    c.post("/login", data={"login": "admin", "parol": "maxfiy"})
    r = c.get("/?oy=2026-11")
    assert r.status_code == 200
    for text in ("Bajarilgan ishlar", "Kassaga tushgan", "Xarajatlar", "Klinika sof foydasi",
                 "150 000", "2 050 000", "1 760 000", "290 000", "/foyda?oy=2026-11"):
        assert text in r.text, text
    assert "Konsultatsiya" in c.get("/ishlar?oy=2026-11").text
    k = c.get("/kassa?oy=2026-11").text
    assert "Terminal" in k and "1 300 000" in k
    assert "Karimova madina" in k and "Aliyev Bobur" in k and "belgilanmagan" in k   # to'lov qaysi shifokor ishi uchun
    assert 'id="kdoc"' in k
    assert "410 000" in c.get("/klinika?oy=2026-11").text
    x = c.get("/xarajatlar?oy=2026-11").text
    for text in ("Klinika ulushi", "Shifokorlar maoshi", "Hamshiralar maoshi", "Material rasxod", "900 000"):
        assert text in x, text
    assert "Ijara" not in x   # xarajatlar ro'yxati olib tashlangan, faqat 4 ta summa
    assert "/material?oy=2026-11" in x
    m = c.get("/material?oy=2026-11").text   # material rasxod qaysi xarajatlardan yig'ilgani
    for text in ("Ijara", "Bino ijarasi", "Dental Market", "02.11.2026", "-100 000", "900 000"):
        assert text in m, text
    assert 'id="mcat"' in m and "<option>Materiallar</option>" in m   # filtr: toifa, nima uchun, kimga, summa
    f = c.get("/foyda?oy=2026-11").text
    assert "Klinika sof foydasi" in f and "290 000" in f and "Kassaga tushgan" in f
    d = c.get("/shifokorlar?oy=2026-11").text
    assert "Karimova madina" in d and "Berilgan summa" in d and "Berilmagan summa" in d
    assert c.get("/qanday-ishlaydi").status_code == 200
