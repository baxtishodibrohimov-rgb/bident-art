"""Shifokor bo'lmagan xodimlar: kunlik summa, noqulay kun qo'shimchasi, davomat va maosh hisobi.

Qoida: ishlagan har bir kun uchun kunlik summa. Qo'shimcha summa (kuniga bir marta) quyidagi
holatda qo'shiladi: kun noqulay (klinikaning dam olish kuni — DAM_OLISH_KUNLARI) yoki xodim
o'sha kuni normadan ko'p ishlagan deb belgilangan.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from db import DB, now

WEEKDAYS = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]


def rest_days() -> set[int]:
    """Dam olish kunlari: 0=dushanba ... 6=yakshanba. Standart: yakshanba."""
    raw = os.environ.get("DAM_OLISH_KUNLARI", "6")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit() and int(x) < 7}


def is_rest_day(d: date) -> bool:
    return d.weekday() in rest_days()


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1)]


@dataclass
class WorkerPay:
    worker: dict
    worked: list[str] = field(default_factory=list)     # ishlagan kunlar
    extra_days: list[str] = field(default_factory=list)  # qo'shimcha to'langan kunlar

    @property
    def base(self) -> int:
        return len(self.worked) * self.worker["daily_rate"]

    @property
    def extra(self) -> int:
        return len(self.extra_days) * self.worker["extra_rate"]

    @property
    def total(self) -> int:
        return self.base + self.extra


def gets_extra(day: str, extra_flag: int) -> bool:
    return bool(extra_flag) or is_rest_day(date.fromisoformat(day))


def payroll(db: DB, start: date, end: date, include_inactive: bool = True) -> list[WorkerPay]:
    rows = db.q("SELECT * FROM workers" + ("" if include_inactive else " WHERE active=1") + " ORDER BY full_name")
    out = {w["id"]: WorkerPay(dict(w)) for w in rows}
    for a in db.q("SELECT * FROM attendance WHERE day BETWEEN ? AND ? ORDER BY day", (start.isoformat(), end.isoformat())):
        wp = out.get(a["worker_id"])
        if not wp:
            continue
        wp.worked.append(a["day"])
        if gets_extra(a["day"], a["extra"]):
            wp.extra_days.append(a["day"])
    # faol bo'lmagan va shu davrda ishlamagan xodimlarni ko'rsatmaymiz
    return [wp for wp in out.values() if wp.worker["active"] or wp.worked]


def payroll_total(db: DB, start: date, end: date) -> int:
    return sum(wp.total for wp in payroll(db, start, end))


def grid(db: DB, start: date, end: date) -> dict:
    """Grafik uchun: {(day, worker_id): extra_flag} va klinika kunlari."""
    marks = {(a["day"], a["worker_id"]): a["extra"]
             for a in db.q("SELECT * FROM attendance WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))}
    clinic = {c["day"]: c["worked"]
              for c in db.q("SELECT * FROM clinic_days WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))}
    return {"marks": marks, "clinic": clinic}


def set_mark(db: DB, day: str, worker_id: int, state: str, source: str = "web") -> None:
    """state: 'yoq' (kelmadi) | 'keldi' | 'qoshimcha' (keldi + normadan ko'p)."""
    with db.tx():
        db.conn.execute("DELETE FROM attendance WHERE day=? AND worker_id=?", (day, worker_id))
        if state in ("keldi", "qoshimcha"):
            db.conn.execute("INSERT INTO attendance(day, worker_id, extra, source) VALUES(?,?,?,?)",
                            (day, worker_id, int(state == "qoshimcha"), source))
            db.conn.execute("INSERT INTO clinic_days(day, worked, answered_at) VALUES(?,1,?) "
                            "ON CONFLICT(day) DO UPDATE SET worked=1", (day, now()))


def save_day(db: DB, day: str, worked: bool, present: list[int], extra: list[int], reporter: int | None) -> None:
    with db.tx():
        db.conn.execute("INSERT INTO clinic_days(day, worked, reported_by, answered_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(day) DO UPDATE SET worked=excluded.worked, reported_by=excluded.reported_by, "
                        "answered_at=excluded.answered_at", (day, int(worked), reporter, now()))
        db.conn.execute("DELETE FROM attendance WHERE day=? AND source='bot'", (day,))
        if worked:
            for wid in present:
                db.conn.execute("INSERT OR REPLACE INTO attendance(day, worker_id, extra, source) VALUES(?,?,?,'bot')",
                                (day, wid, int(wid in extra)))
