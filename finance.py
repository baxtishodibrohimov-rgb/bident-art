"""Klinika moliyasi: o'zgarmas ulushlar, xarajatlar tarkibi, sof foyda, shifokorlarga berilgan maosh.

Qoidalar (o'zgarmas):
- Klinika ulushi — kassaga tushgan pulning 20%.
- Har bir shifokorning ulushi — unga shu davrda tushgan pulning 25%.

Xarajatlar = klinika ulushi + shifokorlar maoshi + xodimlar maoshi + material rasxod.
Sof foyda = shu davrda bajarilgan ishlar − xarajatlar.

Cliniccards xarajatlari ikkiga bo'linadi:
- maosh xarajatlari (xodimga bog'langan yoki «зарплата/maosh» toifasidagi) — shifokorlarga
  «berilgan» summa shulardan olinadi va material rasxodga qo'shilmaydi (ikki marta hisoblanmasin);
- qolganlari — «material rasxod» (material, ijara, kommunal va boshqa sarflar).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from report import money

CLINIC_PCT = Decimal(20)
DOCTOR_PCT = Decimal(25)

SALARY_WORDS = ("зарплат", "заработ", "maosh", "oylik", "salary", "payroll")


def _q(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), ROUND_HALF_UP)


def member_info(sp: dict) -> tuple[str, str]:
    """Xarajat qaysi xodimga bog'langan: (id, ism). Bog'lanmagan bo'lsa ('', '')."""
    m = sp.get("member") or sp.get("clinic_member") or sp.get("spendings_clinicmember")
    mid = str(sp.get("clinic_member_id") or sp.get("member_id") or sp.get("spendings_clinicmember_id") or "")
    if isinstance(m, dict):
        mid = str(m.get("id") or m.get("doctor_id") or m.get("member_id") or mid or "")
        name = " ".join(x for x in (m.get("lastname"), m.get("firstname")) if x) or m.get("name") or ""
        return mid, name.strip()
    if isinstance(m, str):
        return mid, m.strip()
    return mid, ""


def is_salary(sp: dict) -> bool:
    mid, name = member_info(sp)
    if mid or name:
        return True
    text = " ".join(str((sp.get(k) or {}).get("name") or "") for k in ("spendings_category", "spendings_subcategory")).lower()
    return any(w in text for w in SALARY_WORDS)


def split_spendings(spendings: list[dict]) -> tuple[list[dict], list[dict]]:
    salary, material = [], []
    for sp in spendings:
        (salary if is_salary(sp) else material).append(sp)
    return salary, material


def _norm(name: str) -> str:
    return " ".join(sorted(name.lower().replace("'", "").replace("ʻ", "").split()))


def payouts_by_doctor(salary_spendings: list[dict], staff: list[dict]) -> dict[str, Decimal]:
    """Shifokorlarga berilgan maosh: {doctor_id: summa}. Xodimni ID yoki ism bo'yicha topadi."""
    by_id = {str(s.get("doctor_id")): str(s.get("doctor_id")) for s in staff}
    by_name = {_norm(" ".join(x for x in (s.get("lastname"), s.get("firstname")) if x)): str(s.get("doctor_id")) for s in staff}
    out: dict[str, Decimal] = {}
    for sp in salary_spendings:
        mid, name = member_info(sp)
        doc = by_id.get(mid) or by_name.get(_norm(name)) if (mid or name) else None
        if doc:
            out[doc] = out.get(doc, Decimal(0)) + money(sp.get("paid") if sp.get("paid") not in (None, "") else sp.get("amount"))
    return out


@dataclass
class Overview:
    done_total: Decimal
    done_count: int
    kassa_total: Decimal
    kassa_count: int
    doctors_salary: Decimal
    staff_salary: Decimal
    material_total: Decimal
    material_count: int

    @property
    def clinic_share(self) -> Decimal:
        return _q(self.kassa_total * CLINIC_PCT / 100)

    @property
    def expenses_total(self) -> Decimal:
        return self.clinic_share + self.doctors_salary + self.staff_salary + self.material_total

    @property
    def profit(self) -> Decimal:
        return self.done_total - self.expenses_total

    def parts(self) -> list[tuple[str, str, Decimal]]:
        """Xarajat qismlari: (havola, nomi, summa)."""
        return [
            ("/klinika", f"Klinika ulushi (kassaning {CLINIC_PCT:.0f}%)", self.clinic_share),
            ("/shifokorlar", "Shifokorlar maoshi", self.doctors_salary),
            ("/xodimlar", "Xodimlar maoshi", Decimal(self.staff_salary)),
            ("/xarajatlar#material", "Material rasxod", self.material_total),
        ]
