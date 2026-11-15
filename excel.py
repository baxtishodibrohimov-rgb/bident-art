"""Hisobotni Excel (.xlsx) faylga chiqarish."""
from __future__ import annotations

import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from decimal import ROUND_HALF_UP, Decimal

from report import DoctorReport, MonthReport

HEAD = Font(bold=True, color="FFFFFF")
FILL = PatternFill("solid", fgColor="2F5D8A")
MONEY = '#,##0.00'


def _header(ws, cols):
    ws.append(cols)
    for c in ws[ws.max_row]:
        c.font, c.fill, c.alignment = HEAD, FILL, Alignment(horizontal="center", wrap_text=True)


def _widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _doctor_sheet(wb, d: DoctorReport, oy: str):
    title = re.sub(r"[\[\]:*?/\\]", "-", d.name)[:28] or d.doctor_id
    ws = wb.create_sheet(title)
    ws.append([f"{d.name} — {oy}: shu davrda tushgan pul bo'yicha hisobot"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    _header(ws, ["Bemor", "Ish", "Ish sanasi", "Narx", "Jami to'langan",
                 "Tushgan", "To'lov sanalari", f"Ulush ({d.percent}%)"])
    for r in d.rows:
        ws.append([r.patient, r.work, r.done_date, float(r.price), float(r.paid_total),
                   float(r.month_paid), ", ".join(r.payment_dates), float(r.share)])
    ws.append(["JAMI", "", "", "", "", float(d.total), "", float(d.salary)])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=4, min_col=4, max_col=8):
        for c in row:
            if isinstance(c.value, float):
                c.number_format = MONEY
    _widths(ws, [28, 34, 12, 14, 14, 14, 22, 14])
    ws.freeze_panes = "A4"


def report_xlsx(rep: MonthReport, doctor_id: str | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Jami"
    if doctor_id is None:
        ws.append([f"Barcha shifokorlar — {rep.oy}"])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append([])
        _header(ws, ["Shifokor", "Bemorlar", "Tushgan", "Foiz", "Maosh"])
        for d in rep.doctors:
            ws.append([d.name, d.patients, float(d.total), float(d.percent), float(d.salary)])
        ws.append(["JAMI", "", float(rep.total), "", float(rep.salary)])
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
        if rep.kassa_total is not None:
            ws.append([])
            ws.append(["Kassa bo'yicha jami (GET /payments)", "", float(rep.kassa_total)])
            ws.append(["Farq (ishga bog'lanmagan to'lovlar)", "", float(rep.difference)])
        for row in ws.iter_rows(min_row=4, min_col=3, max_col=5):
            for c in row:
                if isinstance(c.value, float) and c.column != 4:
                    c.number_format = MONEY
        _widths(ws, [34, 10, 16, 8, 16])
        doctors = rep.doctors
    else:
        wb.remove(ws)
        doctors = [d for d in rep.doctors if d.doctor_id == str(doctor_id)]
    for d in doctors:
        _doctor_sheet(wb, d, rep.oy)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _dmy(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if iso and len(iso) >= 10 else iso or ""


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def payslip_xlsx(name: str, payout, lines: list[dict], percent) -> bytes:
    """Bitta berilgan maosh uchun: Sana | F.I.SH. | Summa | Shifokor, oxirida shifokor olgan summa.

    «Summa» — kassaga haqiqatan tushgan pul (ish narxi emas)."""
    wb = Workbook()
    ws = wb.active
    if payout["kind"] == "avans":
        ws.title = "Avans"
        ws.append([f"Shifokor {name} — avans"])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append([])
        for label, value in (("Berilgan sana", _dmy(payout["paid_on"])), ("Summa", payout["amount"])):
            ws.append([label, value])
            ws.cell(ws.max_row, 1).font = Font(bold=True)
        ws.cell(4, 2).number_format = '#,##0'
        _widths(ws, [20, 18])
        return _save(wb)
    ws.title = "Maosh"
    s, e = payout["period_from"], payout["period_to"]
    per = f"{_dmy(s)[:5]}–{_dmy(e)[:5]} {e[:4]}-yil" if s[:4] == e[:4] else f"{_dmy(s)}–{_dmy(e)}"
    ws.append([f"Shifokor {name} {per} uchun oylik maoshi"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4)
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    _header(ws, ["Sana", "F.I.SH.", "Summa", "Shifokor"])
    pct = Decimal(str(percent))
    total = share_total = Decimal(0)
    for x in lines:
        share = (x["amount"] * pct / 100).quantize(Decimal(1), ROUND_HALF_UP)
        ws.append([_dmy(x["date"]), x["patient"], float(x["amount"]), float(share)])
        total += x["amount"]
        share_total += share
    ws.append(["Jami", "", float(total), float(share_total)])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    ws.append([])
    if payout["avans_used"]:
        ws.append(["Oldingi avansdan ushlandi", "", "", payout["avans_used"]])
    ws.append(["Shifokor oldi", "", "", payout["amount"]])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=4, min_col=3, max_col=4):
        for c in row:
            if isinstance(c.value, (int, float)):
                c.number_format = '#,##0'
    _widths(ws, [14, 32, 16, 16])
    return _save(wb)


def payouts_xlsx(name: str, period_label: str, rows) -> bytes:
    """Tanlangan davrda shifokorga berilgan barcha maosh va avanslar."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Berilgan maosh"
    ws.append([f"Shifokor {name} — {period_label}: berilgan maosh"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4)
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    _header(ws, ["Qachon berildi", "Qaysi davr uchun", "Maosh", "Avans"])
    sm = sa = 0
    for r in rows:
        if r["kind"] == "avans":
            ws.append([_dmy(r["paid_on"]), "", None, r["amount"]])
            sa += r["amount"]
        else:
            ws.append([_dmy(r["paid_on"]), f"{_dmy(r['period_from'])} – {_dmy(r['period_to'])}", r["amount"], None])
            sm += r["amount"]
    ws.append(["Jami", "", sm, sa])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=4, min_col=3, max_col=4):
        for c in row:
            if isinstance(c.value, (int, float)):
                c.number_format = '#,##0'
    _widths(ws, [16, 26, 16, 16])
    return _save(wb)
