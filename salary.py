"""Shifokorlarga maosh va avans berish.

Qoidalar:
- Maosh davri o'zi aniqlanadi: oxirgi maosh berilgan kunning ertasidan bugungacha.
  Hali maosh berilmagan bo'lsa — hisob boshlanish sanasidan (`maosh_boshi`).
- Shu davrda ishlagan puli = shu davrda kassaga tushgan pulning shifokor ulushi.
- Oldin berilgan avans keyingi maoshdan avtomatik ushlanadi.
- Ishlaganidan ko'p berib bo'lmaydi: ortig'i alohida avans qilib yoziladi.
- Hisob boshlanishidan oldingi davr eski usulda berilgan (yopilgan) deb hisoblanadi;
  o'sha sanagacha berilmay qolgan pul «boshlang'ich qoldiq» qilib bir marta yoziladi.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

from db import DB, now
from report import Period


def som(v) -> int:
    """Pulni butun so'mga yaxlitlaydi."""
    return int(Decimal(str(v or 0)).quantize(Decimal(1), ROUND_HALF_UP))


def start_date(db: DB, today: date) -> date:
    """Maosh hisobi qachondan boshlanadi. Birinchi marta joriy oyning 1-sanasi qilib saqlanadi."""
    raw = db.meta("maosh_boshi")
    if not raw:
        raw = today.replace(day=1).isoformat()
        db.set_meta("maosh_boshi", raw)
    return date.fromisoformat(raw)


def set_start(db: DB, start: date, openings: dict[str, int]) -> None:
    with db.tx():
        db.conn.execute("INSERT INTO meta(key, value) VALUES('maosh_boshi', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (start.isoformat(),))
        db.conn.execute("DELETE FROM doctor_opening")
        for doc, amount in openings.items():
            if amount > 0:
                db.conn.execute("INSERT INTO doctor_opening(doctor_id, amount) VALUES(?, ?)", (str(doc), amount))


def openings(db: DB) -> dict[str, int]:
    return {r["doctor_id"]: r["amount"] for r in db.q("SELECT * FROM doctor_opening")}


def payouts(db: DB, doctor_id: str | None = None) -> list:
    if doctor_id is None:
        return db.q("SELECT * FROM doctor_payouts ORDER BY paid_on, id")
    return db.q("SELECT * FROM doctor_payouts WHERE doctor_id=? ORDER BY paid_on, id", (str(doctor_id),))


def avans_left(rows) -> int:
    """Berilgan, lekin hali maoshdan ushlanmagan avans."""
    given = sum(r["amount"] for r in rows if r["kind"] == "avans")
    used = sum(r["avans_used"] for r in rows if r["kind"] == "maosh")
    return max(0, given - used)


def next_from(rows, start: date) -> date:
    last = max((r["period_to"] for r in rows if r["kind"] == "maosh"), default=None)
    if not last or date.fromisoformat(last) < start:
        return start
    return date.fromisoformat(last) + timedelta(days=1)


@dataclass
class Plan:
    """«Maosh berish» oynasidagi hisob."""
    doctor_id: str
    start: date
    end: date
    earned: int       # shu davrda ishlagan puli (boshlang'ich qoldiq bilan)
    opening: int      # boshlang'ich qoldiq (faqat birinchi maoshda qo'shiladi)
    avans: int        # shu maoshdan ushlanadigan oldingi avans
    avans_left: int   # shifokorda hozir turgan avans
    paid_up: bool     # bugungacha maosh berib bo'lingan

    @property
    def can(self) -> int:
        """Qo'lga berish mumkin bo'lgan summa."""
        return max(0, self.earned - self.avans)

    @property
    def net(self) -> int:
        """Ishlagan puli − olgan avansi (manfiy bo'lsa shifokor oldindan olgan)."""
        return self.earned - self.avans_left

    def as_dict(self) -> dict:
        return {"doctor_id": self.doctor_id, "from": self.start.isoformat(), "to": self.end.isoformat(),
                "earned": self.earned, "opening": self.opening, "avans": self.avans,
                "avans_left": self.avans_left, "can": self.can, "net": self.net, "paid_up": self.paid_up}


def plan(db: DB, doctor_id: str, today: date, earned_fn: Callable[[str, Period], Decimal]) -> Plan:
    rows = payouts(db, doctor_id)
    start = start_date(db, today)
    f = next_from(rows, start)
    left = avans_left(rows)
    if f > today:
        return Plan(str(doctor_id), f, f, 0, 0, 0, left, True)
    opening = 0 if any(r["kind"] == "maosh" for r in rows) else openings(db).get(str(doctor_id), 0)
    earned = som(earned_fn(str(doctor_id), Period(f, today))) + opening
    return Plan(str(doctor_id), f, today, earned, opening, min(left, earned), left, False)


class TooMuch(ValueError):
    def __init__(self, can: int):
        super().__init__(f"Ishlaganidan ko'p: berish mumkin {can}")
        self.can = can


def _add(db: DB, doctor_id: str, name: str, kind: str, amount: int, paid_on: date,
         period: tuple[date, date] | None = None, avans_used: int = 0) -> int:
    return db.run("INSERT INTO doctor_payouts(doctor_id, doctor_name, kind, period_from, period_to, amount, avans_used, "
                  "paid_on, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (str(doctor_id), name, kind, period[0].isoformat() if period else None,
                   period[1].isoformat() if period else None, amount, avans_used, paid_on.isoformat(), now()))


def pay_salary(db: DB, p: Plan, name: str, amount: int, today: date) -> int:
    """Maosh beradi. To'liq bermasa ham davr yopiladi (qolgani «Berilmagan»da ko'rinadi)."""
    if p.paid_up:
        raise ValueError("Bugungacha maosh berib bo'lingan")
    if amount <= 0:
        raise ValueError("Summani kiriting")
    if amount > p.can:
        raise TooMuch(p.can)
    return _add(db, p.doctor_id, name, "maosh", amount, today, (p.start, p.end), p.avans)


def pay_with_avans(db: DB, p: Plan, name: str, avans: int, today: date) -> None:
    """Ishlagan puli maosh sifatida + ustiga avans. Avans keyingi maoshdan ushlanadi."""
    if avans <= 0:
        raise ValueError("Avans summasini kiriting")
    with db.tx():
        if not p.paid_up and (p.can > 0 or p.avans > 0):
            db.conn.execute(
                "INSERT INTO doctor_payouts(doctor_id, doctor_name, kind, period_from, period_to, amount, avans_used, "
                "paid_on, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (p.doctor_id, name, "maosh", p.start.isoformat(), p.end.isoformat(), p.can, p.avans, today.isoformat(), now()))
        db.conn.execute(
            "INSERT INTO doctor_payouts(doctor_id, doctor_name, kind, amount, paid_on, created_at) VALUES(?,?,?,?,?,?)",
            (p.doctor_id, name, "avans", avans, today.isoformat(), now()))


def delete(db: DB, payout_id: int) -> None:
    db.run("DELETE FROM doctor_payouts WHERE id=?", (payout_id,))


# --- Shifokorlar jadvali uchun ----------------------------------------------------
@dataclass
class Status:
    given: int        # shu davrda qo'lga berilgan: maosh (o'z davri bo'yicha) + avans (berilgan sanasi bo'yicha)
    avans: int        # shundan avans
    avans_left: int   # hozir ushlanmagan avans
    left: int         # berilmagan; manfiy — shifokor oldindan olgan


def status(rows, period: Period, tracked_salary: int) -> Status:
    """`tracked_salary` — shu davrning hisob boshlanishidan keyingi qismidagi maosh."""
    s, e = period.start.isoformat(), period.end.isoformat()
    sal = [r for r in rows if r["kind"] == "maosh" and r["period_from"] <= e and r["period_to"] >= s]
    av = sum(r["amount"] for r in rows if r["kind"] == "avans" and s <= r["paid_on"] <= e)
    cash = sum(r["amount"] for r in sal)
    covered = cash + sum(r["avans_used"] for r in sal)
    left_av = avans_left(rows)
    return Status(cash + av, av, left_av, tracked_salary - covered - min(left_av, av))


def clip(period: Period, start: date) -> Period | None:
    """Davrning hisob boshlanishidan keyingi qismi."""
    s = max(period.start, start)
    return Period(s, period.end) if s <= period.end else None
