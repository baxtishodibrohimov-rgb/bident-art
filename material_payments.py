"""Cliniccards kassa operatsiyalaridan to'lovlar hisoboti."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from finance import member_info, spending_kind, spending_name
from report import Period, money

MISSING_CATEGORY = "Xarajat toifasi aniqlanmadi"
UZS_CURRENCIES = ("", "UZS", "СУМ", "СЎМ", "SO'M")
INVOICE_FIELDS = ("spending_id", "spendings_id", "expense_id")
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
    operation_type: str = ""
    id: str = ""
    cash_desk_id: str = ""
    category: str = ""
    subcategory: str = ""
    standalone: bool = False


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


def payment_day(value) -> str:
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
    return next((str(operation[field]) for field in INVOICE_FIELDS if operation.get(field) not in (None, "", 0, "0")), "")


def payment_id(entry: dict) -> str:
    return str(entry.get("payment_id") or entry.get("cash_flow_operation_id") or entry.get("id") or "")


def _payment_amount(operation: dict, kind: str) -> Decimal:
    amount = money(operation.get("amount"))
    if kind == "SPENDING":
        return abs(amount)
    if kind == "MONEYBACK":
        return -abs(amount)
    return amount


def _payment(operation: dict, day: str, recipient: str, kind: str, invoice: dict | None = None) -> Payment:
    bill = invoice or {}
    return Payment(
        date=day, recipient=recipient, amount=_payment_amount(operation, kind),
        cash_desk=spending_name(operation.get("cash_desk_name")), note=spending_name(operation.get("comment")),
        currency=str(operation.get("currency") or "").strip().upper().translate(str.maketrans("‘’ʻʼ", "''''")),
        invoice_id=str(bill.get("id") or _invoice_id(operation)), invoice_date=payment_day(bill.get("date_issued")),
        operation_type=kind, id=payment_id(operation), cash_desk_id=str(operation.get("cash_desk_id") or ""),
        category=spending_name(operation.get("spendings_category")),
        subcategory=spending_name(operation.get("spendings_subcategory")),
        standalone=not _invoice_id(operation) and any(field in operation for field in INVOICE_FIELDS),
    )


def counterparty_payments(operations: list[dict], period: Period, invoices: Iterable[dict] = ()) -> list[Payment]:
    invoice_names, supplier_names, member_names, payment_names = {}, {}, {}, {}
    for invoice in invoices:
        if not invoice["supplier"]:
            continue
        if invoice["id"]:
            invoice_names[invoice["id"]] = invoice["supplier"]
        if invoice["supplier_id"]:
            supplier_names[invoice["supplier_id"]] = invoice["supplier"]
        if invoice["member_id"]:
            member_names[invoice["member_id"]] = invoice["supplier"]
        for entry in invoice["payments"]:
            if entry["id"]:
                payment_names[entry["id"]] = invoice["supplier"]
    rows = []
    seen = set()
    for operation in operations:
        kind = str(operation.get("type") or "").strip().upper()
        if kind not in ("SPENDING", "MONEYBACK") or operation.get("patient") or operation.get("patient_id"):
            continue
        supplier = operation.get("supplier")
        supplier_id = (supplier.get("id") if isinstance(supplier, dict) else None) or operation.get("supplier_id")
        member_id, member_name = member_info(operation)
        row_id = payment_id(operation)
        recipient = (invoice_names.get(_invoice_id(operation)) or payment_names.get(row_id)
                     or supplier_names.get(str(supplier_id or ""))
                     or spending_name(supplier) or spending_name(operation.get("supplier_name"))
                     or member_names.get(member_id) or spending_name(member_name))
        if not recipient or (row_id and row_id in seen):
            continue
        day = payment_day(operation.get("date_issued") or operation.get("date"))
        if day and not period.contains(day):
            continue
        rows.append(_payment(operation, day, recipient, kind))
        if row_id:
            seen.add(row_id)
    return sorted(rows, key=lambda row: row.date, reverse=True)


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
                    candidates[key].append((invoice, payment_day(entry.get("date_issued") or entry.get("date"))))

    expenses = []
    for operation in operations:
        kind = str(operation.get("type") or "").strip().upper()
        if kind not in ("SPENDING", "MONEYBACK"):
            continue
        category = _operation_kind(operation)
        if kind == "MONEYBACK" and (operation.get("patient") or operation.get("patient_id")) and not operation.get("supplier"):
            continue
        day = payment_day(operation.get("date_issued"))
        if day and not period.contains(day):
            continue
        amount = _payment_amount(operation, kind)
        expenses.append((operation, category, day, kind, _key(operation, operation.get("supplier"), amount)))

    key_counts = Counter(row[4] for row in expenses)
    day_counts = Counter((row[4], row[2]) for row in expenses)
    result = PaymentReport()
    for operation, category, day, kind, key in expenses:
        invoice_id = _invoice_id(operation)
        invoice = by_id.get(invoice_id)
        if invoice is None and not invoice_id and key is not None:
            matches = [(bill, paid_on) for bill, paid_on in candidates[key]
                       if (not paid_on or paid_on == day) and payment_day(bill.get("date_issued")) <= day
                       and _matches_category(operation, bill)]
            if len(matches) == 1 and (day_counts[key, day] if matches[0][1] else key_counts[key]) == 1:
                invoice = matches[0][0]
        if category is None and invoice is not None:
            category = spending_kind(invoice)
        if category is not None and category != "material":
            continue

        recipient = spending_name(operation.get("supplier") or (invoice or {}).get("supplier"))
        row = _payment(operation, day, recipient or "Oluvchi ko'rsatilmagan", kind, invoice)
        if category is None:
            row.reason = MISSING_CATEGORY
        elif not day:
            row.reason = "To'lov sanasi aniqlanmadi"
        elif row.currency not in UZS_CURRENCIES:
            row.reason = "To'lov boshqa valyutada"
        if row.reason:
            result.review.append(row)
        else:
            result.rows.append(row)
    result.rows.sort(key=lambda row: (row.date, row.recipient.casefold()), reverse=True)
    return result
