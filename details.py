"""Bosh ekrandagi har bir summa nimadan tashkil topganini ko'rsatadigan ro'yxatlar."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from report import NO_DOCTOR, dedupe_items, money, staff_name


@dataclass
class Group:
    key: str
    name: str
    rows: list[dict] = field(default_factory=list)
    extra: str = ""

    @property
    def total(self) -> Decimal:
        return sum((r["amount"] for r in self.rows), Decimal(0))

    @property
    def paid_total(self) -> Decimal:
        return sum((r.get("paid", Decimal(0)) for r in self.rows), Decimal(0))

    @property
    def left_total(self) -> Decimal:
        return sum((r.get("left", Decimal(0)) for r in self.rows), Decimal(0))


def _sorted(groups: dict[str, Group]) -> list[Group]:
    return sorted(groups.values(), key=lambda g: -g.total)


def done_work(items: list[dict], staff: list[dict], patients: dict[str, str]) -> list[Group]:
    """Bajarilgan ishlar: shifokor bo'yicha, har bir ish narxi bilan."""
    staff_by_id = {str(s.get("doctor_id")): s for s in staff}
    groups: dict[str, Group] = {}
    for it in dedupe_items(items):
        doc = str(it.get("done_by_doctor_id") or NO_DOCTOR)
        if doc not in groups:
            name = ("Shifokor belgilanmagan" if doc == NO_DOCTOR else
                    staff_name(staff_by_id[doc]) if doc in staff_by_id else it.get("done_by") or f"#{doc}")
            groups[doc] = Group(doc, name)
        pid = str(it.get("patient_id") or "")
        price, paid = money(it.get("price_with_discount")), money(it.get("paid"))
        groups[doc].rows.append({
            "date": str(it.get("marked_done_date") or "")[:10], "patient": patients.get(pid, f"#{pid}"),
            "work": it.get("name") or "", "amount": price, "paid": paid, "left": max(price - paid, Decimal(0)),
        })
    for g in groups.values():
        g.rows.sort(key=lambda r: (r["date"], r["patient"]))
    return _sorted(groups)


def kassa(payments: list[dict], patients: dict[str, str]) -> list[Group]:
    """Kassaga tushgan to'lovlar: kassa bo'yicha, har bir to'lov bilan."""
    groups: dict[str, Group] = {}
    for p in payments:
        desk = p.get("cash_desk") or {}
        key = str(p.get("cash_desk_id") or desk.get("id") or "?")
        if key not in groups:
            groups[key] = Group(key, desk.get("name") or f"Kassa #{key}", extra=desk.get("currency") or "")
        pid = str(p.get("patient_id") or "")
        groups[key].rows.append({
            "date": str(p.get("date") or "")[:16], "patient": patients.get(pid, f"#{pid}") if pid else "—",
            "type": p.get("type") or "", "who": p.get("created_by") or "", "note": p.get("note") or "",
            "amount": money(p.get("amount")),
        })
    for g in groups.values():
        g.rows.sort(key=lambda r: r["date"])
    return _sorted(groups)


def expenses(spendings: list[dict]) -> list[Group]:
    """Xarajatlar: toifa bo'yicha, har bir xarajat bilan (manfiy summa — qaytarilgan)."""
    groups: dict[str, Group] = {}
    for s in spendings:
        cat = s.get("spendings_category") or {}
        key = str(cat.get("id") or "0")
        if key not in groups:
            groups[key] = Group(key, cat.get("name") or "Toifasiz")
        sub = (s.get("spendings_subcategory") or {}).get("name") or ""
        member = s.get("member")
        if isinstance(member, dict):
            member = " ".join(x for x in (member.get("lastname"), member.get("firstname"), member.get("name")) if x)
        groups[key].rows.append({
            "date": str(s.get("date_issued") or "")[:10], "sub": sub,
            "supplier": (s.get("supplier") or {}).get("name") or (member or ""),
            "note": s.get("note") or "", "status": s.get("payment_status") or "",
            "amount": money(s.get("amount")), "paid": money(s.get("paid")),
        })
    for g in groups.values():
        g.rows.sort(key=lambda r: r["date"])
    return _sorted(groups)
