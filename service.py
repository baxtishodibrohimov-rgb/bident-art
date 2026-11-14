"""API'dan ma'lumot yig'ish, keshlash va foizlar sozlamasi."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path

import details
import finance
from client import CliniccardsClient
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from report import (DONE_STATUSES, MonthReport, Period, as_period, build_debts, build_report,
                    dedupe_items, money, month_ranges, patient_name)

DEFAULT_START = "2015-01-01"


def start_date_from_env() -> date:
    """BOSHLANISH_SANA: shu sanadan boshlab barcha ishlar olinadi (Cliniccards'ning butun davri)."""
    raw = (os.environ.get("BOSHLANISH_SANA") or DEFAULT_START).strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"BOSHLANISH_SANA YYYY-MM-DD ko'rinishida bo'lishi kerak, berilgan: {raw!r}")

HERE = Path(__file__).resolve().parent


def load_env(path: Path = HERE / ".env") -> None:
    """Oddiy .env o'quvchi (KEY=VALUE), tashqi kutubxonasiz."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Summary:
    """Bosh ekrandagi 5 ta jami summa."""
    period: Period
    report: MonthReport
    done_total: Decimal       # shu davrda bajarilgan ishlar summasi
    done_count: int
    kassa_total: Decimal      # shu davrda kassaga tushgan jami pul
    kassa_count: int
    clinic_percent: Decimal   # klinika ulushi, %
    doctors_percent: Decimal  # shifokorlar ulushi, %
    expenses_total: Decimal   # Cliniccards xarajatlari
    expenses_count: int

    @property
    def clinic_share(self) -> Decimal:
        return (self.kassa_total * self.clinic_percent / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)

    @property
    def salary_total(self) -> Decimal:
        return self.report.salary

    @property
    def doctors_share(self) -> Decimal:
        return (self.kassa_total * self.doctors_percent / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)

    @property
    def expense_percent(self) -> Decimal:
        return Decimal(100) - self.clinic_percent - self.doctors_percent

    @property
    def expense_budget(self) -> Decimal:
        return self.kassa_total - self.clinic_share - self.doctors_share


class Percents:
    """Shifokor foizlari: foizlar.json -> {"default": 0, "doctors": {"<doctor_id>": 25}}."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> tuple[dict[str, float], float]:
        if not self.path.exists():
            return {}, 0.0
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (data.get("doctors") or {}).items()}, float(data.get("default") or 0)

    def clinic_percent(self) -> float:
        """Klinika ulushi: kassaga tushgan pulning necha foizi (standart KLINIKA_FOIZ yoki 20)."""
        fallback = float(os.environ.get("KLINIKA_FOIZ") or 20)
        if not self.path.exists():
            return fallback
        value = json.loads(self.path.read_text(encoding="utf-8")).get("klinika")
        return fallback if value is None else float(value)

    def doctors_percent(self) -> float:
        """Shifokorlar ulushi: kassaning necha foizi (standart SHIFOKOR_FOIZ yoki 25)."""
        fallback = float(os.environ.get("SHIFOKOR_FOIZ") or 25)
        if not self.path.exists():
            return fallback
        value = json.loads(self.path.read_text(encoding="utf-8")).get("shifokorlar")
        return fallback if value is None else float(value)

    def save(self, doctors: dict[str, float], default: float = 0.0, clinic: float | None = None,
             doctors_share: float | None = None) -> None:
        clinic = self.clinic_percent() if clinic is None else clinic
        doctors_share = self.doctors_percent() if doctors_share is None else doctors_share
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"default": default, "klinika": clinic, "shifokorlar": doctors_share, "doctors": doctors}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)


class Service:
    def __init__(self, client: CliniccardsClient, percents: Percents,
                 start_date: date | None = None, cache_ttl: int = 900, chunk_months: int = 6):
        self.client = client
        self.percents = percents
        self.start_date = start_date or date.fromisoformat(DEFAULT_START)
        self.chunk_months = chunk_months
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # --- kesh ----------------------------------------------------------
    def _cached(self, key: str, fn):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.cache_ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)
        return value

    def clear_cache(self) -> None:
        self._cache.clear()

    # --- ma'lumot -------------------------------------------------------
    def staff(self) -> list[dict]:
        return self._cached("staff", self.client.staff)

    def items(self, until: date | None = None) -> list[dict]:
        """Butun davr: `start_date` dan bugungacha (yoki tanlangan davr oxirigacha) barcha ishlar.

        Kalit davrga bog'liq emas, shuning uchun bir marta yuklangan ma'lumot hamma davrlar uchun ishlatiladi."""
        start = self.start_date
        end = max(date.today(), until or date.today())

        def fetch():
            out: list[dict] = []
            for a, b in month_ranges(start, end, self.chunk_months):
                for status in DONE_STATUSES:
                    out.extend(self.client.plan_items(status, a, b))
            return out

        with self._lock:
            return self._cached(f"items:{start}:{end}", fetch)

    def patient_names(self, ids) -> dict[str, str]:
        names: dict[str, str] = self._cache.setdefault("patients", (float("inf"), {}))[1]
        missing = sorted({str(i) for i in ids if i} - names.keys())
        for p in self.client.patients_by_ids(missing):
            names[str(p.get("patient_id"))] = patient_name(p)
        return names

    def payments(self, period) -> list[dict]:
        p = as_period(period)
        return self._cached(f"payments:{p.start}:{p.end}", lambda: self.client.payments(p.start, p.end))

    def kassa_total(self, period):
        return sum((money(x.get("amount")) for x in self.payments(period)), money(0))

    def spendings(self, period) -> list[dict]:
        p = as_period(period)
        return self._cached(f"spendings:{p.start}:{p.end}", lambda: self.client.spendings(p.start, p.end))

    def done_items(self, period) -> list[dict]:
        """Shu davrda bajarilgan ishlar (`marked_done_date` bo'yicha)."""
        p = as_period(period)
        return [it for it in dedupe_items(self.items(p.end)) if p.contains(it.get("marked_done_date"))]

    def summary(self, period) -> "Summary":
        p = as_period(period)
        rep = self.month_report(p)
        done = self.done_items(p)
        spend = self.spendings(p)
        return Summary(
            period=p,
            report=rep,
            done_total=sum((money(it.get("price_with_discount")) for it in done), money(0)),
            done_count=len(done),
            kassa_total=rep.kassa_total,
            kassa_count=len(self.payments(p)),
            clinic_percent=Decimal(str(self.percents.clinic_percent())),
            doctors_percent=Decimal(str(self.percents.doctors_percent())),
            expenses_total=sum((money(s.get("amount")) for s in spend), money(0)),
            expenses_count=len(spend),
        )

    # --- hisobotlar ------------------------------------------------------
    def month_report(self, period, with_kassa: bool = True) -> MonthReport:
        """`period`: Period yoki 'YYYY-MM' (bitta oy)."""
        period = as_period(period)
        items = self.items(period.end)
        relevant = [it for it in items if any(period.contains(p.get("date")) for p in (it.get("payments") or []))]
        names = self.patient_names(it.get("patient_id") for it in relevant)
        # O'zgarmas: har bir shifokor — 25%
        rep = build_report(relevant, period, self.staff(), {}, names, float(finance.DOCTOR_PCT))
        if with_kassa:
            rep.kassa_total = self.kassa_total(period)
        return rep

    def overview(self, period, staff_salary=0) -> "finance.Overview":
        p = as_period(period)
        rep = self.month_report(p)
        done = self.done_items(p)
        _, material = finance.split_spendings(self.spendings(p))
        return finance.Overview(
            done_total=sum((money(it.get("price_with_discount")) for it in done), money(0)),
            done_count=len(done),
            kassa_total=rep.kassa_total,
            kassa_count=len(self.payments(p)),
            doctors_salary=rep.salary,
            staff_salary=money(staff_salary),
            material_total=sum((money(s.get("amount")) for s in material), money(0)),
            material_count=len(material),
        )

    def doctor_payouts(self, period) -> dict:
        salary, _ = finance.split_spendings(self.spendings(period))
        return finance.payouts_by_doctor(salary, self.staff())

    def material_detail(self, period):
        _, material = finance.split_spendings(self.spendings(period))
        return details.expenses(material)

    # --- sotuvchilar bilan hisob-kitob (butun davr) -------------------------
    def stock_invoices(self) -> list[dict]:
        import stock as _stock
        return self._cached(f"stock_invoices:{self.start_date}",
                            lambda: _stock.fetch_invoices(self.client, self.start_date, date.today()))

    def all_spendings(self) -> list[dict]:
        return self._cached(f"spendings_all:{self.start_date}",
                            lambda: [s for a, b in month_ranges(self.start_date, date.today(), 12)
                                     for s in self.client.spendings(a, b)])

    def supplier_ledger(self, suppliers: list, products: dict) -> dict:
        """{supplier_id: {"bought": [...], "paid": [...], "b": Decimal, "pd": Decimal, "debt": Decimal}}"""
        import stock as _stock
        by_cc = {str(s["cc_supplier_id"]): s["id"] for s in suppliers if s["cc_supplier_id"]}
        by_name = {(s["name"] or "").strip().lower(): s["id"] for s in suppliers}
        out = {s["id"]: {"bought": [], "paid": [], "b": money(0), "pd": money(0)} for s in suppliers}

        def find(cc_id, name):
            return by_cc.get(str(cc_id or "")) or by_name.get((name or "").strip().lower())

        for inv in self.stock_invoices():
            if _stock.invoice_sign(inv) <= 0:
                continue
            sid = find(inv.get("supplier_id"), inv.get("supplier_name"))
            if sid not in out:
                continue
            for it in inv.get("invoice_items") or []:
                qty, price = money(it.get("count")), money(it.get("price"))
                total = money(it.get("total_price")) or qty * price
                pid = str(it.get("product_id"))
                out[sid]["bought"].append({"date": str(inv.get("date") or "")[:10], "doc": inv.get("invoice_number") or inv.get("invoice_id"),
                                           "product": products.get(pid, {}).get("name", f"#{pid}"),
                                           "unit": products.get(pid, {}).get("unit", it.get("measure_name") or ""),
                                           "qty": qty, "price": price, "sum": total})
                out[sid]["b"] += total
        for sp in self.all_spendings():
            sup = sp.get("supplier") or {}
            sid = find(sup.get("id"), sup.get("name"))
            if sid not in out:
                continue
            amount = money(sp.get("paid") if sp.get("paid") not in (None, "") else sp.get("amount"))
            out[sid]["paid"].append({"date": str(sp.get("date_issued") or "")[:10], "amount": amount, "note": sp.get("note") or ""})
            out[sid]["pd"] += amount
        for v in out.values():
            v["bought"].sort(key=lambda r: r["date"])
            v["paid"].sort(key=lambda r: r["date"])
            v["debt"] = v["b"] - v["pd"]
        return out

    # --- tafsilotlar -----------------------------------------------------
    def done_detail(self, period):
        items = self.done_items(period)
        return details.done_work(items, self.staff(), self.patient_names(it.get("patient_id") for it in items))

    def kassa_detail(self, period):
        pays = self.payments(period)
        return details.kassa(pays, self.patient_names(p.get("patient_id") for p in pays))

    def expenses_detail(self, period):
        return details.expenses(self.spendings(period))

    def debts(self, _period=None):
        items = self.items()
        unpaid = [it for it in items if money(it.get("price_with_discount")) > money(it.get("paid"))]
        names = self.patient_names(it.get("patient_id") for it in unpaid)
        return build_debts(unpaid, self.staff(), names)
