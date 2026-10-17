#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общие правила разбора служебной почты — для читалки приёмника и для сбора с Минска.

Зачем отдельный модуль (17.10.2026): письма теперь приходят двумя путями —
IMAP-приёмник (`mail_monitor.py`) и чтение Gmail внутри профиля AdsPower на Минске
(`POST /api/mail/inbox`). Классификация и маска почты обязаны быть одними и теми же,
иначе одно и то же письмо получит две разные категории в зависимости от дороги.

Здесь нет ни сети, ни базы — только чистые функции, чтобы их можно было гонять тестами
на обезличенных строках и импортировать откуда угодно (сервер, читалка, тесты).
"""
import hashlib
import re

UC_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Классификация: сначала проверяемые правила (отправитель + тема), §8 ТЗ.
# НЕ опираемся на один домен или одно английское слово — паттерны мультиязычные,
# по умолчанию important_unclassified, чтобы ничего не уехало в неверную категорию молча.
# (type, domain_substrings, subject_regex) — subject_regex без учёта регистра.
#
# Порядок — от частного к общему, и это важно: правило «готовность» ловит `moneti`,
# то есть любую тему со словом monetization, и стоя первым оно съедало «Monetization
# disabled». Поэтому авторские права, отключение и результат рассмотрения идут ДО
# готовности. Домен `youtube.com` у платежей — по живому письму с профиля 20
# (17.10.2026): «Changes to your YouTube payment details» приходит с no-reply@youtube.com,
# а не с adsense/google.
RULES = [
    ("copyright",
     ["youtube.com", "google.com"],
     r"(copyright|авторск|claim|претензи|strike|страйк|content id|удал.*за наруш)"),
    ("monetization_limited_or_off",
     ["youtube.com", "google.com"],
     r"(disabled|отключ|limited|ограничен|suspend|приостанов|turned off|monetization.*(off|disabled))"),
    ("monetization_review_result",
     ["youtube.com", "google.com"],
     r"(review|рассмотр|проверк|approved|одобрен|rejected|отклон|not approved|не одобрен|decision|решени)"),
    ("monetization_eligible",
     ["youtube.com", "google.com"],
     r"(moneti|монетиз|zarab|partner program|партнёрск|партнерск|YPP|применить|eligib|подать заявку)"),
    ("adsense_payments",
     ["adsense", "payments.google.com", "google.com", "youtube.com"],
     r"(adsense|payment|выплат|платёж|платеж|payout|threshold|порог|earnings|доход)"),
    ("account_security",
     ["google.com", "accounts.google.com"],
     r"(security|безопасн|sign-?in|вход|verify|подтверд|suspicious|подозрит|recovery|восстановл|password|пароль)"),
]

# Как категория называется человеку в пульте. Порядок — от «деньги» к «шум».
TYPE_TITLES = {
    "monetization_eligible": "можно подавать на монетизацию",
    "monetization_review_result": "результат рассмотрения заявки",
    "monetization_limited_or_off": "монетизация ограничена или отключена",
    "copyright": "авторские права",
    "adsense_payments": "AdSense и платежи",
    "account_security": "безопасность аккаунта",
    "important_unclassified": "важное без категории",
}

CONFIDENCE_TITLES = {
    "high": "канал назван в письме",
    "medium": "по почте аккаунта",
    "low": "по профилю AdsPower",
    "review": "на проверку: канал не определён",
    "manual": "канал указан человеком",
}


def classify(from_hdr, subject):
    """Тип события по отправителю и теме. Неизвестное — important_unclassified, не тишина."""
    f = (from_hdr or "").lower()
    s = subject or ""
    for etype, domains, subj_re in RULES:
        if any(d in f for d in domains) and re.search(subj_re, s, re.I):
            return etype
    return "important_unclassified"


def mask_email(e):
    """`min0628625171@gmail.com` → `min…@gmail.com`. Этого хватает, чтобы узнать почту
    в разборе, и не хватает, чтобы её восстановить: полные адреса в студию не едут."""
    e = str(e or "")
    if "@" not in e:
        return e
    u, d = e.split("@", 1)
    return u[:3] + "…@" + d


def mask_emails_in(text):
    """Снять все адреса из свободного текста (сниппет письма Google повторяет почту
    аккаунта целиком — см. «Security alert»)."""
    return EMAIL_RE.sub(lambda m: mask_email(m.group(0)), str(text or ""))


def uc_hits(*texts):
    """Все UC… из текстов, без повторов, в порядке появления."""
    out = []
    for t in texts:
        for uc in UC_RE.findall(str(t or "")):
            if uc not in out:
                out.append(uc)
    return out


def inbox_message_key(profile, login_tag, sender, subject, date_text):
    """Ключ дедупликации строки списка Gmail: у строки нет Message-ID, есть только то,
    что видно в списке. Профиль и почта входят в ключ: одно и то же письмо Google
    на двух аккаунтах одного профиля — два письма, не одно."""
    raw = "|".join(str(x or "").strip() for x in (profile, login_tag, sender, subject, date_text))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def idem_key(message_key, etype, uc):
    """Тот же вид ключа, что у читалки приёмника: сообщение|тип|канал."""
    return hashlib.sha256(f"{message_key}|{etype}|{uc or ''}".encode("utf-8")).hexdigest()
