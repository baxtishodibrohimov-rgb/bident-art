"""Veb-sahifa: oylik hisobot, shifokor tafsiloti, qarzdorlar, foizlar sozlamasi.

Ishga tushirish:  uvicorn app:app --host 0.0.0.0 --port 8090
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import auth as sessions

from client import CliniccardsClient, CliniccardsError
from excel import report_xlsx
from report import Period
from service import HERE, Percents, Service, load_env, start_date_from_env

load_env()

templates = Jinja2Templates(directory=str(HERE / "templates"))


def fmt(value) -> str:
    if value is None:
        return "—"
    v = Decimal(value).quantize(Decimal("0.01"))
    s = f"{v:,.2f}".replace(",", " ")
    return s[:-3] if s.endswith(".00") else s


templates.env.filters["pul"] = fmt


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def check_month(oy: str | None) -> str:
    oy = oy or current_month()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", oy):
        raise HTTPException(400, "Oy YYYY-MM ko'rinishida bo'lishi kerak")
    return oy


def get_period(oy: str | None = None, dan: str | None = None, gacha: str | None = None) -> Period:
    """?oy=YYYY-MM (bitta oy) yoki ?dan=YYYY-MM-DD&gacha=YYYY-MM-DD (sana oralig'i)."""
    if dan or gacha:
        try:
            start = date.fromisoformat(dan or gacha)
            end = date.fromisoformat(gacha or dan)
        except ValueError:
            raise HTTPException(400, "Sana YYYY-MM-DD ko'rinishida bo'lishi kerak")
        if start > end:
            raise HTTPException(400, "«Dan» sanasi «Gacha» sanasidan keyin bo'lmasligi kerak")
        return Period(start, end)
    return Period.month(check_month(oy))


def period_context(p: Period) -> dict:
    today = date.today()
    this_month = Period.month(today.strftime("%Y-%m"))
    prev = date.fromordinal(this_month.start.toordinal() - 1)
    return {"p": p, "oy": p.oy, "q": p.query, "presets": [
        ("Bugun", Period(today, today)),
        ("Shu oy", this_month),
        ("O'tgan oy", Period.month(prev.strftime("%Y-%m"))),
        ("Shu yil", Period(date(today.year, 1, 1), today)),
    ]}


def make_service() -> Service:
    client = CliniccardsClient(os.environ.get("CLINICCARDS_TOKEN", ""))
    percents = Percents(Path(os.environ.get("FOIZLAR_FAYL", HERE / "foizlar.json")))
    return Service(client, percents, start_date=start_date_from_env())


app = FastAPI(title="Cliniccards maosh hisoboti")
_service: Service | None = None


def service() -> Service:
    global _service
    if _service is None:
        _service = make_service()
    return _service


attempts = sessions.Attempts()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() or (request.client.host if request.client else "?")


def auth(request: Request) -> str:
    user = sessions.check_token(request.cookies.get(sessions.COOKIE))
    if not user:
        raise sessions.NeedLogin()
    return user


@app.exception_handler(sessions.NeedLogin)
def need_login(request: Request, exc: sessions.NeedLogin):
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse("/login" + ("" if target == "/" else f"?next={quote(target)}"), status_code=303)


def login_page(request: Request, next_: str, login: str = "", error: str = "", status: int = 200):
    return templates.TemplateResponse(request, "login.html",
                                      {"next": next_, "login": login, "error": error}, status_code=status)


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if sessions.check_token(request.cookies.get(sessions.COOKIE)):
        return RedirectResponse(sessions.safe_next(next), status_code=303)
    return login_page(request, sessions.safe_next(next))


@app.post("/login")
def login_submit(request: Request, login: str = Form(""), parol: str = Form(""),
                 eslab: str = Form(""), next: str = Form("/")):
    next_ = sessions.safe_next(next)
    ip = client_ip(request)
    if not sessions.credentials()[1]:
        return login_page(request, next_, login, "Server sozlanmagan: HISOBOT_PAROL o'zgaruvchisi berilmagan.", 500)
    if attempts.blocked(ip):
        return login_page(request, next_, login, "Juda ko'p urinish. 5 daqiqadan keyin qayta urinib ko'ring.", 429)
    if not sessions.check_password(login, parol):
        attempts.fail(ip)
        return login_page(request, next_, login, "Login yoki parol noto'g'ri.", 401)
    attempts.reset(ip)
    ttl = sessions.LONG if eslab else sessions.SHORT
    resp = RedirectResponse(next_, status_code=303)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(sessions.COOKIE, sessions.make_token(sessions.credentials()[0], ttl),
                    max_age=ttl if eslab else None, httponly=True, samesite="lax", secure=secure)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(sessions.COOKIE)
    return resp


@app.exception_handler(CliniccardsError)
def api_error(request: Request, exc: CliniccardsError):
    return templates.TemplateResponse(request, "error.html", {"error": str(exc)}, status_code=502)


@app.on_event("startup")
def warm_cache():
    """Server ishga tushganda butun davr ma'lumotini fonda yuklab qo'yadi, birinchi sahifa tez ochilsin."""
    if not os.environ.get("CLINICCARDS_TOKEN"):
        return

    def run():
        try:
            service().month_report(current_month())
        except Exception as e:  # sahifa ochilganda xato baribir ko'rsatiladi
            print(f"Oldindan yuklash bajarilmadi: {e}", flush=True)

    threading.Thread(target=run, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
    s = service().summary(p)
    import staff
    staff_total = Decimal(staff.payroll_total(sklad_routes.get_db(), p.start, p.end))
    used = staff_total + s.expenses_total
    return templates.TemplateResponse(request, "index.html", {
        "s": s, "staff_total": staff_total, "used": used, "left": s.expense_budget - used, **period_context(p)})


@app.get("/shifokorlar")
def doctors(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
    rep = service().month_report(p)
    return templates.TemplateResponse(request, "doctors.html", {"rep": rep, **period_context(p)})


@app.get("/ishlar")
def done_work(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
    groups = service().done_detail(p)
    return templates.TemplateResponse(request, "detail_done.html", {
        "groups": groups, "total": sum((g.total for g in groups), Decimal(0)), **period_context(p)})


@app.get("/kassa")
def kassa(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
    groups = service().kassa_detail(p)
    return templates.TemplateResponse(request, "detail_kassa.html", {
        "groups": groups, "total": sum((g.total for g in groups), Decimal(0)),
        "currencies": len({g.extra for g in groups if g.extra}), **period_context(p)})


@app.get("/klinika")
def clinic(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
    return templates.TemplateResponse(request, "detail_clinic.html", {
        "s": service().summary(p), "groups": service().kassa_detail(p), **period_context(p)})


@app.get("/xarajatlar")
def expenses(request: Request, p: Period = Depends(get_period), _=Depends(auth)):
    groups = service().expenses_detail(p)
    return templates.TemplateResponse(request, "detail_expenses.html", {
        "groups": groups, "total": sum((g.total for g in groups), Decimal(0)), **period_context(p)})


@app.get("/shifokor/{doctor_id}")
def doctor(request: Request, doctor_id: str, p: Period = Depends(get_period), _=Depends(auth)):
    rep = service().month_report(p, with_kassa=False)
    d = rep.doctor(doctor_id)
    if d is None:
        staff = {str(s.get("doctor_id")): s for s in service().staff()}
        if doctor_id not in staff:
            raise HTTPException(404, "Shifokor topilmadi")
    return templates.TemplateResponse(request, "doctor.html", {"d": d, "doctor_id": doctor_id, **period_context(p)})


@app.get("/excel")
def excel(p: Period = Depends(get_period), shifokor: str | None = None, _=Depends(auth)):
    rep = service().month_report(p, with_kassa=shifokor is None)
    data = report_xlsx(rep, shifokor)
    name = f"maosh_{p.key}" + (f"_{shifokor}" if shifokor else "") + ".xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/qarzdorlar")
def debts(request: Request, _=Depends(auth)):
    groups = service().debts()
    return templates.TemplateResponse(request, "debts.html", {"groups": groups})


@app.get("/sozlamalar")
def settings_page(request: Request, saved: int = 0, _=Depends(auth)):
    percents, default = service().percents.load()
    staff = sorted(service().staff(), key=lambda s: (s.get("role") != "DOCTOR", s.get("lastname") or ""))
    return templates.TemplateResponse(request, "settings.html", {
        "staff": staff, "percents": percents, "default": default, "saved": saved,
        "clinic": service().percents.clinic_percent(), "doctors_share": service().percents.doctors_percent()})


@app.post("/sozlamalar")
async def settings_save(request: Request, default: float = Form(0), klinika: float = Form(20),
                        shifokorlar: float = Form(25), _=Depends(auth)):
    if not 0 <= klinika <= 100 or not 0 <= shifokorlar <= 100 or klinika + shifokorlar > 100:
        raise HTTPException(400, "Klinika va shifokorlar foizi 0–100 oralig'ida, yig'indisi 100 dan oshmasligi kerak")
    form = await request.form()
    doctors: dict[str, float] = {}
    for key, value in form.items():
        if key.startswith("foiz_") and str(value).strip() != "":
            try:
                pct = float(str(value).replace(",", "."))
            except ValueError:
                raise HTTPException(400, f"Noto'g'ri foiz: {value}")
            if not 0 <= pct <= 100:
                raise HTTPException(400, "Foiz 0 dan 100 gacha bo'lishi kerak")
            doctors[key[5:]] = pct
    service().percents.save(doctors, default, klinika, shifokorlar)
    return RedirectResponse("/sozlamalar?saved=1", status_code=303)


import sklad_routes  # noqa: E402

sklad_routes.setup(app, templates, auth, service)

import staff_routes  # noqa: E402

staff_routes.setup(app, templates, auth, get_period, period_context)


@app.post("/yangilash")
def refresh(request: Request, _=Depends(auth)):
    service().clear_cache()
    ref = urlsplit(request.headers.get("referer") or "")
    target = ref.path + (f"?{ref.query}" if ref.query else "") if ref.path.startswith("/") else "/"
    return RedirectResponse(target, status_code=303)
