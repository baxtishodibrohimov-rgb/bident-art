"""«Shifokorlar» bo'limi: maosh hisobi, maosh va avans berish, Excel."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

import salary
from db import TZ
from excel import payouts_xlsx, payslip_xlsx
from report import NO_DOCTOR, Period
from sklad_routes import get_db

router = APIRouter()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def today() -> date:
    from datetime import datetime
    return datetime.now(TZ).date()


def _amount(v: str) -> int:
    try:
        n = int(Decimal(str(v).replace(" ", "").replace(",", ".") or "0"))
    except Exception:
        raise HTTPException(400, "Summa son bo'lishi kerak")
    if n < 0:
        raise HTTPException(400, "Summa manfiy bo'lmasligi kerak")
    return n


def _file(data: bytes, name: str) -> Response:
    safe = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("_") or "maosh.xlsx"
    return Response(data, media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename=\"maosh.xlsx\"; filename*=UTF-8''{quote(safe)}"})


def setup(app, templates, auth, service, get_period, period_context):
    def doctor_name(doc: str) -> str:
        return service().doctor_names().get(str(doc), f"#{doc}")

    def earned(doc: str, p: Period) -> Decimal:
        d = service().month_report(p, with_kassa=False).doctor(doc)
        return d.salary if d else Decimal(0)

    def known(doc: str) -> None:
        if doc == NO_DOCTOR or doc not in service().doctor_names():
            raise HTTPException(404, "Shifokor topilmadi")

    @router.get("/shifokorlar")
    def doctors(request: Request, p: Period = Depends(get_period), ochiq: str = "", _=Depends(auth)):
        db = get_db()
        rep = service().month_report(p)
        start = salary.start_date(db, today())
        tracked = salary.clip(p, start)
        trep = service().month_report(tracked, with_kassa=False) if tracked else None
        rows, stats = {}, {}
        for d in rep.doctors:
            if d.doctor_id == NO_DOCTOR:
                continue
            rows[d.doctor_id] = salary.payouts(db, d.doctor_id)
            td = trep.doctor(d.doctor_id) if trep else None
            stats[d.doctor_id] = salary.status(rows[d.doctor_id], p, salary.som(td.salary if td else 0))
        in_period = {doc: [r for r in rs if p.contains(r["paid_on"])][::-1] for doc, rs in rows.items()}
        return templates.TemplateResponse(request, "doctors.html", {
            "rep": rep, "stats": stats, "pays": in_period, "ochiq": ochiq, "start": start,
            "given": sum(s.given for s in stats.values()), "avans": sum(s.avans for s in stats.values()),
            "not_given": sum(s.left for s in stats.values()), "openings": salary.openings(db),
            "staff": sorted(service().doctor_names().items(), key=lambda x: x[1].lower()),
            "section": "shifokorlar", **period_context(p)})

    @router.get("/shifokor/{doctor_id}")
    def doctor_page(request: Request, doctor_id: str, _=Depends(auth)):
        """Alohida sahifa yo'q: shifokorlar jadvalida o'sha shifokor qatori ochiladi."""
        q = "&".join(f"{k}={quote(v)}" for k, v in request.query_params.items() if k in ("oy", "dan", "gacha"))
        return RedirectResponse(f"/shifokorlar?{q + '&' if q else ''}ochiq={quote(doctor_id)}#d-{quote(doctor_id)}",
                                status_code=303)

    @router.get("/maosh/{doctor_id}/hisob")
    def pay_plan(doctor_id: str, _=Depends(auth)):
        known(doctor_id)
        pl = salary.plan(get_db(), doctor_id, today(), earned)
        rows = salary.payouts(get_db(), doctor_id)
        return {**pl.as_dict(), "name": doctor_name(doctor_id),
                "history": [{"id": r["id"], "kind": r["kind"], "from": r["period_from"], "to": r["period_to"],
                             "amount": r["amount"], "avans_used": r["avans_used"], "paid_on": r["paid_on"]}
                            for r in reversed(rows)]}

    @router.post("/maosh/{doctor_id}/berish")
    def pay(doctor_id: str, summa: str = Form(""), _=Depends(auth)):
        known(doctor_id)
        db, t = get_db(), today()
        pl = salary.plan(db, doctor_id, t, earned)
        try:
            salary.pay_salary(db, pl, doctor_name(doctor_id), _amount(summa), t)
        except salary.TooMuch as e:
            return JSONResponse({"error": "ortiq", "can": e.can}, status_code=409)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"ok": True}

    @router.post("/maosh/{doctor_id}/avans")
    def pay_avans(doctor_id: str, avans: str = Form(""), _=Depends(auth)):
        known(doctor_id)
        db, t = get_db(), today()
        pl = salary.plan(db, doctor_id, t, earned)
        try:
            salary.pay_with_avans(db, pl, doctor_name(doctor_id), _amount(avans), t)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"ok": True}

    @router.post("/maosh/ochirish/{payout_id}")
    def remove(payout_id: int, _=Depends(auth)):
        salary.delete(get_db(), payout_id)
        return {"ok": True}

    @router.post("/maosh/boshlanish")
    async def set_start(request: Request, _=Depends(auth)):
        form = await request.form()
        try:
            start = date.fromisoformat(str(form.get("boshlanish") or ""))
        except ValueError:
            raise HTTPException(400, "Boshlanish sanasi YYYY-MM-DD ko'rinishida bo'lishi kerak")
        openings = {k[len("qoldiq_"):]: _amount(v) for k, v in form.items() if k.startswith("qoldiq_")}
        salary.set_start(get_db(), start, openings)
        back = str(form.get("qaytish") or "/shifokorlar")
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/shifokorlar",
                                status_code=303)

    @router.get("/maosh/{payout_id}/excel")
    def payslip(payout_id: int, _=Depends(auth)):
        r = get_db().one("SELECT * FROM doctor_payouts WHERE id=?", (payout_id,))
        if not r:
            raise HTTPException(404, "To'lov topilmadi")
        name = doctor_name(r["doctor_id"])
        lines = [] if r["kind"] == "avans" else service().doctor_lines(
            r["doctor_id"], Period(date.fromisoformat(r["period_from"]), date.fromisoformat(r["period_to"])))
        import finance
        kind = "avans" if r["kind"] == "avans" else "maosh"
        return _file(payslip_xlsx(name, r, lines, finance.DOCTOR_PCT), f"{name}_{kind}_{r['paid_on']}.xlsx")

    @router.get("/shifokor/{doctor_id}/maosh.xlsx")
    def payouts_file(doctor_id: str, p: Period = Depends(get_period), _=Depends(auth)):
        name = doctor_name(doctor_id)
        rows = [r for r in salary.payouts(get_db(), doctor_id) if p.contains(r["paid_on"])]
        return _file(payouts_xlsx(name, p.label, rows), f"{name}_maosh_{p.key}.xlsx")

    app.include_router(router)
