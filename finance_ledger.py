"""Cliniccards schyotlari va kassa to'lovlaridan oluvchi bo'yicha hisobot."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal

from details import Group, name_key
from material_payments import Payment, UZS_CURRENCIES
from report import Period

ZERO = Decimal(0)


@dataclass
class Statement:
    invoices: Group
    payments: list[Payment] = field(default_factory=list)

    @property
    def timeline(self) -> list[dict | Payment]:
        return sorted([*self.invoices.rows, *self.payments],
                      key=lambda row: row.date if isinstance(row, Payment) else row["date"], reverse=True)

    @property
    def paid_total(self) -> Decimal:
        return sum((row.amount for row in self.payments if row.currency in UZS_CURRENCIES), ZERO)

    @property
    def balance(self) -> Decimal:
        return self.paid_total - self.invoices.total


def _matches(entry: dict, invoice: dict, payment: Payment) -> bool:
    if entry["id"] and payment.id:
        return entry["id"] == payment.id
    if entry["cash_desk_id"] and payment.cash_desk_id:
        if entry["cash_desk_id"] != payment.cash_desk_id:
            return False
    elif entry["cash_desk"] and payment.cash_desk:
        if name_key(entry["cash_desk"]) != name_key(payment.cash_desk):
            return False
    if entry["date"] and payment.date:
        return entry["date"] == payment.date
    return not payment.date or invoice["date"] <= payment.date


def _unmatched_payments(groups: list[Group], cash: list[Payment]) -> list[tuple[str, Payment]]:
    invoices = [(group, row) for group in groups for row in group.rows]
    by_id = {invoice["id"]: index for index, (_, invoice) in enumerate(invoices) if invoice["id"]}
    candidates = defaultdict(list)
    remaining = [invoice["paid"] for _, invoice in invoices]
    matched = set()
    allocated = set()

    for index, (group, invoice) in enumerate(invoices):
        for part, entry in enumerate(invoice["payments"]):
            if group.key and entry["amount"]:
                candidates[group.key, entry["amount"]].append((index, part))

    # Avval API ko'rsatgan bog'lanishlar, keyin bir xil summa va kassa bo'yicha mos yozuvlar.
    for cash_index in sorted(range(len(cash)), key=lambda i: (not cash[i].invoice_id, not cash[i].id)):
        payment = cash[cash_index]
        if payment.currency not in UZS_CURRENCIES or payment.standalone:
            continue
        explicit = by_id.get(payment.invoice_id)
        matches = [(index, part) for index, part in candidates[name_key(payment.recipient), payment.amount]
                   if (index, part) not in matched
                   and (not payment.invoice_id or index == explicit)
                   and _matches(invoices[index][1]["payments"][part], invoices[index][1], payment)]
        if matches:
            index, part = min(matches, key=lambda pair: (
                not (payment.id and invoices[pair[0]][1]["payments"][pair[1]]["id"]),
                not invoices[pair[0]][1]["payments"][pair[1]]["date"],
            ))
            matched.add((index, part))
        else:
            index = explicit
        if index is not None:
            remaining[index] -= payment.amount
            allocated.add(cash_index)
            invoice = invoices[index][1]
            if payment.invoice_id or len(matches) == 1:
                cash[cash_index] = replace(payment, invoice_id=invoice["id"],
                                           category=payment.category or invoice["cat"],
                                           subcategory=payment.subcategory or invoice["sub"])

    # Ayrim schyotlarda API to'lov tafsilotlari o'rniga faqat jami summani beradi.
    aggregates = defaultdict(list)
    for index, (group, invoice) in enumerate(invoices):
        if group.key and not invoice["payments"] and remaining[index]:
            aggregates[group.key, remaining[index]].append(index)
    for cash_index, payment in enumerate(cash):
        if cash_index in allocated or payment.invoice_id or payment.standalone or payment.currency not in UZS_CURRENCIES:
            continue
        matches = [index for index in aggregates[name_key(payment.recipient), payment.amount]
                   if remaining[index] and invoices[index][1]["date"] <= payment.date
                   and (not payment.category or name_key(invoices[index][1]["cat"]) == name_key(payment.category))
                   and (not payment.subcategory or name_key(invoices[index][1]["sub"]) == name_key(payment.subcategory))]
        if matches:
            index = matches[0]
            remaining[index] = ZERO
            if len(matches) == 1:
                invoice = invoices[index][1]
                cash[cash_index] = replace(payment, invoice_id=invoice["id"],
                                           category=payment.category or invoice["cat"],
                                           subcategory=payment.subcategory or invoice["sub"])

    unmatched = []
    for index, (group, invoice) in enumerate(invoices):
        amount = remaining[index]
        if not amount:
            continue
        parts = [entry for part, entry in enumerate(invoice["payments"]) if (index, part) not in matched]
        if not parts or sum((entry["amount"] for entry in parts), ZERO) != amount:
            # Ortiqcha kassa to'lovidan sun'iy qaytim chiqarmaymiz.
            if invoice["paid"] * amount <= 0:
                continue
            parts = [{"amount": amount, "date": "", "cash_desk": ""}]
        for entry in parts:
            unmatched.append((group.key, Payment(
                date=entry["date"], recipient=group.name, amount=entry["amount"], cash_desk=entry["cash_desk"],
                note="", currency="", invoice_id=invoice["id"], invoice_date=invoice["date"],
                operation_type="SCHYOT", category=invoice["cat"], subcategory=invoice["sub"],
            )))
    return unmatched


def statements_for_period(groups: list[Group], payments: list[Payment], period: Period,
                          full_history: bool) -> dict[str, Statement]:
    statements = {}
    for group in groups:
        rows = [row for row in group.rows if period.contains(row["date"]) or (full_history and not row["date"])]
        if rows:
            statements[group.key] = Statement(Group(group.key, group.name, rows))

    cash = list(payments)
    entries = _unmatched_payments(groups, cash)
    entries.extend((name_key(row.recipient), row) for row in cash)
    for key, row in entries:
        if not (period.contains(row.date) or (full_history and not row.date)):
            continue
        if key not in statements:
            statements[key] = Statement(Group(key, row.recipient))
        statements[key].payments.append(row)
    return dict(sorted(statements.items(), key=lambda entry: (not entry[0], entry[0])))
