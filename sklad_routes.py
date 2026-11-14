"""«Sklad» bo'limi: qoldiq, sotuvchilar, xodimlar, hisobotlar, buyurtmalar va bot boshqaruvi."""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

import stock
from db import DB, new_token

router = APIRouter()
_state: dict = {"db": None, "bot": None}

STATUS_UZ = {"soraldi": "javob kutilmoqda", "olinmadi": "olinmadi", "kiritilmoqda": "kiritilmoqda",
             "tekshiruvda": "tekshiruvda", "tasdiqlandi": "tasdiqlandi", "farq": "farq bilan tasdiqlandi",
             "cc_chiqarildi": "Cliniccards'dan chiqarildi",
             "taklif": "taklif (admin qarori kutilmoqda)", "yuborildi": "sotuvchiga yuborildi",
             "qabul": "sotuvchi qabul qildi", "keldi": "keldi (kirim qilindi)", "bekor": "bekor qilindi"}
STATUS_CLS = {"tasdiqlandi": "good", "cc_chiqarildi": "good", "olinmadi": "good", "farq": "warn", "tekshiruvda": "warn",
              "soraldi": "off", "kiritilmoqda": "off", "taklif": "warn", "yuborildi": "warn", "qabul": "warn",
              "keldi": "good", "bekor": "off"}
STOCK_CLS = {"tugagan": "bad", "kam": "warn", "yetarli": "good"}
MAX_PHOTO = 1_500_000
DATA_URL = re.compile(r"^data:(image/(?:jpeg|png|webp|gif));base64,([A-Za-z0-9+/=]+)$")


def db_path() -> Path:
    default = Path(os.environ.get("FOIZLAR_FAYL", "foizlar.json")).with_name("hisobot.db")
    return Path(os.environ.get("DB_FAYL") or default)


def get_db() -> DB:
    if _state["db"] is None:
        _state["db"] = DB(db_path())
    return _state["db"]


def get_bot():
    return _state["bot"]


def setup(app, templates, auth, service):
    """app.py dan chaqiriladi: marshrutlarni ulaydi va botni ishga tushiradi."""
    templates.env.globals["holat"] = STATUS_UZ
    templates.env.globals["holat_cls"] = STATUS_CLS
    templates.env.globals["stock_cls"] = STOCK_CLS
    templates.env.filters["son"] = stock.fmt_qty

    def page(request, name, **ctx):
        ctx.setdefault("section", "sklad")
        return templates.TemplateResponse(request, name, {"bot": get_bot(), **ctx})

    @router.get("/sklad")
    def sklad(request: Request, f: str = "", v: str = "", _=Depends(auth)):
        db = get_db()
        all_rows = stock.products(db)
        rows = [r for r in all_rows if not f or stock.status(r) == f]
        counts = {k: 0 for k in ("tugagan", "kam", "yetarli")}
        for r in all_rows:
            counts[stock.status(r)] += 1
        value = sum(max(0, r["qty"]) * r["last_price"] for r in all_rows)
        view = v if v in ("list", "cards") else request.cookies.get("sklad_view", "list")
        photos = {r["product_id"] for r in db.q("SELECT product_id FROM product_photos")}
        resp = page(request, "sklad.html", rows=rows, all_count=len(all_rows), f=f, counts=counts, status=stock.status,
                    value=value, rows_value=sum(max(0, r["qty"]) * r["last_price"] for r in rows), view=view,
                    photos=photos, synced=db.meta("stock_synced"))
        if v in ("list", "cards"):
            resp.set_cookie("sklad_view", v, max_age=365 * 24 * 3600, samesite="lax")
        return resp

    # --- mahsulot rasmlari ---------------------------------------------
    @router.get("/sklad/rasm/{pid}")
    def photo(pid: str, _=Depends(auth)):
        row = get_db().one("SELECT mime, data FROM product_photos WHERE product_id=?", (pid,))
        if not row:
            raise HTTPException(404, "Rasm yo'q")
        return Response(bytes(row["data"]), media_type=row["mime"], headers={"Cache-Control": "private, max-age=300"})

    @router.post("/sklad/rasm/{pid}")
    def photo_save(pid: str, data: str = Form(...), _=Depends(auth)):
        db = get_db()
        if not db.one("SELECT 1 FROM products WHERE id=?", (pid,)):
            raise HTTPException(404, "Mahsulot topilmadi")
        m = DATA_URL.match(data.strip())
        if not m:
            raise HTTPException(400, "Rasm noto'g'ri formatda")
        raw = base64.b64decode(m.group(2))
        if len(raw) > MAX_PHOTO:
            raise HTTPException(400, "Rasm juda katta")
        from db import now
        db.run("INSERT INTO product_photos(product_id, mime, data, updated_at) VALUES(?,?,?,?) "
               "ON CONFLICT(product_id) DO UPDATE SET mime=excluded.mime, data=excluded.data, updated_at=excluded.updated_at",
               (pid, m.group(1), raw, now()))
        return {"ok": True}

    @router.post("/sklad/rasm/{pid}/ochirish")
    def photo_delete(pid: str, _=Depends(auth)):
        get_db().run("DELETE FROM product_photos WHERE product_id=?", (pid,))
        return {"ok": True}

    @router.post("/sklad/sinxron")
    def sklad_sync(request: Request, _=Depends(auth)):
        svc = service()
        res = stock.sync(get_db(), svc.client, svc.start_date)
        if get_bot():
            get_bot().propose_orders()
        msg = (f"Boshlang'ich qoldiq {res['invoices']} ta nakladnoydan hisoblandi" if res["first"]
               else f"{res['new_income_items']} ta yangi kirim qo'shildi")
        return RedirectResponse(f"/sklad?xabar={msg}", status_code=303)

    @router.get("/sklad/diagnostika")
    def diagnostics(request: Request, oy: int = 12, _=Depends(auth)):
        """Cliniccards sklad ma'lumoti qanday ko'rinishda kelishini ko'rsatadi (ombor qismini to'g'ri qurish uchun)."""
        import json
        from datetime import date as _date
        from report import shift_months
        oy = max(1, min(oy, 240))
        svc = service()
        end = _date.today()
        start = shift_months(_date(end.year, end.month, 1), -(oy - 1))
        invoices = stock.fetch_invoices(svc.client, start, end, 3)
        products = svc.client.get("/stock-products") or []
        types: dict[str, dict] = {}
        rooms: dict[str, str] = {}
        item_keys, dim_types, measures = set(), {}, {}
        for inv in invoices:
            t = str(inv.get("type"))
            d = types.setdefault(t, {"n": 0, "lines": 0, "first": inv.get("date"), "last": inv.get("date"),
                                     "suppliers": set(), "from_rooms": set(), "rooms": set(), "example": inv})
            d["n"] += 1
            d["lines"] += len(inv.get("invoice_items") or [])
            d["first"] = min(str(d["first"] or ""), str(inv.get("date") or ""))
            d["last"] = max(str(d["last"] or ""), str(inv.get("date") or ""))
            if inv.get("supplier_name"):
                d["suppliers"].add(inv["supplier_name"])
            for key, name, bucket in (("room_id", "room_name", "rooms"), ("from_room_id", "from_room_name", "from_rooms")):
                if inv.get(key) and str(inv.get(key)) != "0":
                    rooms[str(inv[key])] = inv.get(name) or ""
                    d[bucket].add(inv.get(name) or str(inv[key]))
            for it in inv.get("invoice_items") or []:
                item_keys.update(it.keys())
                dim_types[str(it.get("dimension_type"))] = dim_types.get(str(it.get("dimension_type")), 0) + 1
                measures[f"{it.get('measure_name')} / {it.get('dimension_name')}"] = 1 + measures.get(
                    f"{it.get('measure_name')} / {it.get('dimension_name')}", 0)
        for d in types.values():
            d["example_json"] = json.dumps(d.pop("example"), ensure_ascii=False, indent=2)[:4000]
        return page(request, "diagnostics.html", oy=oy, start=start, end=end, invoices=len(invoices), types=types,
                    rooms=rooms, item_keys=sorted(item_keys), dim_types=dim_types, measures=measures,
                    products=len(products),
                    product_example=json.dumps(products[0], ensure_ascii=False, indent=2) if products else "")

    @router.get("/sklad/mahsulot/{pid}")
    def product(request: Request, pid: str, _=Depends(auth)):
        db = get_db()
        p = db.one("SELECT * FROM products WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404, "Mahsulot topilmadi")
        return page(request, "product.html", p=p, status=stock.status(p),
                    moves=db.q("SELECT * FROM movements WHERE product_id=? ORDER BY id DESC LIMIT 200", (pid,)),
                    suppliers=db.q("SELECT * FROM suppliers ORDER BY name"))

    @router.post("/sklad/mahsulot/{pid}")
    def product_save(pid: str, min_qty: float = Form(0), order_qty: float = Form(0), supplier_id: str = Form(""),
                     actual: str = Form(""), _=Depends(auth)):
        db = get_db()
        if min_qty < 0 or order_qty < 0:
            raise HTTPException(400, "Son manfiy bo'lmasligi kerak")
        db.run("UPDATE products SET min_qty=?, order_qty=?, supplier_id=? WHERE id=?",
               (min_qty, order_qty, int(supplier_id) if supplier_id else None, pid))
        if actual.strip():
            try:
                val = float(actual.replace(",", "."))
            except ValueError:
                raise HTTPException(400, "Qoldiq son bo'lishi kerak")
            db.set_qty(pid, val, "qo'lda", "dasturda qo'lda tuzatildi")
        if get_bot():
            get_bot().propose_orders()
        return RedirectResponse(f"/sklad/mahsulot/{pid}?saqlandi=1", status_code=303)

    # --- sotuvchilar ---------------------------------------------------
    def ledger():
        db = get_db()
        sups = db.q("SELECT * FROM suppliers ORDER BY name")
        products = {r["id"]: {"name": r["name"], "unit": r["unit"]} for r in db.q("SELECT id, name, unit FROM products")}
        return sups, service().supplier_ledger(sups, products)

    @router.get("/sotuvchilar")
    def suppliers(request: Request, _=Depends(auth)):
        sups, led = ledger()
        rows = [{"s": s, **led[s["id"]]} for s in sups]
        tot_b = sum((r["b"] for r in rows), 0)
        tot_p = sum((r["pd"] for r in rows), 0)
        return page(request, "suppliers.html", rows=rows, tot_b=tot_b, tot_p=tot_p, tot_debt=tot_b - tot_p)

    @router.get("/sotuvchi/{sid}")
    def supplier(request: Request, sid: int, _=Depends(auth)):
        sups, led = ledger()
        s = next((x for x in sups if x["id"] == sid), None)
        if not s:
            raise HTTPException(404, "Sotuvchi topilmadi")
        return page(request, "supplier.html", s=s, x=led[sid])

    @router.post("/sotuvchilar")
    def supplier_add(name: str = Form(...), contact: str = Form(""), phone: str = Form(""), _=Depends(auth)):
        if not name.strip():
            raise HTTPException(400, "Do'kon nomini kiriting")
        get_db().run("INSERT INTO suppliers(name, contact, phone, token) VALUES(?,?,?,?)",
                     (name.strip(), contact.strip(), phone.strip(), new_token()))
        return RedirectResponse("/sotuvchilar", status_code=303)

    @router.post("/sotuvchilar/{sid}")
    def supplier_edit(sid: int, name: str = Form(...), contact: str = Form(""), phone: str = Form(""),
                      unlink: str = Form(""), _=Depends(auth)):
        db = get_db()
        db.run("UPDATE suppliers SET name=?, contact=?, phone=? WHERE id=?", (name.strip(), contact.strip(), phone.strip(), sid))
        if unlink:
            db.run("UPDATE suppliers SET chat_id=NULL, token=? WHERE id=?", (new_token(), sid))
        return RedirectResponse(f"/sotuvchi/{sid}?xabar=Saqlandi", status_code=303)

    # --- xodimlar ------------------------------------------------------
    @router.get("/bot")
    def bot_page(request: Request, chat: str = "", _=Depends(auth)):
        import bot_chats
        tab = chat if chat in bot_chats.CHATS else next(iter(bot_chats.CHATS))
        return page(request, "bot.html", chats=bot_chats.CHATS, tab=tab, section="bot",
                    ask_time=os.environ.get("SKLAD_SOROV_VAQTI", "19:00"),
                    att_time=os.environ.get("DAVOMAT_VAQTI", "20:00"))

    @router.get("/bot/xodimlar")
    def people(request: Request, _=Depends(auth)):
        return page(request, "people.html", section="bot", rows=get_db().q("SELECT * FROM people ORDER BY name"),
                    ask_time=os.environ.get("SKLAD_SOROV_VAQTI", "19:00"),
                    att_time=os.environ.get("DAVOMAT_VAQTI", "20:00"))

    @router.post("/bot/xodimlar")
    def person_add(name: str = Form(...), is_taker: str = Form(""), is_checker: str = Form(""), is_admin: str = Form(""),
                   is_attendance: str = Form(""), _=Depends(auth)):
        if not name.strip():
            raise HTTPException(400, "Ismini kiriting")
        get_db().run("INSERT INTO people(name, is_taker, is_checker, is_admin, is_attendance, token) VALUES(?,?,?,?,?,?)",
                     (name.strip(), int(bool(is_taker)), int(bool(is_checker)), int(bool(is_admin)),
                      int(bool(is_attendance)), new_token()))
        return RedirectResponse("/bot/xodimlar", status_code=303)

    @router.post("/bot/xodimlar/{pid}")
    def person_edit(pid: int, name: str = Form(...), is_taker: str = Form(""), is_checker: str = Form(""),
                    is_admin: str = Form(""), is_attendance: str = Form(""), delete: str = Form(""), unlink: str = Form(""),
                    _=Depends(auth)):
        db = get_db()
        if delete:
            if db.one("SELECT 1 FROM reports WHERE person_id=? OR checked_by=?", (pid, pid)) or \
                    db.one("SELECT 1 FROM clinic_days WHERE reported_by=?", (pid,)):
                db.run("UPDATE people SET is_taker=0, is_checker=0, is_admin=0, is_attendance=0, chat_id=NULL WHERE id=?", (pid,))
            else:
                db.run("DELETE FROM people WHERE id=?", (pid,))
        else:
            db.run("UPDATE people SET name=?, is_taker=?, is_checker=?, is_admin=?, is_attendance=? WHERE id=?",
                   (name.strip(), int(bool(is_taker)), int(bool(is_checker)), int(bool(is_admin)),
                    int(bool(is_attendance)), pid))
            if unlink:
                db.run("UPDATE people SET chat_id=NULL, token=? WHERE id=?", (new_token(), pid))
        return RedirectResponse("/bot/xodimlar", status_code=303)

    # --- hisobotlar va buyurtmalar -------------------------------------
    @router.get("/sklad/hisobotlar")
    def reports(request: Request, _=Depends(auth)):
        db = get_db()
        reps = db.q("""SELECT r.*, pe.name AS person, ch.name AS checker FROM reports r
                       JOIN people pe ON pe.id=r.person_id LEFT JOIN people ch ON ch.id=r.checked_by
                       ORDER BY r.id DESC LIMIT 200""")
        items = {}
        for it in db.q("""SELECT ri.*, p.name, p.unit FROM report_items ri JOIN products p ON p.id=ri.product_id
                          WHERE ri.report_id IN (SELECT id FROM reports ORDER BY id DESC LIMIT 200)"""):
            items.setdefault(it["report_id"], []).append(it)
        return page(request, "reports.html", reps=reps, items=items)

    @router.get("/buyurtmalar")
    def orders(request: Request, _=Depends(auth)):
        rows = get_db().q("""SELECT o.*, p.name, p.unit, s.name AS supplier FROM orders o
                             JOIN products p ON p.id=o.product_id LEFT JOIN suppliers s ON s.id=o.supplier_id
                             ORDER BY o.id DESC LIMIT 300""")
        arrived = [o for o in rows if o["status"] == "keldi"]
        return page(request, "orders.html", rows=rows, arrived=len(arrived),
                    waiting=sum(1 for o in rows if o["status"] in ("taklif", "yuborildi", "qabul")),
                    arrived_sum=sum(o["qty"] * o["price"] for o in arrived))

    @router.post("/bot/sorash")
    def ask_now(_=Depends(auth)):
        b = get_bot()
        if not b:
            raise HTTPException(400, "Telegram bot ulanmagan (TELEGRAM_BOT_TOKEN)")
        n = b.ask_takers(force=True)
        return RedirectResponse(f"/bot/xodimlar?xabar={n} ta xodimga so'rov yuborildi", status_code=303)

    @router.post("/bot/davomat")
    def ask_attendance_now(_=Depends(auth)):
        b = get_bot()
        if not b:
            raise HTTPException(400, "Telegram bot ulanmagan (TELEGRAM_BOT_TOKEN)")
        n = b.ask_attendance(force=True)
        return RedirectResponse(f"/bot/xodimlar?xabar=Davomat so'rovi {n} ta xodimga yuborildi", status_code=303)

    @router.post("/bot/buyurtma-tekshir")
    def check_orders(_=Depends(auth)):
        b = get_bot()
        if not b:
            raise HTTPException(400, "Telegram bot ulanmagan (TELEGRAM_BOT_TOKEN)")
        n = b.propose_orders()
        return RedirectResponse(f"/buyurtmalar?xabar={n} ta yangi taklif adminlarga yuborildi", status_code=303)

    app.include_router(router)

    @app.on_event("startup")
    def start_bot():
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return
        import bot as botmod
        _state["bot"] = botmod.start_background(get_db(), token, os.environ.get("KLINIKA_NOMI", "Biodent"),
                                                os.environ.get("SKLAD_SOROV_VAQTI", "19:00"),
                                                os.environ.get("DAVOMAT_VAQTI", "20:00"))
