"""Bosh ekrandagi har bir summa nimadan tashkil topganini ko'rsatadigan ro'yxatlar."""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from decimal import Decimal

from finance import AUDIT_LABELS, member_info, spending_name
from report import NO_DOCTOR, dedupe_items, money, staff_name

APOSTROPHES = str.maketrans("‘’ʻʼ", "''''")


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
    def balance(self) -> Decimal:
        return self.paid_total - self.total

    @property
    def left_total(self) -> Decimal:
        return sum((r.get("left", Decimal(0)) for r in self.rows), Decimal(0))


def text(v) -> str:
    """Cliniccards ba'zi matnlarni HTML-kodlangan holda yuboradi («Xo&#39;jalik»): oddiy matnga qaytaramiz."""
    return html.unescape(str(v or "")).strip()


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


def pay_key(patient_id, when, amount) -> tuple:
    """To'lovni ish bilan solishtirish kaliti (payment_id bo'lmasa): bemor, vaqt (daqiqagacha), summa."""
    return (str(patient_id or ""), str(when or "")[:16], str(money(amount).normalize()))


def kassa(payments: list[dict], patients: dict[str, str], doctors_by_payment: dict | None = None,
          doctor_names: dict[str, str] | None = None) -> list[Group]:
    """Kassaga tushgan to'lovlar: kassa bo'yicha, har bir to'lov bilan (qaysi shifokor ishi uchun ham)."""
    groups: dict[str, Group] = {}
    doctors_by_payment, doctor_names = doctors_by_payment or {}, doctor_names or {}
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
        doc = (doctors_by_payment.get(str(p.get("payment_id"))) if p.get("payment_id") not in (None, "") else None) \
            or doctors_by_payment.get(pay_key(pid, p.get("date"), p.get("amount")))
        row = groups[key].rows[-1]
        row["doctor_id"] = doc or ""
        row["doctor"] = ("belgilanmagan" if doc == NO_DOCTOR else doctor_names.get(doc, f"#{doc}")) if doc else ""
    for g in groups.values():
        g.rows.sort(key=lambda r: r["date"])
    return _sorted(groups)


def expense_row(sp: dict) -> dict:
    _, member = member_info(sp)
    amount, paid = money(sp.get("amount")), money(sp.get("paid"))
    # Cliniccards kassadan chiqqan to'lovni minusda qaytaradi.
    payments = [{"cash_desk": text(payment.get("cash_desk_name")), "amount": -money(payment.get("amount"))}
                for payment in sp.get("payments") or [] if isinstance(payment, dict)]
    return {
        "id": str(sp.get("id") or ""),
        "date": str(sp.get("date_issued") or "")[:10],
        "cat": spending_name(sp.get("spendings_category")) or "Toifasiz",
        "sub": spending_name(sp.get("spendings_subcategory")),
        "supplier": spending_name(sp.get("supplier")) or text(member),
        "note": text(sp.get("note")), "status": sp.get("payment_status") or "",
        "amount": amount, "paid": paid, "balance": paid - amount, "payments": payments,
    }


@dataclass
class AuditSection:
    name: str
    recipients: list[Group]
    key: str = ""

    @property
    def count(self) -> int:
        return sum(len(group.rows) for group in self.recipients)

    @property
    def paid_total(self) -> Decimal:
        return sum((group.paid_total for group in self.recipients), Decimal(0))


def _name_key(value: str) -> str:
    return " ".join(value.translate(APOSTROPHES).casefold().split())


def expenses_by_recipient(spendings: list[dict]) -> list[Group]:
    recipients: dict[str, Group] = {}
    for sp in spendings:
        row = expense_row(sp)
        name = " ".join(row["supplier"].split())
        key = _name_key(name)
        if key not in recipients:
            recipients[key] = Group(key, name or "Oluvchi ko'rsatilmagan")
        recipients[key].rows.append(row)
    for group in recipients.values():
        group.rows.sort(key=lambda row: row["date"], reverse=True)
    return sorted(recipients.values(), key=lambda group: (not group.key, group.key))


def audit_sections(groups: dict[str, list[dict]]) -> tuple[dict[str, AuditSection], list[AuditSection]]:
    main = {kind: AuditSection(label, expenses_by_recipient(groups[kind]), kind) for kind, label in AUDIT_LABELS.items()}
    by_category: dict[tuple[str, str], tuple[str, list[dict]]] = {}
    for sp in groups["material"] + groups["salary"]:
        category = sp.get("spendings_category") or {}
        subcategory = sp.get("spendings_subcategory") or {}
        title = text(category.get("name")) or "Toifasiz"
        subtitle = text(subcategory.get("name"))
        key = (str(category.get("id") or _name_key(title)), str(subcategory.get("id") or _name_key(subtitle)))
        name = f"{title} → {subtitle}" if subtitle else title
        by_category.setdefault(key, (name, []))[1].append(sp)
    other = [AuditSection(name, expenses_by_recipient(spendings))
             for name, spendings in sorted(by_category.values(), key=lambda entry: _name_key(entry[0]))]
    return main, other


def expenses(spendings: list[dict]) -> list[Group]:
    """Xarajatlar: toifa bo'yicha, har bir xarajat bilan (manfiy summa — qaytarilgan)."""
    groups: dict[str, Group] = {}
    for s in spendings:
        cat = s.get("spendings_category") or {}
        key = str(cat.get("id") or "0")
        if key not in groups:
            groups[key] = Group(key, text(cat.get("name")) or "Toifasiz")
        groups[key].rows.append(expense_row(s))
    for g in groups.values():
        g.rows.sort(key=lambda r: r["date"])
    return _sorted(groups)
