"""Cliniccards kassa operatsiyalaridan material to'lovlari hisoboti."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from finance import member_info, spending_kind, spending_name
from report import Period, money

MISSING_CATEGORY = "Xarajat toifasi aniqlanmadi"
PaymentKey = tuple[str, str, Decimal]


@dataclass
class Payment:
    date: str
    recipient: str
    amount: Decimal
    cash_desk: str
    note: str
    currency: str
    invoice_id: str = ""
    invoice_date: str = ""
    reason: str = ""


@dataclass
class PaymentReport:
    rows: list[Payment] = field(default_factory=list)
    review: list[Payment] = field(default_factory=list)
    error: str = ""

    @property
    def total(self) -> Decimal:
        return sum((row.amount for row in self.rows), Decimal(0))

    @property
    def verified_total(self) -> Decimal | None:
        return None if self.error or self.review else self.total


def _day(value) -> str:
    raw = str(value or "").strip()[:10]
    for pattern in ("%Y-%m-%d", "%m.%d.%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    return ""


def _key(row: dict, supplier, amount: Decimal) -> PaymentKey | None:
    name = spending_name(supplier).casefold()
    desk = str(row.get("cash_desk_id") or "")
    return (name, desk, amount) if name and desk else None


def _invoice_id(operation: dict) -> str:
    return str(operation.get("spending_id") or operation.get("spendings_id") or operation.get("expense_id") or "")


def _operation_kind(operation: dict) -> str | None:
    category = spending_name(operation.get("spendings_category"))
    subcategory = spending_name(operation.get("spendings_subcategory"))
    if category.casefold() == "ishxona" and not subcategory:
        return None
    if not category and subcategory.casefold() == "ishxonaga":
        return "clinic"
    if category or subcategory or any(member_info(operation)):
        return spending_kind(operation)
    return None


def _matches_category(operation: dict, invoice: dict) -> bool:
    for field in ("spendings_category", "spendings_subcategory"):
        actual = spending_name(operation.get(field)).casefold()
        expected = spending_name(invoice.get(field)).casefold()
        if actual and expected and actual != expected:
            return False
    return True


def report(operations: list[dict], period: Period, invoices: Sequence[dict] = ()) -> PaymentReport:
    by_id = {str(invoice["id"]): invoice for invoice in invoices if invoice.get("id")}
    candidates = defaultdict(list)
    for invoice in invoices:
        for entry in invoice.get("payments") or ():
            if isinstance(entry, dict):
                key = _key(entry, invoice.get("supplier"), -money(entry.get("amount")))
                if key is not None:
                    candidates[key].append((invoice, _day(entry.get("date_issued") or entry.get("date"))))

    expenses = []
    for operation in operations:
        kind = str(operation.get("type") or "").strip().upper()
        if kind not in ("SPENDING", "MONEYBACK"):
            continue
        category = _operation_kind(operation)
        if kind == "MONEYBACK" and (operation.get("patient") or operation.get("patient_id")) and not operation.get("supplier"):
            continue
        day = _day(operation.get("date_issued"))
        if day and not period.contains(day):
            continue
        amount = abs(money(operation.get("amount")))
        if kind == "MONEYBACK":
            amount = -amount
        expenses.append((operation, category, day, amount, _key(operation, operation.get("supplier"), amount)))

    key_counts = Counter(row[4] for row in expenses)
    day_counts = Counter((row[4], row[2]) for row in expenses)
    result = PaymentReport()
    for operation, category, day, amount, key in expenses:
        invoice_id = _invoice_id(operation)
        invoice = by_id.get(invoice_id)
        if invoice is None and not invoice_id and key is not None:
            matches = [(bill, paid_on) for bill, paid_on in candidates[key]
                       if (not paid_on or paid_on == day) and _day(bill.get("date_issued")) <= day
                       and _matches_category(operation, bill)]
            if len(matches) == 1 and (day_counts[key, day] if matches[0][1] else key_counts[key]) == 1:
                invoice = matches[0][0]
        if category is None and invoice is not None:
            category = spending_kind(invoice)
        if category is not None and category != "material":
            continue

        currency = str(operation.get("currency") or "").strip().upper()
        recipient = spending_name(operation.get("supplier") or (invoice or {}).get("supplier"))
        row = Payment(day, recipient or "Oluvchi ko'rsatilmagan", amount,
                      spending_name(operation.get("cash_desk_name")), spending_name(operation.get("comment")), currency,
                      str((invoice or {}).get("id") or invoice_id), _day((invoice or {}).get("date_issued")))
        if category is None:
            row.reason = MISSING_CATEGORY
        elif not day:
            row.reason = "To'lov sanasi aniqlanmadi"
        elif currency and currency not in ("UZS", "СУМ", "SO'M"):
            row.reason = "To'lov boshqa valyutada"
        if row.reason:
            result.review.append(row)
        else:
            result.rows.append(row)
    result.rows.sort(key=lambda row: (row.date, row.recipient.casefold()), reverse=True)
    return result
