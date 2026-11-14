"""«Xodimlar» bo'limi: maosh, grafik va xodim vazifalari (shifokor bo'lmagan xodimlar)."""
from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

import staff
from db import new_token, now
from report import Period
from sklad_routes import get_db

router = APIRouter()

TG_RE = re.compile(r"^(@[A-Za-z0-9_]{5,32}|-?\d{5,15})$")
BOT_ROLES = [
    ("is_taker", "Skladdan material oladi", "har kuni sklad so'rovi vaqtida «skladdan nima oldingiz?» deb so'raladi"),
    ("is_checker", "Skladni tekshiradi", "ertalab skladni sanab, qoldiqni tasdiqlaydi"),
    ("is_attendance", "Davomat beradi", "har kuni davomat vaqtida «kimlar keldi?» deb so'raladi"),
    ("is_admin", "Admin xabarlarini oladi", "Cliniccards eslatmalari va buyurtma takliflari keladi"),
]
ROLE_KEYS = {k for k, _, _ in BOT_ROLES}


def _int(v: str, name: str) -> int:
    try:
        n = int(float(str(v).replace(" ", "").replace(",", ".") or 0))
    except ValueError:
        raise HTTPException(400, f"{name} son bo'lishi kerak")
    if n < 0:
        raise HTTPException(400, f"{name} manfiy bo'lmasligi kerak")
    return n


def setup(app, templates, auth, get_period, period_context):
    def page(request, name, **ctx):
        from sklad_routes import get_bot
        return templates.TemplateResponse(request, name, {"bot": get_bot(), "section": "xodimlar", **ctx})

    @router.get("/xodimlar")
    def workers(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
        db = get_db()
        pays = staff.payroll(db, p.start, p.end)
        return page(request, "workers.html", rows=db.q("SELECT * FROM workers WHERE active=1 ORDER BY full_name"),
                    pays=pays, total=sum(w.total for w in pays), **period_context(p))

    @router.get("/maosh-xodimlar")
    def workers_old(request: Request):
        return RedirectResponse("/xodimlar" + (f"?{request.url.query}" if request.url.query else ""), status_code=303)

    @router.post("/xodimlar/saqlash")
    async def workers_save(request: Request, _=Depends(auth)):
        """⚙ oynasidan: barcha xodimlar bir yo'la saqlanadi; ro'yxatdan olib tashlanganlar «faol emas» bo'ladi."""
        form = await request.form()
        ids, names, poss = form.getlist("id"), form.getlist("name"), form.getlist("pos")
        dailies, extras, tgs = form.getlist("daily"), form.getlist("extra"), form.getlist("tg")
        if not (len(ids) == len(names) == len(poss) == len(dailies) == len(extras) == len(tgs)):
            raise HTTPException(400, "Forma to'liq emas")
        db = get_db()
        keep = set()
        rows = []
        for wid, name, pos, daily, extra, tg in zip(ids, names, poss, dailies, extras, tgs):
            name, tg = name.strip(), tg.replace(" ", "")
            if not name and not str(daily).strip():
                continue
            if not name:
                raise HTTPException(400, "Har bir xodimning ism-familiyasini kiriting")
            d, e = _int(daily, "Kunlik summa"), _int(extra, "Qo'shimcha summa")
            if d <= 0:
                raise HTTPException(400, f"{name}: kunlik summa 0 dan katta bo'lishi kerak")
            if tg and not TG_RE.match(tg):
                raise HTTPException(400, f"{name}: Telegram — @username (kamida 5 belgi) yoki raqamli ID bo'lishi kerak")
            rows.append((wid, name, pos.strip(), d, e, tg))
        if not rows:
            raise HTTPException(400, "Kamida bitta xodim bo'lishi kerak")
        with db.tx():
            for wid, name, pos, d, e, tg in rows:
                if wid and db.one("SELECT 1 FROM workers WHERE id=?", (int(wid),)):
                    db.conn.execute("UPDATE workers SET full_name=?, position=?, daily_rate=?, extra_rate=?, tg=?, active=1 "
                                    "WHERE id=?", (name, pos, d, e, tg, int(wid)))
                    keep.add(int(wid))
                else:
                    cur = db.conn.execute("INSERT INTO workers(full_name, position, daily_rate, extra_rate, tg, created_at) "
                                          "VALUES(?,?,?,?,?,?)", (name, pos, d, e, tg, now()))
                    keep.add(cur.lastrowid)
            for r in db.conn.execute("SELECT id FROM workers WHERE active=1").fetchall():
                if r["id"] not in keep:
                    db.conn.execute("UPDATE workers SET active=0 WHERE id=?", (r["id"],))
        return RedirectResponse("/xodimlar?xabar=Saqlandi", status_code=303)

    @router.get("/grafik")
    def schedule(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
        db = get_db()
        if (p.end - p.start).days > 62:
            raise HTTPException(400, "Grafik uchun 2 oydan uzun bo'lmagan davr tanlang")
        g = staff.grid(db, p.start, p.end)
        pays = staff.payroll(db, p.start, p.end)
        return page(request, "schedule.html", days=staff.days(p.start, p.end), pays=pays, marks=g["marks"],
                    clinic=g["clinic"], rest=staff.rest_days(), wd=staff.WEEKDAYS,
                    total=sum(w.total for w in pays), **period_context(p))

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
        return RedirectResponse(back if back.startswith("/grafik") else "/grafik", status_code=303)

    # --- xodim vazifalari ---------------------------------------------
    @router.get("/vazifalar")
    def duties(request: Request, _=Depends(auth)):
        db = get_db()
        workers = db.q("SELECT * FROM workers WHERE active=1 ORDER BY full_name")
        tasks: dict[int, list] = {}
        for t in db.q("SELECT * FROM worker_tasks ORDER BY id"):
            tasks.setdefault(t["worker_id"], []).append(t)
        people = {r["worker_id"]: r for r in db.q("SELECT * FROM people WHERE worker_id IS NOT NULL")}
        return page(request, "duties.html", workers=workers, tasks=tasks, people=people, roles=BOT_ROLES)

    @router.post("/vazifalar/{wid}/qosh")
    def task_add(wid: int, text: str = Form(...), _=Depends(auth)):
        text = text.strip()
        if text:
            get_db().run("INSERT INTO worker_tasks(worker_id, text) VALUES(?,?)", (wid, text[:300]))
        return RedirectResponse(f"/vazifalar#w{wid}", status_code=303)

    @router.post("/vazifalar/ochir/{tid}")
    def task_delete(tid: int, _=Depends(auth)):
        db = get_db()
        row = db.one("SELECT worker_id FROM worker_tasks WHERE id=?", (tid,))
        db.run("DELETE FROM worker_tasks WHERE id=?", (tid,))
        return RedirectResponse(f"/vazifalar#w{row['worker_id']}" if row else "/vazifalar", status_code=303)

    @router.post("/vazifalar/{wid}/rol")
    def role_set(wid: int, role: str = Form(...), on: str = Form(""), _=Depends(auth)):
        """Botdagi vazifa: xodimga bog'langan bot foydalanuvchisi (people) yaratiladi yoki yangilanadi."""
        if role not in ROLE_KEYS:
            raise HTTPException(400, "Vazifa noto'g'ri")
        db = get_db()
        w = db.one("SELECT * FROM workers WHERE id=?", (wid,))
        if not w:
            raise HTTPException(404, "Xodim topilmadi")
        person = db.one("SELECT * FROM people WHERE worker_id=?", (wid,))
        if not person:
            db.run("INSERT INTO people(name, worker_id, token) VALUES(?,?,?)", (w["full_name"], wid, new_token()))
        db.run(f"UPDATE people SET {role}=? WHERE worker_id=?", (int(bool(on)), wid))
        return RedirectResponse(f"/vazifalar#w{wid}", status_code=303)

    app.include_router(router)
