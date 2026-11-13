"""Telegram bot: sklad hisoboti va sotuvchilarga buyurtma.

Ish tartibi:
1. Har kuni SKLAD_SOROV_VAQTI da (standart 18:00) bot «olib ketuvchi» xodimlardan so'raydi:
   «Bugun skladdan narsa oldingizmi?» — [Ha] [Yo'q].
2. «Ha» bo'lsa: mahsulot nomini yozadi → ro'yxatdan tanlaydi → sonini yozadi → [Yana qo'shish] [Tugatdim].
3. Tekshiruvchiga: «Skladda shu mahsulotlar shuncha qolishi kerak. To'g'rimi?» — [To'g'ri] [Noto'g'ri].
   «Noto'g'ri» bo'lsa, har bir mahsulotning haqiqiy sonini so'raydi va qoldiqni shunga tenglashtiradi.
4. Adminlarga: «Cliniccards'dan chiqaring: ...» — [Chiqarildi].
5. Kam qolgan mahsulot uchun adminlarga taklif: «... buyurtma beraymi?» — [Yuborish] [Bekor].
   «Yuborish» → sotuvchiga buyurtma xabari — [Qabul qildim].

Telegram boti odamga birinchi bo'lib yoza olmaydi: har bir xodim va sotuvchi dasturdagi
maxsus havolani bir marta ochib «Start» bosadi (`/start <token>`).
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime

import httpx

import staff
import stock
from db import DB, TZ, now, today

fq = stock.fmt_qty


def pul(v) -> str:
    return f"{float(v):,.0f}".replace(",", " ")


class TelegramAPI:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.token = token
        self.http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}/", timeout=40, transport=transport)

    def call(self, method: str, **params):
        r = self.http.post(method, json={k: v for k, v in params.items() if v is not None})
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method}: {body.get('description')} ({body.get('error_code')})")
        return body["result"]


def kb(*rows) -> dict:
    """kb([("Ha", "data"), ...], ...) → inline klaviatura."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class Bot:
    def __init__(self, db: DB, api: TelegramAPI, clinic: str = "Klinika"):
        self.db = db
        self.api = api
        self.clinic = clinic
        self.username = db.meta("bot_username") or os.environ.get("TELEGRAM_BOT_USERNAME", "")
        self.last_error = ""
        self._msg_id = None

    # ------------------------------------------------------------------ yordamchi
    def send(self, chat_id, text, markup=None):
        try:
            return self.api.call("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)
        except Exception as e:  # bitta xabar yuborilmasa, qolgan ish to'xtamasin
            self.last_error = str(e)
            return None

    def link(self, token: str) -> str:
        return f"https://t.me/{self.username}?start={token}" if self.username else f"(bot nomi noma'lum) /start {token}"

    def person(self, chat_id):
        return self.db.one("SELECT * FROM people WHERE chat_id=?", (chat_id,))

    def admins(self):
        return self.db.q("SELECT * FROM people WHERE is_admin=1 AND chat_id IS NOT NULL")

    def checkers(self):
        return self.db.q("SELECT * FROM people WHERE is_checker=1 AND chat_id IS NOT NULL")

    def to_admins(self, text, markup=None):
        for a in self.admins():
            self.send(a["chat_id"], text, markup)

    def identify(self) -> None:
        try:
            me = self.api.call("getMe")
            self.username = me.get("username", "")
            self.db.set_meta("bot_username", self.username)
        except Exception as e:
            self.last_error = str(e)

    # ------------------------------------------------------------------ kunlik so'rov
    def ask_takers(self, force: bool = False) -> int:
        day = today()
        sent = 0
        for p in self.db.q("SELECT * FROM people WHERE is_taker=1 AND chat_id IS NOT NULL"):
            if not force and self.db.one("SELECT 1 FROM reports WHERE person_id=? AND day=?", (p["id"], day)):
                continue
            rid = self.db.run("INSERT INTO reports(person_id, day, status, created_at) VALUES(?,?,?,?)",
                              (p["id"], day, "soraldi", now()))
            self.send(p["chat_id"], f"Assalomu alaykum, {p['name']}!\nBugun ({day}) skladdan biror narsa oldingizmi?",
                      kb([("Ha, oldim", f"r:yes:{rid}"), ("Yo'q", f"r:no:{rid}")]))
            sent += 1
        return sent

    # ------------------------------------------------------------------ davomat
    def ask_attendance(self, force: bool = False) -> int:
        day = today()
        if not force and self.db.one("SELECT 1 FROM clinic_days WHERE day=?", (day,)):
            return 0
        d = datetime.fromisoformat(day)
        sent = 0
        for p in self.db.q("SELECT * FROM people WHERE is_attendance=1 AND chat_id IS NOT NULL"):
            self.send(p["chat_id"], f"Davomat: bugun {d:%d.%m.%Y} ({staff.WEEKDAYS[d.weekday()]}) klinika ishladimi?",
                      kb([("Ha, ishladi", f"att:w:{day}:1"), ("Yo'q, dam olish", f"att:w:{day}:0")]))
            sent += 1
        return sent

    def _att_kb(self, ids, selected):
        names = {w["id"]: w["full_name"] for w in self.db.q("SELECT id, full_name FROM workers")}
        rows = [[(("✅ " if i in selected else "⬜ ") + names.get(i, f"#{i}"), f"att:t:{i}")] for i in ids]
        return kb(*rows, [("Tayyor", "att:ok")])

    def on_attendance(self, chat_id, p, parts):
        action = parts[0]
        st = self.db.state(chat_id)
        if action == "w":
            day, worked = parts[1], parts[2] == "1"
            if not worked:
                staff.save_day(self.db, day, False, [], [], p["id"])
                self.db.set_state(chat_id, None)
                self.send(chat_id, f"Yozib qo'ydim: {day} — klinika ishlamadi.")
                return
            ids = [w["id"] for w in self.db.q("SELECT id FROM workers WHERE active=1 ORDER BY full_name")]
            if not ids:
                staff.save_day(self.db, day, True, [], [], p["id"])
                self.send(chat_id, "Yozib qo'ydim: klinika ishladi. Dasturda hali xodimlar qo'shilmagan.")
                return
            self.db.set_state(chat_id, {"step": "att_present", "day": day, "ids": ids, "sel": ids, "extra": []})
            self.send(chat_id, "Kimlar keldi? Kelmaganlarni bosib olib tashlang, keyin «Tayyor».", self._att_kb(ids, ids))
        elif action == "t" and st.get("step") in ("att_present", "att_extra"):
            wid = int(parts[1])
            key = "sel" if st["step"] == "att_present" else "extra"
            ids = st["ids"] if st["step"] == "att_present" else st["sel"]
            cur = set(st[key])
            cur ^= {wid}
            st[key] = [i for i in ids if i in cur]
            self.db.set_state(chat_id, st)
            try:
                self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=self._msg_id,
                              reply_markup=self._att_kb(ids, set(st[key])))
            except Exception:
                pass
        elif action == "ok" and st.get("step") == "att_present":
            if not st["sel"]:
                staff.save_day(self.db, st["day"], True, [], [], p["id"])
                self.db.set_state(chat_id, None)
                self.send(chat_id, "Yozib qo'ydim: klinika ishladi, xodimlardan hech kim belgilanmadi.")
                return
            st["step"] = "att_extra"
            self.db.set_state(chat_id, st)
            self.send(chat_id, "Kimlar normadan ko'p ishladi? Belgilang (hech kim bo'lmasa, darhol «Tayyor»).",
                      self._att_kb(st["sel"], set()))
        elif action == "ok" and st.get("step") == "att_extra":
            staff.save_day(self.db, st["day"], True, st["sel"], st["extra"], p["id"])
            self.db.set_state(chat_id, None)
            day = datetime.fromisoformat(st["day"]).date()
            pays = {wp.worker["id"]: wp for wp in staff.payroll(self.db, day, day)}
            lines = []
            for wid in st["sel"]:
                wp = pays.get(wid)
                if wp:
                    mark = " (+qo'shimcha)" if wp.extra_days else ""
                    lines.append(f"• {wp.worker['full_name']}{mark} — {pul(wp.total)} so'm")
            rest = " Bugun dam olish kuni: hammaga qo'shimcha hisoblandi." if staff.is_rest_day(day) else ""
            text = f"Davomat {day:%d.%m.%Y}:\n" + "\n".join(lines) + f"\nJami: {pul(sum(pays[w].total for w in st['sel'] if w in pays))} so'm.{rest}"
            self.send(chat_id, "Rahmat! " + text)
            for a in self.admins():
                if a["chat_id"] != chat_id:
                    self.send(a["chat_id"], text)
        else:
            self.send(chat_id, "Bu davomat allaqachon yuborilgan.")

    # ------------------------------------------------------------------ yangilanishlar
    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat_id = cq["message"]["chat"]["id"]
            try:
                self.api.call("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass
            data = cq.get("data") or ""
            self._msg_id = cq["message"]["message_id"]
            if not data.startswith("att:t:"):  # belgilash tugmalari qoladi, qolganlari bosilgach olinadi
                try:
                    self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=self._msg_id,
                                  reply_markup={"inline_keyboard": []})
                except Exception:
                    pass
            self.on_button(chat_id, data)
        elif "message" in upd:
            msg = upd["message"]
            chat_id = msg["chat"]["id"]
            text = (msg.get("text") or "").strip()
            if text.startswith("/start"):
                self.on_start(chat_id, text[6:].strip(), msg.get("from") or {})
            elif text == "/oldim":
                self.start_manual_report(chat_id)
            elif text in ("/bekor", "/cancel"):
                self.db.set_state(chat_id, None)
                self.send(chat_id, "Bekor qilindi.")
            else:
                self.on_text(chat_id, text)

    def on_start(self, chat_id, token, user):
        if token:
            p = self.db.one("SELECT * FROM people WHERE token=?", (token,))
            if p:
                self.db.run("UPDATE people SET chat_id=? WHERE id=?", (chat_id, p["id"]))
                roles = [r for r, f in (("sklad hisoboti", p["is_taker"]), ("sklad tekshiruvi", p["is_checker"]),
                                        ("tasdiq va buyurtmalar", p["is_admin"]), ("kunlik davomat", p["is_attendance"])) if f]
                self.send(chat_id, f"{p['name']}, siz {self.clinic} botiga ulandingiz.\nVazifangiz: {', '.join(roles) or '—'}."
                          + ("\nSkladdan narsa olganingizda istalgan vaqtda /oldim deb yozishingiz mumkin." if p["is_taker"] else ""))
                return
            s = self.db.one("SELECT * FROM suppliers WHERE token=?", (token,))
            if s:
                self.db.run("UPDATE suppliers SET chat_id=? WHERE id=?", (chat_id, s["id"]))
                self.send(chat_id, f"Assalomu alaykum! Siz {self.clinic} klinikasining sotuvchilar ro'yxatiga ulandingiz "
                                   f"({s['name']}). Buyurtmalar shu yerga keladi.")
                self.to_admins(f"Sotuvchi botga ulandi: {s['name']}")
                return
        self.send(chat_id, f"Bu bot {self.clinic} klinikasi uchun. Administrator bergan maxsus havola orqali kiring.")

    def start_manual_report(self, chat_id):
        p = self.person(chat_id)
        if not p or not p["is_taker"]:
            self.send(chat_id, "Bu buyruq faqat sklad hisobotini beradigan xodimlar uchun.")
            return
        rid = self.db.run("INSERT INTO reports(person_id, day, status, created_at) VALUES(?,?,?,?)",
                          (p["id"], today(), "kiritilmoqda", now()))
        self.db.set_state(chat_id, {"step": "search", "report": rid})
        self.send(chat_id, "Qaysi mahsulotni oldingiz? Nomini (yoki bir qismini) yozing.")

    # ------------------------------------------------------------------ tugmalar
    def on_button(self, chat_id, data: str):
        parts = data.split(":")
        kind = parts[0]
        p = self.person(chat_id)
        try:
            if kind == "r" and p:                       # xodim: Ha / Yo'q
                self.on_report_answer(chat_id, p, parts[1], int(parts[2]))
            elif kind == "pick" and p:                  # xodim mahsulot tanladi
                st = self.db.state(chat_id)
                if st.get("report"):
                    prod = self.db.one("SELECT * FROM products WHERE id=?", (parts[1],))
                    self.db.set_state(chat_id, {**st, "step": "qty", "product": parts[1]})
                    self.send(chat_id, f"{prod['name']} — nechta oldingiz? ({prod['unit'] or 'dona'})\nSonini yozing, masalan: 2")
            elif kind == "more" and p:
                st = self.db.state(chat_id)
                self.db.set_state(chat_id, {**st, "step": "search"})
                self.send(chat_id, "Keyingi mahsulot nomini yozing.")
            elif kind == "done" and p:
                self.finish_report(chat_id, int(parts[1]))
            elif kind == "chk" and p and p["is_checker"]:  # tekshiruvchi
                self.on_check(chat_id, p, parts[1], int(parts[2]))
            elif kind == "cc" and p and p["is_admin"]:     # admin: Cliniccards'dan chiqarildi
                self.db.run("UPDATE reports SET status='cc_chiqarildi', cc_done_at=? WHERE id=?", (now(), int(parts[1])))
                self.send(chat_id, "Belgilandi: Cliniccards'dan chiqarildi.")
            elif kind == "ord" and p and p["is_admin"]:    # admin: buyurtma
                self.on_order_decision(chat_id, parts[1], int(parts[2]))
            elif kind == "sup":                           # sotuvchi: qabul qildim
                self.on_supplier_ack(chat_id, int(parts[1]))
            elif kind == "att" and p and p["is_attendance"]:  # davomat
                self.on_attendance(chat_id, p, parts[1:])
        except Exception as e:
            self.last_error = f"{e}"
            traceback.print_exc()
            self.send(chat_id, "Xatolik yuz berdi, qaytadan urinib ko'ring yoki /bekor deb yozing.")

    def on_report_answer(self, chat_id, p, answer, rid):
        rep = self.db.one("SELECT * FROM reports WHERE id=? AND person_id=?", (rid, p["id"]))
        if not rep or rep["status"] not in ("soraldi",):
            self.send(chat_id, "Bu so'rovga javob allaqachon berilgan.")
            return
        if answer == "no":
            self.db.run("UPDATE reports SET status='olinmadi' WHERE id=?", (rid,))
            self.send(chat_id, "Rahmat, yozib qo'ydim: bugun skladdan hech narsa olinmadi.")
        else:
            self.db.run("UPDATE reports SET status='kiritilmoqda' WHERE id=?", (rid,))
            self.db.set_state(chat_id, {"step": "search", "report": rid})
            self.send(chat_id, "Qaysi mahsulotni oldingiz? Nomini (yoki bir qismini) yozing.")

    # ------------------------------------------------------------------ matn
    def on_text(self, chat_id, text):
        st = self.db.state(chat_id)
        step = st.get("step")
        if step == "search":
            found = stock.search(self.db, text)
            if not found:
                self.send(chat_id, f"«{text}» topilmadi. Boshqacha yozib ko'ring (masalan, nomining bir qismi).")
                return
            self.send(chat_id, "Qaysi biri?", kb(*[[(f"{r['name']} ({fq(r['qty'])} {r['unit']})"[:60], f"pick:{r['id']}")]
                                                  for r in found]))
        elif step == "qty":
            n = self.parse_num(text)
            if n is None or n <= 0:
                self.send(chat_id, "Iltimos, faqat son yozing, masalan: 2 yoki 0.5")
                return
            rid, pid = st["report"], st["product"]
            self.db.run("INSERT INTO report_items(report_id, product_id, qty) VALUES(?,?,?)", (rid, pid, n))
            self.db.set_state(chat_id, {"step": "added", "report": rid})
            items = self.report_lines(rid)
            self.send(chat_id, "Yozildi:\n" + items, kb([("Yana qo'shish", "more"), ("Tugatdim", f"done:{rid}")]))
        elif step == "actual":
            n = self.parse_num(text)
            if n is None or n < 0:
                self.send(chat_id, "Iltimos, skladdagi haqiqiy sonni yozing, masalan: 4")
                return
            self.on_actual(chat_id, st, n)
        else:
            p = self.person(chat_id)
            hint = "\nSkladdan narsa olgan bo'lsangiz, /oldim deb yozing." if p and p["is_taker"] else ""
            self.send(chat_id, "Hozir javob kutilayotgan savol yo'q." + hint)

    @staticmethod
    def parse_num(text):
        try:
            return float(text.replace(",", ".").split()[0])
        except (ValueError, IndexError):
            return None

    def report_lines(self, rid, with_expected=False) -> str:
        rows = self.db.q("""SELECT ri.*, p.name, p.unit, p.qty AS stock FROM report_items ri
                            JOIN products p ON p.id = ri.product_id WHERE ri.report_id=? ORDER BY ri.id""", (rid,))
        lines = []
        for r in rows:
            line = f"• {r['name']} — {fq(r['qty'])} {r['unit']}"
            if with_expected:
                line += f" → skladda {fq(r['expected_after'])} {r['unit']} qolishi kerak"
            lines.append(line)
        return "\n".join(lines) or "—"

    # ------------------------------------------------------------------ hisobot → tekshiruv
    def finish_report(self, chat_id, rid):
        rep = self.db.one("SELECT r.*, pe.name FROM reports r JOIN people pe ON pe.id=r.person_id WHERE r.id=?", (rid,))
        if not rep or rep["status"] != "kiritilmoqda":
            self.send(chat_id, "Bu hisobot allaqachon yuborilgan.")
            return
        self.db.set_state(chat_id, None)
        items = self.db.q("SELECT * FROM report_items WHERE report_id=?", (rid,))
        if not items:
            self.db.run("UPDATE reports SET status='olinmadi' WHERE id=?", (rid,))
            self.send(chat_id, "Hech narsa kiritilmadi.")
            return
        # kutilgan qoldiq: hozirgi qoldiq − shu hisobotda olingan (bir mahsulot bir necha marta bo'lsa — yig'indisi)
        taken: dict[str, float] = {}
        for it in items:
            taken[it["product_id"]] = taken.get(it["product_id"], 0) + it["qty"]
        for it in items:
            cur = self.db.one("SELECT qty FROM products WHERE id=?", (it["product_id"],))["qty"]
            self.db.run("UPDATE report_items SET expected_after=? WHERE id=?", (cur - taken[it["product_id"]], it["id"]))
        self.db.run("UPDATE reports SET status='tekshiruvda' WHERE id=?", (rid,))
        self.send(chat_id, "Rahmat! Hisobot tekshiruvchiga yuborildi:\n" + self.report_lines(rid))
        checkers = self.checkers()
        text = (f"Sklad tekshiruvi. {rep['name']} {rep['day']} kuni oldi:\n{self.report_lines(rid, True)}\n\n"
                "Skladga borib tekshiring. Qoldiq to'g'rimi?")
        if not checkers:
            self.to_admins("Diqqat: tekshiruvchi botga ulanmagan. " + text)
        for c in checkers:
            self.send(c["chat_id"], text, kb([("To'g'ri", f"chk:ok:{rid}"), ("Noto'g'ri", f"chk:bad:{rid}")]))

    def on_check(self, chat_id, p, answer, rid):
        rep = self.db.one("SELECT * FROM reports WHERE id=?", (rid,))
        if not rep or rep["status"] != "tekshiruvda":
            self.send(chat_id, "Bu hisobot allaqachon tekshirilgan.")
            return
        if answer == "ok":
            self.apply_report(rid, p, actual=None)
            self.send(chat_id, "Rahmat! Qoldiq yangilandi.")
        else:
            items = [r["id"] for r in self.db.q("SELECT id FROM report_items WHERE report_id=? ORDER BY id", (rid,))]
            self.db.set_state(chat_id, {"step": "actual", "report": rid, "items": items, "pos": 0})
            self.ask_actual(chat_id, items[0])

    def ask_actual(self, chat_id, item_id):
        r = self.db.one("""SELECT ri.*, p.name, p.unit FROM report_items ri JOIN products p ON p.id=ri.product_id
                           WHERE ri.id=?""", (item_id,))
        self.send(chat_id, f"{r['name']}: skladda haqiqatda nechta bor? ({r['unit'] or 'dona'})\n"
                           f"Kutilgan: {fq(r['expected_after'])}")

    def on_actual(self, chat_id, st, n):
        items, pos = st["items"], st["pos"]
        self.db.run("UPDATE report_items SET actual_after=? WHERE id=?", (n, items[pos]))
        pos += 1
        if pos < len(items):
            self.db.set_state(chat_id, {**st, "pos": pos})
            self.ask_actual(chat_id, items[pos])
            return
        self.db.set_state(chat_id, None)
        self.apply_report(st["report"], self.person(chat_id), actual=True)
        self.send(chat_id, "Rahmat! Qoldiq haqiqiy songa tenglashtirildi, farq adminga yuborildi.")

    def apply_report(self, rid, checker, actual):
        rep = self.db.one("SELECT r.*, pe.name FROM reports r JOIN people pe ON pe.id=r.person_id WHERE r.id=?", (rid,))
        items = self.db.q("""SELECT ri.*, p.name, p.unit FROM report_items ri JOIN products p ON p.id=ri.product_id
                             WHERE ri.report_id=? ORDER BY ri.id""", (rid,))
        diffs = []
        with self.db.tx():
            for it in items:
                self.db.move(it["product_id"], -it["qty"], "chiqim", f"hisobot #{rid}", f"{rep['name']}, {rep['day']}")
            if actual:
                done = set()
                for it in items:
                    if it["product_id"] in done or it["actual_after"] is None:
                        continue
                    done.add(it["product_id"])
                    d = self.db.set_qty(it["product_id"], it["actual_after"], f"hisobot #{rid}", "tekshiruvda farq")
                    if d:
                        diffs.append(f"• {it['name']}: kutilgan {fq(it['expected_after'])}, haqiqatda "
                                     f"{fq(it['actual_after'])} ({'+' if d > 0 else ''}{fq(d)} {it['unit']})")
            status = "farq" if diffs else "tasdiqlandi"
            self.db.conn.execute("UPDATE reports SET status=?, checked_by=?, checked_at=? WHERE id=?",
                                 (status, checker["id"] if checker else None, now(), rid))
        lines = "\n".join(f"• {it['name']} — {fq(it['qty'])} {it['unit']}" for it in items)
        text = (f"Tekshirildi ({checker['name'] if checker else '—'}). {rep['name']} {rep['day']} kuni oldi:\n{lines}\n\n"
                "Cliniccards'dan chiqarish kerak (Склад → Списание).")
        if diffs:
            text += "\n\nSkladda farq chiqdi, qoldiq haqiqiy songa tenglashtirildi:\n" + "\n".join(diffs)
        self.to_admins(text, kb([("Chiqarildi", f"cc:{rid}")]))
        self.propose_orders()

    # ------------------------------------------------------------------ buyurtmalar
    def propose_orders(self) -> int:
        n = 0
        for p in stock.needs_order(self.db):
            qty = stock.suggested_qty(p)
            sup = self.db.one("SELECT * FROM suppliers WHERE id=?", (p["supplier_id"],)) if p["supplier_id"] else None
            oid = self.db.run("INSERT INTO orders(product_id, supplier_id, qty, price, status, created_at) VALUES(?,?,?,?,?,?)",
                              (p["id"], sup["id"] if sup else None, qty, p["last_price"], "taklif", now()))
            total = qty * p["last_price"]
            text = (f"Skladda kam qoldi: {p['name']} — {fq(p['qty'])} {p['unit']} (chegara {fq(p['min_qty'])}).\n"
                    f"Taklif: {fq(qty)} {p['unit']}" + (f" × {pul(p['last_price'])} = ~{pul(total)} so'm" if p["last_price"] else "")
                    + f"\nSotuvchi: {sup['name'] if sup else 'belgilanmagan'}")
            if sup and sup["chat_id"]:
                self.to_admins(text + "\n\nSotuvchiga buyurtma yuborilsinmi?",
                               kb([("Yuborish", f"ord:send:{oid}"), ("Bekor", f"ord:cancel:{oid}")]))
            else:
                why = "sotuvchi belgilanmagan" if not sup else "sotuvchi botga ulanmagan"
                self.to_admins(text + f"\n\nBuyurtmani bot yubora olmaydi: {why}. Dasturdagi «Sotuvchilar» bo'limida sozlang.",
                               kb([("Bekor qilish", f"ord:cancel:{oid}")]))
            n += 1
        return n

    def order_text(self, o) -> str:
        p = self.db.one("SELECT * FROM products WHERE id=?", (o["product_id"],))
        return f"{p['name']} — {fq(o['qty'])} {p['unit']}"

    def on_order_decision(self, chat_id, action, oid):
        o = self.db.one("SELECT * FROM orders WHERE id=?", (oid,))
        if not o or o["status"] != "taklif":
            self.send(chat_id, "Bu buyurtma bo'yicha qaror allaqachon qabul qilingan.")
            return
        if action == "cancel":
            self.db.run("UPDATE orders SET status='bekor', answered_at=? WHERE id=?", (now(), oid))
            self.send(chat_id, "Buyurtma bekor qilindi.")
            return
        sup = self.db.one("SELECT * FROM suppliers WHERE id=?", (o["supplier_id"],)) if o["supplier_id"] else None
        if not sup or not sup["chat_id"]:
            self.send(chat_id, "Sotuvchi botga ulanmagan, buyurtma yuborilmadi.")
            return
        sent = self.send(sup["chat_id"], f"Assalomu alaykum! {self.clinic} klinikasidan buyurtma:\n{self.order_text(o)}\n\n"
                                         "Iltimos, qabul qilganingizni tasdiqlang.", kb([("Qabul qildim", f"sup:{oid}")]))
        if sent is None:
            self.send(chat_id, f"Sotuvchiga yuborib bo'lmadi: {self.last_error}")
            return
        self.db.run("UPDATE orders SET status='yuborildi', sent_at=? WHERE id=?", (now(), oid))
        self.send(chat_id, f"Buyurtma {sup['name']}ga yuborildi: {self.order_text(o)}")

    def on_supplier_ack(self, chat_id, oid):
        o = self.db.one("""SELECT o.*, s.name AS sname FROM orders o JOIN suppliers s ON s.id=o.supplier_id
                           WHERE o.id=? AND s.chat_id=?""", (oid, chat_id))
        if not o or o["status"] != "yuborildi":
            return
        self.db.run("UPDATE orders SET status='qabul', answered_at=? WHERE id=?", (now(), oid))
        self.send(chat_id, "Rahmat! Buyurtmangiz qayd etildi.")
        self.to_admins(f"{o['sname']} buyurtmani qabul qildi: {self.order_text(o)}")

    # ------------------------------------------------------------------ fon jarayonlari
    def poll_forever(self, stop: threading.Event):
        offset = int(self.db.meta("tg_offset", "0") or 0)
        while not stop.is_set():
            try:
                updates = self.api.call("getUpdates", offset=offset, timeout=25,
                                        allowed_updates=["message", "callback_query"])
                self.last_error = "" if self.last_error.startswith("Telegram getUpdates") else self.last_error
                for u in updates:
                    offset = u["update_id"] + 1
                    self.db.set_meta("tg_offset", str(offset))
                    try:
                        self.handle(u)
                    except Exception:
                        traceback.print_exc()
            except Exception as e:
                self.last_error = str(e)
                stop.wait(10)

    def schedule_forever(self, stop: threading.Event, at: str, attendance_at: str = "20:00"):
        """Har kuni `at` da sklad so'rovi, `attendance_at` da davomat so'rovi (klinika vaqti, HH:MM)."""
        jobs = (("last_ask_day", at, self.ask_takers), ("last_att_day", attendance_at, self.ask_attendance))
        while not stop.is_set():
            t = datetime.now(TZ)
            for key, when, fn in jobs:
                try:
                    if t.strftime("%H:%M") >= when and self.db.meta(key) != t.strftime("%Y-%m-%d"):
                        self.db.set_meta(key, t.strftime("%Y-%m-%d"))
                        fn()
                except Exception as e:
                    self.last_error = str(e)
            stop.wait(30)


def start_background(db: DB, token: str, clinic: str, at: str, attendance_at: str = "20:00") -> Bot:
    bot = Bot(db, TelegramAPI(token), clinic)
    bot.identify()
    stop = threading.Event()
    threading.Thread(target=bot.poll_forever, args=(stop,), daemon=True).start()
    threading.Thread(target=bot.schedule_forever, args=(stop, at, attendance_at), daemon=True).start()
    bot._stop = stop
    return bot
