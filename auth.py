"""Kirish sahifasi uchun sessiya: imzolangan cookie, tashqi kutubxonasiz.

Cookie ichida: login | amal qilish muddati | HMAC imzo.
Imzo kaliti SESSION_SECRET (bo'lmasa, paroldan olinadi — parol o'zgarsa, eski sessiyalar bekor bo'ladi).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

COOKIE = "hisobot_sessiya"
SHORT = 12 * 3600          # oddiy kirish: 12 soat
LONG = 30 * 24 * 3600      # "Eslab qolish": 30 kun


class NeedLogin(Exception):
    pass


def credentials() -> tuple[str, str]:
    return os.environ.get("HISOBOT_LOGIN", "admin"), os.environ.get("HISOBOT_PAROL", "")


def _key() -> bytes:
    secret = os.environ.get("SESSION_SECRET") or ("hisobot:" + credentials()[1])
    return hashlib.sha256(secret.encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(user: str, ttl: int) -> str:
    payload = f"{user}|{int(time.time()) + ttl}"
    return f"{payload}|{_sign(payload)}"


def check_token(token: str | None) -> str | None:
    if not token or token.count("|") != 2:
        return None
    user, exp, sig = token.split("|")
    if not hmac.compare_digest(sig, _sign(f"{user}|{exp}")):
        return None
    if not exp.isdigit() or int(exp) < time.time():
        return None
    if user != credentials()[0]:
        return None
    return user


def check_password(user: str, password: str) -> bool:
    good_user, good_pass = credentials()
    if not good_pass:
        return False
    return secrets.compare_digest(user.strip().encode(), good_user.encode()) & \
        secrets.compare_digest(password.encode(), good_pass.encode())


class Attempts:
    """Bir IP'dan 5 daqiqada ko'pi bilan 5 ta noto'g'ri urinish."""

    def __init__(self, limit: int = 5, window: int = 300):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _clean(self, ip: str) -> deque[float]:
        q = self._fails[ip]
        while q and time.time() - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        return len(self._clean(ip)) >= self.limit

    def fail(self, ip: str) -> None:
        self._clean(ip).append(time.time())

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)


def safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"
