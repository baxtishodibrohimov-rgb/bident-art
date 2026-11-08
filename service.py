"""API'dan ma'lumot yig'ish, keshlash va foizlar sozlamasi."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path

from client import CliniccardsClient
from report import (DONE_STATUSES, MonthReport, as_period, build_debts, build_report,
                    money, month_ranges, patient_name)

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


class Percents:
    """Shifokor foizlari: foizlar.json -> {"default": 0, "doctors": {"<doctor_id>": 25}}."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> tuple[dict[str, float], float]:
        if not self.path.exists():
            return {}, 0.0
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (data.get("doctors") or {}).items()}, float(data.get("default") or 0)

    def save(self, doctors: dict[str, float], default: float = 0.0) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"default": default, "doctors": doctors}, ensure_ascii=False, indent=2),
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

    def kassa_total(self, period):
        p = as_period(period)
        return self._cached(f"payments:{p.start}:{p.end}",
                            lambda: sum((money(x.get("amount")) for x in self.client.payments(p.start, p.end)),
                                        money(0)))

    # --- hisobotlar ------------------------------------------------------
    def month_report(self, period, with_kassa: bool = True) -> MonthReport:
        """`period`: Period yoki 'YYYY-MM' (bitta oy)."""
        period = as_period(period)
        items = self.items(period.end)
        relevant = [it for it in items if any(period.contains(p.get("date")) for p in (it.get("payments") or []))]
        names = self.patient_names(it.get("patient_id") for it in relevant)
        percents, default = self.percents.load()
        rep = build_report(relevant, period, self.staff(), percents, names, default)
        if with_kassa:
            rep.kassa_total = self.kassa_total(period)
        return rep

    def debts(self, _period=None):
        items = self.items()
        unpaid = [it for it in items if money(it.get("price_with_discount")) > money(it.get("paid"))]
        names = self.patient_names(it.get("patient_id") for it in unpaid)
        return build_debts(unpaid, self.staff(), names)
