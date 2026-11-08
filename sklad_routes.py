"""«Sklad» bo'limi: qoldiq, sotuvchilar, xodimlar, hisobotlar, buyurtmalar va bot boshqaruvi."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

import stock
from db import DB, new_token

router = APIRouter()
_state: dict = {"db": None, "bot": None}

STATUS_UZ = {"soraldi": "javob kutilmoqda", "olinmadi": "olinmadi", "kiritilmoqda": "kiritilmoqda",
             "tekshiruvda": "tekshiruvda", "tasdiqlandi": "tasdiqlandi", "farq": "farq bilan tasdiqlandi",
             "cc_chiqarildi": "Cliniccards'dan chiqarildi",
             "taklif": "taklif (admin qarori kutilmoqda)", "yuborildi": "sotuvchiga yuborildi",
             "qabul": "sotuvchi qabul qildi", "bekor": "bekor qilindi"}


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
    templates.env.filters["son"] = stock.fmt_qty

    def page(request, name, **ctx):
        return templates.TemplateResponse(request, name, {"bot": get_bot(), **ctx})

    @router.get("/sklad")
    def sklad(request: Request, f: str = "", _=Depends(auth)):
        db = get_db()
        rows = stock.products(db)
        if f:
            rows = [r for r in rows if stock.status(r) == f]
        counts = {k: 0 for k in ("tugagan", "kam", "yetarli")}
        for r in stock.products(db):
            counts[stock.status(r)] += 1
        return page(request, "sklad.html", rows=rows, f=f, counts=counts, status=stock.status,
                    synced=db.meta("stock_synced"), snapshot=db.meta("stock_snapshot"),
                    suppliers=db.q("SELECT * FROM suppliers ORDER BY name"))

    @router.post("/sklad/sinxron")
    def sklad_sync(request: Request, _=Depends(auth)):
        svc = service()
        res = stock.sync(get_db(), svc.client, svc.start_date)
        if get_bot():
            get_bot().propose_orders()
        msg = (f"Boshlang'ich qoldiq {res['invoices']} ta nakladnoydan hisoblandi" if res["first"]
               else f"{res['new_income_items']} ta yangi kirim qo'shildi")
        return RedirectResponse(f"/sklad?xabar={msg}", status_code=303)

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
    @router.get("/sotuvchilar")
    def suppliers(request: Request, _=Depends(auth)):
        db = get_db()
        rows = db.q("""SELECT s.*, (SELECT count(*) FROM products p WHERE p.supplier_id=s.id AND p.active=1) AS n
                       FROM suppliers s ORDER BY s.name""")
        return page(request, "suppliers.html", rows=rows)

    @router.post("/sotuvchilar")
    def supplier_add(name: str = Form(...), phone: str = Form(""), _=Depends(auth)):
        if not name.strip():
            raise HTTPException(400, "Nomini kiriting")
        get_db().run("INSERT INTO suppliers(name, phone, token) VALUES(?,?,?)", (name.strip(), phone.strip(), new_token()))
        return RedirectResponse("/sotuvchilar", status_code=303)

    @router.post("/sotuvchilar/{sid}")
    def supplier_edit(sid: int, name: str = Form(...), phone: str = Form(""), unlink: str = Form(""), _=Depends(auth)):
        db = get_db()
        db.run("UPDATE suppliers SET name=?, phone=? WHERE id=?", (name.strip(), phone.strip(), sid))
        if unlink:
            db.run("UPDATE suppliers SET chat_id=NULL, token=? WHERE id=?", (new_token(), sid))
        return RedirectResponse("/sotuvchilar", status_code=303)

    # --- xodimlar ------------------------------------------------------
    @router.get("/xodimlar")
    def people(request: Request, _=Depends(auth)):
        return page(request, "people.html", rows=get_db().q("SELECT * FROM people ORDER BY name"),
                    ask_time=os.environ.get("SKLAD_SOROV_VAQTI", "18:00"))

    @router.post("/xodimlar")
    def person_add(name: str = Form(...), is_taker: str = Form(""), is_checker: str = Form(""), is_admin: str = Form(""),
                   _=Depends(auth)):
        if not name.strip():
            raise HTTPException(400, "Ismini kiriting")
        get_db().run("INSERT INTO people(name, is_taker, is_checker, is_admin, token) VALUES(?,?,?,?,?)",
                     (name.strip(), int(bool(is_taker)), int(bool(is_checker)), int(bool(is_admin)), new_token()))
        return RedirectResponse("/xodimlar", status_code=303)

    @router.post("/xodimlar/{pid}")
    def person_edit(pid: int, name: str = Form(...), is_taker: str = Form(""), is_checker: str = Form(""),
                    is_admin: str = Form(""), delete: str = Form(""), unlink: str = Form(""), _=Depends(auth)):
        db = get_db()
        if delete:
            if db.one("SELECT 1 FROM reports WHERE person_id=? OR checked_by=?", (pid, pid)):
                db.run("UPDATE people SET is_taker=0, is_checker=0, is_admin=0, chat_id=NULL WHERE id=?", (pid,))
            else:
                db.run("DELETE FROM people WHERE id=?", (pid,))
        else:
            db.run("UPDATE people SET name=?, is_taker=?, is_checker=?, is_admin=? WHERE id=?",
                   (name.strip(), int(bool(is_taker)), int(bool(is_checker)), int(bool(is_admin)), pid))
            if unlink:
                db.run("UPDATE people SET chat_id=NULL, token=? WHERE id=?", (new_token(), pid))
        return RedirectResponse("/xodimlar", status_code=303)

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
        total = sum(o["qty"] * o["price"] for o in rows if o["status"] in ("yuborildi", "qabul"))
        return page(request, "orders.html", rows=rows, total=total)

    @router.post("/bot/sorash")
    def ask_now(_=Depends(auth)):
        b = get_bot()
        if not b:
            raise HTTPException(400, "Telegram bot ulanmagan (TELEGRAM_BOT_TOKEN)")
        n = b.ask_takers(force=True)
        return RedirectResponse(f"/xodimlar?xabar={n} ta xodimga so'rov yuborildi", status_code=303)

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
                                                os.environ.get("SKLAD_SOROV_VAQTI", "18:00"))
