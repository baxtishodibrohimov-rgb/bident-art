"""«Xodimlar maoshi» va «Grafik» sahifalari (shifokor bo'lmagan xodimlar)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

import staff
from db import now
from report import Period
from sklad_routes import get_db

router = APIRouter()


def _int(v: str, name: str) -> int:
    try:
        n = int(float(str(v).replace(" ", "").replace(",", ".") or 0))
    except ValueError:
        raise HTTPException(400, f"{name} son bo'lishi kerak")
    if n < 0:
        raise HTTPException(400, f"{name} manfiy bo'lmasligi kerak")
    return n


def setup(app, templates, auth, get_period, period_context):
    @router.get("/maosh-xodimlar")
    def workers(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
        db = get_db()
        pays = staff.payroll(db, p.start, p.end)
        return templates.TemplateResponse(request, "workers.html", {
            "rows": db.q("SELECT * FROM workers ORDER BY active DESC, full_name"), "pays": pays,
            "total": sum(w.total for w in pays), **period_context(p)})

    @router.post("/maosh-xodimlar")
    def worker_add(full_name: str = Form(...), position: str = Form(""), daily_rate: str = Form("0"),
                   extra_rate: str = Form("0"), _=Depends(auth)):
        if not full_name.strip():
            raise HTTPException(400, "Ism-familiyani kiriting")
        get_db().run("INSERT INTO workers(full_name, position, daily_rate, extra_rate, created_at) VALUES(?,?,?,?,?)",
                     (full_name.strip(), position.strip(), _int(daily_rate, "Kunlik summa"),
                      _int(extra_rate, "Qo'shimcha summa"), now()))
        return RedirectResponse("/maosh-xodimlar?xabar=Xodim qo'shildi", status_code=303)

    @router.post("/maosh-xodimlar/{wid}")
    def worker_edit(wid: int, full_name: str = Form(...), position: str = Form(""), daily_rate: str = Form("0"),
                    extra_rate: str = Form("0"), active: str = Form(""), _=Depends(auth)):
        get_db().run("UPDATE workers SET full_name=?, position=?, daily_rate=?, extra_rate=?, active=? WHERE id=?",
                     (full_name.strip(), position.strip(), _int(daily_rate, "Kunlik summa"),
                      _int(extra_rate, "Qo'shimcha summa"), int(bool(active)), wid))
        return RedirectResponse("/maosh-xodimlar?xabar=Saqlandi", status_code=303)

    @router.get("/grafik")
    def schedule(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
        db = get_db()
        if (p.end - p.start).days > 92:
            raise HTTPException(400, "Grafik uchun 3 oydan uzun bo'lmagan davr tanlang")
        g = staff.grid(db, p.start, p.end)
        pays = staff.payroll(db, p.start, p.end)
        return templates.TemplateResponse(request, "schedule.html", {
            "days": staff.days(p.start, p.end), "pays": pays, "marks": g["marks"], "clinic": g["clinic"],
            "rest": staff.rest_days(), "wd": staff.WEEKDAYS, "total": sum(w.total for w in pays), **period_context(p)})

    @router.post("/grafik/belgi")
    def mark(day: str = Form(...), worker_id: int = Form(...), state: str = Form(...), back: str = Form("/grafik"),
             _=Depends(auth)):
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "Sana noto'g'ri")
        if state not in ("yoq", "keldi", "qoshimcha"):
            raise HTTPException(400, "Holat noto'g'ri")
        staff.set_mark(get_db(), day, worker_id, state)
        target = back if back.startswith("/grafik") else "/grafik"
        return RedirectResponse(target, status_code=303)

    app.include_router(router)
