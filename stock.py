"""Sklad hisobi: Cliniccards'dan mahsulot va nakladnoylarni olish, qoldiq, kam qolganlar.

Tartib:
- Birinchi sinxronlashda Cliniccards'dagi BARCHA nakladnoylardan boshlang'ich qoldiq hisoblanadi
  (kirim +, chiqim/hisobdan chiqarish/qaytarish −, skladlar orasidagi ko'chirish hisobga olinmaydi).
- Keyingi sinxronlashlarda faqat YANGI kirim nakladnoylari qo'shiladi.
  Chiqim esa bot orqali (xodim hisoboti + tekshiruvchi tasdig'i) yoziladi, shuning uchun
  Cliniccards'dagi yangi chiqim nakladnoylari qayta hisoblanmaydi — ikki marta ayrilmasin.
"""
from __future__ import annotations

from datetime import date

from db import DB, new_token, now
from report import month_ranges

OUT_HINTS = ("OUT", "WRITE", "SPIS", "RETURN", "EXPENSE", "CONSUM", "USE")


def invoice_sign(inv: dict) -> int:
    """Nakladnoy turi: +1 kirim, −1 chiqim, 0 ko'chirish/noma'lum."""
    t = str(inv.get("type") or "").upper()
    if t in ("INCOME", "IN", "ARRIVAL", "RECEIPT"):
        return 1
    if t in ("MOVE", "MOVEMENT", "TRANSFER"):
        return 0
    if any(h in t for h in OUT_HINTS):
        return -1
    return 0


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_invoices(client, start: date, end: date, chunk: int = 6) -> list[dict]:
    out, seen = [], set()
    for a, b in month_ranges(start, end, chunk):
        for inv in client.get("/stock-invoices", {"from": a.isoformat(), "to": b.isoformat()}) or []:
            key = str(inv.get("invoice_id"))
            if key not in seen:
                seen.add(key)
                out.append(inv)
    out.sort(key=lambda i: (str(i.get("date") or ""), str(i.get("invoice_id"))))
    return out


def ensure_supplier(db: DB, name: str, cc_id: str | None) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    row = None
    if cc_id and cc_id != "0":
        row = db.one("SELECT id FROM suppliers WHERE cc_supplier_id=?", (str(cc_id),))
    if row is None:
        row = db.one("SELECT id FROM suppliers WHERE lower(name)=lower(?)", (name,))
    if row:
        if cc_id and cc_id != "0":
            db.run("UPDATE suppliers SET cc_supplier_id=? WHERE id=? AND cc_supplier_id IS NULL", (str(cc_id), row["id"]))
        return row["id"]
    return db.run("INSERT INTO suppliers(name, cc_supplier_id, token) VALUES(?,?,?)",
                  (name, str(cc_id) if cc_id and cc_id != "0" else None, new_token()))


def sync(db: DB, client, start: date, end: date | None = None) -> dict:
    """Cliniccards'dan mahsulotlar va nakladnoylarni olib, bazani yangilaydi. Qisqa hisobot qaytaradi."""
    end = end or date.today()
    products = client.get("/stock-products") or []
    added = 0
    for p in products:
        pid = str(p.get("product_id"))
        unit = p.get("measure_name") or p.get("measure_name_full") or ""
        exists = db.one("SELECT id FROM products WHERE id=?", (pid,))
        if exists:
            db.run("UPDATE products SET name=?, category=?, unit=? WHERE id=?",
                   (p.get("product_name") or pid, p.get("category_name") or "", unit, pid))
        else:
            db.run("INSERT INTO products(id, name, category, unit) VALUES(?,?,?,?)",
                   (pid, p.get("product_name") or pid, p.get("category_name") or "", unit))
            added += 1

    invoices = fetch_invoices(client, start, end)
    first = db.meta("stock_snapshot") is None
    new_income = 0
    with db.tx():
        for inv in invoices:
            inv_id = str(inv.get("invoice_id"))
            if db.one("SELECT 1 FROM seen_invoices WHERE invoice_id=?", (inv_id,)):
                continue
            db.conn.execute("INSERT INTO seen_invoices(invoice_id) VALUES(?)", (inv_id,))
            sign = invoice_sign(inv)
            if sign == 0 or (not first and sign < 0):
                continue
            sup = ensure_supplier(db, inv.get("supplier_name"), inv.get("supplier_id")) if sign > 0 else None
            for item in inv.get("invoice_items") or []:
                pid = str(item.get("product_id"))
                if not db.one("SELECT 1 FROM products WHERE id=?", (pid,)):
                    db.conn.execute("INSERT INTO products(id, name, unit) VALUES(?,?,?)",
                                    (pid, f"Mahsulot #{pid}", item.get("measure_name") or ""))
                count = _num(item.get("count"))
                kind = "boshlangich" if first else "kirim"
                db.move(pid, sign * count, kind, f"nakladnoy {inv.get('invoice_number') or inv_id}",
                        f"{inv.get('date') or ''} {inv.get('supplier_name') or inv.get('type') or ''}".strip())
                if sign > 0:
                    if _num(item.get("price")):
                        db.conn.execute("UPDATE products SET last_price=? WHERE id=?", (_num(item.get("price")), pid))
                    if sup:
                        db.conn.execute("UPDATE products SET supplier_id=? WHERE id=? AND supplier_id IS NULL", (sup, pid))
                    if not first:
                        new_income += 1
        if first:
            db.set_meta("stock_snapshot", now())
    db.set_meta("stock_synced", now())
    return {"products": len(products), "new_products": added, "invoices": len(invoices),
            "first": first, "new_income_items": new_income}


def status(p) -> str:
    if p["qty"] <= 0:
        return "tugagan"
    if p["min_qty"] and p["qty"] <= p["min_qty"]:
        return "kam"
    return "yetarli"


def products(db: DB) -> list:
    return db.q("""SELECT p.*, s.name AS supplier_name, s.chat_id AS supplier_chat
                   FROM products p LEFT JOIN suppliers s ON s.id = p.supplier_id
                   WHERE p.active = 1 ORDER BY p.category, p.name""")


def search(db: DB, text: str, limit: int = 8) -> list:
    words = [w for w in text.lower().split() if w]
    rows = db.q("SELECT * FROM products WHERE active=1 ORDER BY name")
    return [r for r in rows if all(w in (r["name"] + " " + (r["category"] or "")).lower() for w in words)][:limit]


def needs_order(db: DB) -> list:
    """Kam qolgan va ochiq buyurtmasi yo'q mahsulotlar."""
    out = []
    for p in db.q("SELECT * FROM products WHERE active=1 AND min_qty > 0 AND qty <= min_qty"):
        last_in = db.one("SELECT max(created_at) AS t FROM movements WHERE product_id=? AND kind IN ('kirim','boshlangich')",
                         (p["id"],))["t"] or ""
        open_order = db.one("SELECT 1 FROM orders WHERE product_id=? AND status IN ('taklif','yuborildi','qabul') "
                            "AND created_at >= ?", (p["id"], last_in))
        if not open_order:
            out.append(p)
    return out


def suggested_qty(p) -> float:
    if p["order_qty"]:
        return p["order_qty"]
    return max(p["min_qty"] * 2 - p["qty"], 1)


def fmt_qty(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")
