"""Sklad va Telegram bot: soxta Cliniccards va soxta Telegram bilan to'liq zanjir."""
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stock  # noqa: E402
from bot import Bot, TelegramAPI  # noqa: E402
from client import CliniccardsClient, RateLimiter  # noqa: E402
from db import DB, new_token  # noqa: E402

PRODUCTS = [
    {"product_id": "1", "product_name": "Kompozit plomba A2", "category_name": "Terapiya", "measure_name": "dona"},
    {"product_id": "2", "product_name": "Anestetik Ubistezin", "category_name": "Anesteziya", "measure_name": "karpula"},
]
INVOICES = [
    {"invoice_id": "10", "invoice_number": "0001", "type": "INCOME", "date": "2025-03-01", "supplier_id": "77",
     "supplier_name": "Dental Market", "invoice_items": [
         {"product_id": 1, "count": 10, "price": 45000}, {"product_id": 2, "count": 50, "price": 9000}]},
    {"invoice_id": "11", "invoice_number": "0002", "type": "OUTCOME", "date": "2025-04-01", "invoice_items": [
        {"product_id": 1, "count": 3}]},
    {"invoice_id": "12", "invoice_number": "0003", "type": "MOVE", "date": "2025-04-02", "invoice_items": [
        {"product_id": 2, "count": 20}]},
]
NEW_INVOICES = []


def fake_cc(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api")
    ok = lambda d: httpx.Response(200, json={"data": d, "result": "success", "error": None})  # noqa: E731
    if path == "/stock-products":
        return ok(PRODUCTS)
    if path == "/stock-invoices":
        frm, to = request.url.params["from"], request.url.params["to"]
        return ok([i for i in INVOICES + NEW_INVOICES if frm <= i["date"] <= to])
    return httpx.Response(404, json={"data": None, "result": "fail", "error": "nf"})


class FakeTG:
    def __init__(self):
        self.sent = []   # (chat_id, text, buttons)
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "biodent_test_bot"}})
        if method == "sendMessage":
            self.n += 1
            buttons = [b["callback_data"] for row in (body.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]
            self.sent.append((body["chat_id"], body["text"], buttons))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.n}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def last(self, chat_id):
        return [m for m in self.sent if m[0] == chat_id][-1]


@pytest.fixture
def env():
    db = DB(":memory:")
    client = CliniccardsClient("t", base_url="https://cc.test/api", transport=httpx.MockTransport(fake_cc),
                               limiter=RateLimiter(1000, 1))
    tg = FakeTG()
    bot = Bot(db, TelegramAPI("x", transport=httpx.MockTransport(tg)), "Biodent")
    bot.identify()
    NEW_INVOICES.clear()
    return db, client, tg, bot


def msg(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text, "from": {}}}


def btn(chat, data):
    return {"update_id": 1, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": chat}, "message_id": 1}}}


def add_person(db, name, taker=0, checker=0, admin=0, chat=None):
    tok = new_token()
    db.run("INSERT INTO people(name, is_taker, is_checker, is_admin, token) VALUES(?,?,?,?,?)",
           (name, taker, checker, admin, tok))
    return tok


def test_opening_balance_and_new_income(env):
    db, client, tg, bot = env
    res = stock.sync(db, client, date(2015, 1, 1), date(2026, 11, 8))
    assert res["first"]
    q = {r["id"]: r["qty"] for r in db.q("SELECT id, qty FROM products")}
    assert q == {"1": 7, "2": 50}          # 10 − 3 chiqim; ko'chirish hisobga olinmaydi
    p1 = db.one("SELECT * FROM products WHERE id='1'")
    assert p1["last_price"] == 45000 and p1["supplier_id"] is not None
    assert db.one("SELECT name FROM suppliers")["name"] == "Dental Market"
    # yangi kirim qo'shiladi, yangi chiqim nakladnoyi esa qayta ayrilmaydi (bot orqali hisoblanadi)
    NEW_INVOICES.extend([
        {"invoice_id": "20", "type": "INCOME", "date": "2026-11-05", "supplier_name": "Dental Market",
         "invoice_items": [{"product_id": 1, "count": 5, "price": 47000}]},
        {"invoice_id": "21", "type": "OUTCOME", "date": "2026-11-06", "invoice_items": [{"product_id": 1, "count": 2}]},
    ])
    res = stock.sync(db, client, date(2015, 1, 1), date(2026, 11, 8))
    assert not res["first"] and res["new_income_items"] == 1
    p1 = db.one("SELECT * FROM products WHERE id='1'")
    assert p1["qty"] == 12 and p1["last_price"] == 47000
    # takroriy sinxron hech narsani o'zgartirmaydi
    stock.sync(db, client, date(2015, 1, 1), date(2026, 11, 8))
    assert db.one("SELECT qty FROM products WHERE id='1'")["qty"] == 12


def test_full_flow(env):
    db, client, tg, bot = env
    stock.sync(db, client, date(2015, 1, 1), date(2026, 11, 8))
    db.run("UPDATE products SET min_qty=5, order_qty=10 WHERE id='1'")
    t_ali = add_person(db, "Ali", taker=1)
    t_vali = add_person(db, "Vali", checker=1)
    t_boss = add_person(db, "Bosh shifokor", admin=1)
    sup = db.one("SELECT * FROM suppliers")

    # hammasi havoladan /start bosadi
    for chat, tok in ((101, t_ali), (102, t_vali), (103, t_boss), (200, sup["token"])):
        bot.handle(msg(chat, f"/start {tok}"))
    assert "ulandingiz" in tg.last(101)[1]
    bot.handle(msg(999, "/start notanish"))
    assert "maxsus havola" in tg.last(999)[1]

    # kunlik so'rov
    assert bot.ask_takers() == 1 and bot.ask_takers() == 0   # bir kunda bir marta
    chat, text, buttons = tg.last(101)
    assert "skladdan" in text and buttons[0].startswith("r:yes:")
    bot.handle(btn(101, buttons[0]))
    bot.handle(msg(101, "plomba"))
    pick = tg.last(101)[2]
    assert pick == ["pick:1"]
    bot.handle(btn(101, "pick:1"))
    bot.handle(msg(101, "abc"))
    assert "son" in tg.last(101)[1]
    bot.handle(msg(101, "3"))
    chat, text, buttons = tg.last(101)
    assert "Kompozit plomba A2 — 3 dona" in text
    bot.handle(btn(101, buttons[1]))   # Tugatdim

    # tekshiruvchiga: 7 − 3 = 4 qolishi kerak
    chat, text, buttons = tg.last(102)
    assert "4 dona qolishi kerak" in text and buttons[0].startswith("chk:ok:")
    bot.handle(btn(102, buttons[0]))
    assert db.one("SELECT qty FROM products WHERE id='1'")["qty"] == 4

    # adminga: Cliniccards'dan chiqarish + buyurtma taklifi (4 ≤ 5)
    admin_msgs = [m for m in tg.sent if m[0] == 103]
    cc = [m for m in admin_msgs if "Cliniccards'dan chiqarish" in m[1]][0]
    offer = [m for m in admin_msgs if "kam qoldi" in m[1]][0]
    assert "10 dona × 45 000 = ~450 000 so'm" in offer[1]
    bot.handle(btn(103, cc[2][0]))
    assert db.one("SELECT status FROM reports")["status"] == "cc_chiqarildi"

    # takroriy taklif yo'q
    assert bot.propose_orders() == 0
    bot.handle(btn(103, offer[2][0]))  # Yuborish
    chat, text, buttons = tg.last(200)
    assert "buyurtma" in text and "Kompozit plomba A2 — 10 dona" in text
    bot.handle(btn(200, buttons[0]))   # sotuvchi: qabul qildim
    assert db.one("SELECT status FROM orders")["status"] == "qabul"
    assert "qabul qildi" in tg.last(103)[1]

    # begona odam tugmani bossa ham hech narsa bo'lmaydi
    bot.handle(btn(999, "ord:send:1"))
    assert db.one("SELECT status FROM orders")["status"] == "qabul"


def test_checker_finds_difference(env):
    db, client, tg, bot = env
    stock.sync(db, client, date(2015, 1, 1), date(2026, 11, 8))
    t_ali, t_vali, t_boss = add_person(db, "Ali", taker=1), add_person(db, "Vali", checker=1), add_person(db, "Boss", admin=1)
    for chat, tok in ((101, t_ali), (102, t_vali), (103, t_boss)):
        bot.handle(msg(chat, f"/start {tok}"))
    bot.handle(msg(101, "/oldim"))
    bot.handle(msg(101, "ubistezin"))
    bot.handle(btn(101, "pick:2"))
    bot.handle(msg(101, "5"))
    bot.handle(btn(101, tg.last(101)[2][1]))
    chat, text, buttons = tg.last(102)
    assert "45 karpula qolishi kerak" in text
    bot.handle(btn(102, buttons[1]))   # Noto'g'ri
    assert "haqiqatda nechta" in tg.last(102)[1]
    bot.handle(msg(102, "40"))
    assert db.one("SELECT qty FROM products WHERE id='2'")["qty"] == 40
    assert db.one("SELECT status FROM reports")["status"] == "farq"
    farq = [m for m in tg.sent if m[0] == 103 and "farq" in m[1]][0][1]
    assert "kutilgan 45, haqiqatda 40 (-5 karpula)" in farq
    kinds = [r["kind"] for r in db.q("SELECT kind FROM movements WHERE product_id='2' ORDER BY id")]
    assert kinds == ["boshlangich", "chiqim", "tuzatish"]


def test_no_and_unlinked_supplier(env):
    db, client, tg, bot = env
    stock.sync(db, client, date(2015, 1, 1), date(2026, 11, 8))
    t_ali, t_boss = add_person(db, "Ali", taker=1), add_person(db, "Boss", admin=1)
    bot.handle(msg(101, f"/start {t_ali}"))
    bot.handle(msg(103, f"/start {t_boss}"))
    bot.ask_takers()
    bot.handle(btn(101, tg.last(101)[2][1]))   # Yo'q
    assert db.one("SELECT status FROM reports")["status"] == "olinmadi"
    bot.handle(btn(101, tg.last(101)[2][1] if tg.last(101)[2] else "r:no:1"))
    assert "allaqachon" in tg.last(101)[1]
    # sotuvchi botga ulanmagan → admin ogohlantiriladi, yuborish tugmasi yo'q
    db.run("UPDATE products SET min_qty=100 WHERE id='2'")
    assert bot.propose_orders() == 1
    text, buttons = tg.last(103)[1], tg.last(103)[2]
    assert "botga ulanmagan" in text and all(not b.startswith("ord:send") for b in buttons)


def test_sklad_pages(env, tmp_path, monkeypatch):
    db, client, tg, bot = env
    monkeypatch.setenv("HISOBOT_PAROL", "maxfiy")
    import app as webapp
    import sklad_routes
    from fastapi.testclient import TestClient
    from service import Percents, Service
    svc = Service(client, Percents(tmp_path / "f.json"), start_date=date(2015, 1, 1))
    monkeypatch.setattr(webapp, "_service", svc)
    monkeypatch.setitem(sklad_routes._state, "db", db)
    monkeypatch.setitem(sklad_routes._state, "bot", bot)
    monkeypatch.setattr(webapp, "attempts", __import__("auth").Attempts())
    c = TestClient(webapp.app)
    c.post("/login", data={"login": "admin", "parol": "maxfiy"})
    r = c.post("/sklad/sinxron", follow_redirects=True)
    assert "Boshlang" in r.text and "Kompozit plomba A2" in r.text
    r = c.post("/sklad/mahsulot/1", data={"min_qty": "8", "order_qty": "0", "supplier_id": "", "actual": "6"},
               follow_redirects=True)
    assert "Saqlandi" in r.text and db.one("SELECT qty FROM products WHERE id='1'")["qty"] == 6
    assert "kam" in c.get("/sklad?f=kam").text
    c.post("/xodimlar", data={"name": "Ali", "is_taker": "1"})
    r = c.get("/xodimlar")
    assert "https://t.me/biodent_test_bot?start=" in r.text
    c.post("/sotuvchilar", data={"name": "Ortho Supply", "phone": "+998901234567"})
    assert "Ortho Supply" in c.get("/sotuvchilar").text
    for path in ("/sklad/hisobotlar", "/buyurtmalar"):
        assert c.get(path).status_code == 200
    assert c.post("/bot/buyurtma-tekshir", follow_redirects=False).status_code == 303
