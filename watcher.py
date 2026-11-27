#!/usr/bin/env python3
"""
EUIC 2027 watcher — controlla RK9 (e pokemon.com) e ti spamma di email
quando escono novità sulle iscrizioni TCG all'EUIC 2027.

Livelli di allerta (crescenti, ognuno notificato una sola volta,
tranne OPEN che può ripetersi ad ogni run):
  0 NONE       nessuna novità
  1 ANNOUNCED  l'EUIC 2027 compare su RK9 / pokemon.com
  2 SCHEDULED  RK9 mostra la data di apertura iscrizioni ("registration opens ...")
  3 OPEN       iscrizioni TCG aperte

Config via variabili d'ambiente (in GitHub: Settings > Secrets and variables > Actions):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO (secret, anche più indirizzi separati da virgola)
  MAIL_FROM          (opz.) default = SMTP_USER
  SPAM_COUNT         (opz.) quante email per burst, default 10
  SPAM_DELAY_SEC     (opz.) pausa tra email del burst, default 5
  REPEAT_WHEN_OPEN   (opz.) "true" = a iscrizioni aperte rispamma ad ogni run, default true
  REPEAT_COUNT       (opz.) email per ogni run successivo a iscrizioni aperte, default 1
  EVENT_YEAR         (opz.) default 2027
  TEST_EMAIL         (opz.) "true" = manda una mail di prova e basta
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

YEAR = os.getenv("EVENT_YEAR", "2027")
STATE_FILE = Path(__file__).with_name("state.json")

RK9_EVENTS = "https://www.rk9.gg/events/pokemon"
RK9_EVENT_PAGE = f"https://www.rk9.gg/event/pokemon-euic-{YEAR}"
POKEMON_NEWS = [
    "https://www.pokemon.com/uk/pokemon-news",
    "https://www.pokemon.com/us/pokemon-news",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

LEVELS = {0: "NONE", 1: "ANNOUNCED", 2: "SCHEDULED", 3: "OPEN"}

# "Europe Pokémon International Championships", "Europe International Championships",
# "European International Championship", "EUIC" ...
EUIC_RE = re.compile(
    r"(europe(an)?\s+(pok[eé]mon\s+)?international\s+championships?|\beuic\b)",
    re.I,
)


@dataclass
class Finding:
    level: int
    source: str
    detail: str
    url: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ok_sources: int = 0

    @property
    def level(self) -> int:
        return max((f.level for f in self.findings), default=0)


# ----------------------------------------------------------------------------- utils

def fetch(url: str) -> tuple[int, str]:
    r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    return r.status_code, r.text


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def is_open_text(text: str) -> bool:
    """True se il testo dice che la registrazione è aperta (non 'opens', non 'closed')."""
    t = text.lower()
    if "registration closed" in t or "registration is closed" in t:
        return False
    return bool(re.search(r"registration\s+(is\s+)?(now\s+)?open\b(?!s)", t))


def opens_snippet(text: str) -> str | None:
    m = re.search(r"registration\s+opens?\s+[^.|]{3,80}", text, re.I)
    return m.group(0).strip() if m else None


# ----------------------------------------------------------------------------- checks

def check_rk9_events(rep: Report) -> None:
    """Lista eventi Pokémon su RK9: cerca l'EUIC dell'anno giusto."""
    status, html = fetch(RK9_EVENTS)
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    rep.ok_sources += 1
    text = page_text(html)

    # Considera solo la sezione "upcoming": la parte "Past events" contiene l'EUIC vecchio.
    past = re.search(r"\bpast\s+events\b", text, re.I)
    upcoming = text[: past.start()] if past else text
    require_year = past is None  # se non trovo la separazione, pretendo l'anno vicino al nome

    for m in EUIC_RE.finditer(upcoming):
        # segmento dell'evento: fino al prossimo "... Championships" (evento successivo)
        nxt = re.search(r"championships?", upcoming[m.end():], re.I)
        end = m.end() + (nxt.start() if nxt else 260)
        window = upcoming[max(0, m.start() - 60): min(end, m.end() + 260)]
        if require_year and YEAR not in window:
            continue
        snippet = opens_snippet(upcoming[m.end():end])
        if snippet:
            rep.findings.append(Finding(2, "RK9 events", snippet, RK9_EVENTS))
        else:
            rep.findings.append(Finding(1, "RK9 events", window.strip()[:300], RK9_EVENTS))


def check_rk9_event_page(rep: Report) -> None:
    """Pagina evento diretta (rk9.gg/event/pokemon-euic-2027) + pagina torneo TCG."""
    status, html = fetch(RK9_EVENT_PAGE)
    if status == 404:
        rep.ok_sources += 1
        return
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    rep.ok_sources += 1

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    text = page_text(html)
    if YEAR not in title and not re.search(rf"{YEAR}\s+europe", text, re.I):
        return  # pagina generica / redirect alla home

    rep.findings.append(Finding(1, "RK9 event page", title or "pagina evento online", RK9_EVENT_PAGE))
    snippet = opens_snippet(text)
    if snippet:
        rep.findings.append(Finding(2, "RK9 event page", snippet, RK9_EVENT_PAGE))

    # trova il link "Registration details" nella sezione TCG
    tcg_link = None
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True).lower()
        if "registration" not in label:
            continue
        # risali fino a un contenitore che contenga l'intestazione della sezione
        node, ctx = a, ""
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            ctx = node.get_text(" ", strip=True)
            if re.search(r"\bTCG\b|trading card", ctx, re.I):
                break
        if re.search(r"\bTCG\b|trading card", ctx, re.I) and not re.search(r"\bVGC\b|UNITE|Pok[eé]mon GO", ctx[:80]):
            tcg_link = urljoin(RK9_EVENT_PAGE, a["href"])
            break

    if not tcg_link:
        return
    st2, html2 = fetch(tcg_link)
    if st2 != 200:
        return
    t2 = page_text(html2)
    if is_open_text(t2) or re.search(r"\bregister now\b", t2, re.I):
        rep.findings.append(Finding(3, "RK9 TCG tournament", "Registrazione TCG APERTA", tcg_link))
    else:
        sn = opens_snippet(t2)
        if sn:
            rep.findings.append(Finding(2, "RK9 TCG tournament", sn, tcg_link))


def check_pokemon_news(rep: Report) -> None:
    """News ufficiali pokemon.com (se la pagina è renderizzata lato server)."""
    any_ok = False
    for url in POKEMON_NEWS:
        try:
            status, html = fetch(url)
        except Exception:
            continue
        if status != 200:
            continue
        any_ok = True
        text = page_text(html)
        pat = re.compile(
            rf"[^.]{{0,80}}{YEAR}\s+europe(an)?\s+international\s+championships?[^.]{{0,80}}", re.I
        )
        for m in pat.finditer(text):
            s = m.group(0).strip()
            lvl = 3 if re.search(r"registration\s+is\s+now\s+open|now open", s, re.I) else (
                2 if re.search(r"registration", s, re.I) else 1
            )
            rep.findings.append(Finding(lvl, "pokemon.com news", s[:300], url))
    if any_ok:
        rep.ok_sources += 1
    else:
        raise RuntimeError("nessuna pagina news raggiungibile")


CHECKS = [
    ("RK9 events", check_rk9_events),
    ("RK9 event page", check_rk9_event_page),
    ("pokemon.com news", check_pokemon_news),
]


# ----------------------------------------------------------------------------- state & mail

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"notified_level": 0, "fail_streak": 0, "fail_alert_sent": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def send_mail(subject: str, body: str, count: int = 1, delay: float = 0) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    to = [x.strip() for x in os.environ["MAIL_TO"].split(",") if x.strip()]
    sender = os.getenv("MAIL_FROM") or user

    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=ctx)
    with server:
        server.login(user, pwd)
        for i in range(count):
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = ", ".join(to)
            msg["Subject"] = f"{subject} [{i + 1}/{count}]" if count > 1 else subject
            msg.set_content(body)
            server.send_message(msg)
            print(f"  mail inviata {i + 1}/{count}")
            if delay and i < count - 1:
                time.sleep(delay)


def build_body(rep: Report) -> str:
    lines = [f"EUIC {YEAR} watcher — livello: {LEVELS[rep.level]}", ""]
    for f in sorted(rep.findings, key=lambda x: -x.level):
        lines += [f"[{LEVELS[f.level]}] {f.source}", f"  {f.detail}", f"  {f.url}", ""]
    lines += [
        "Link utili:",
        f"  {RK9_EVENTS}",
        f"  {RK9_EVENT_PAGE}",
        "",
        "Per fermare lo spam: GitHub > Actions > 'EUIC watcher' > ... > Disable workflow",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- main

def main() -> int:
    dry_run = "--dry-run" in sys.argv
    spam_count = int(os.getenv("SPAM_COUNT", "10"))
    spam_delay = float(os.getenv("SPAM_DELAY_SEC", "5"))
    repeat_open = os.getenv("REPEAT_WHEN_OPEN", "true").lower() == "true"
    repeat_count = int(os.getenv("REPEAT_COUNT", "1"))

    if os.getenv("TEST_EMAIL", "").lower() == "true":
        send_mail(f"[TEST] EUIC {YEAR} watcher", "Se leggi questa mail, la configurazione SMTP funziona.")
        return 0

    state = load_state()
    rep = Report()
    for name, fn in CHECKS:
        try:
            fn(rep)
            print(f"[ok]   {name}")
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"{name}: {e}")
            print(f"[fail] {name}: {e}")

    print(f"Livello rilevato: {LEVELS[rep.level]}  (già notificato: {LEVELS[state['notified_level']]})")
    for f in rep.findings:
        print(f"  - [{LEVELS[f.level]}] {f.source}: {f.detail[:150]}")

    # --- salute: se tutte le fonti falliscono per ~3h, avvisa una volta
    if rep.ok_sources == 0:
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        if state["fail_streak"] >= 12 and not state.get("fail_alert_sent") and not dry_run:
            send_mail(
                f"[EUIC watcher] Nessuna fonte raggiungibile da {state['fail_streak']} run",
                "Il watcher non riesce a leggere RK9/pokemon.com (blocco IP? sito cambiato?).\n\n"
                + "\n".join(rep.errors),
            )
            state["fail_alert_sent"] = True
    else:
        state["fail_streak"] = 0
        state["fail_alert_sent"] = False

    # --- notifiche
    level = rep.level
    should_notify = level > state["notified_level"] or (level == 3 and repeat_open)
    if level > 0 and should_notify:
        if level == 3:
            first_time = state["notified_level"] < 3
            # primo avviso: burst completo; run successivi: promemoria (evita i limiti SMTP, es. Gmail 500/giorno)
            count = spam_count if first_time else repeat_count
            subject = f"🚨 ISCRIZIONI TCG EUIC {YEAR} APERTE! 🚨"
        elif level == 2:
            subject, count = f"📅 EUIC {YEAR}: annunciata la data di apertura iscrizioni", spam_count
        else:
            subject, count = f"👀 EUIC {YEAR} comparso su RK9/pokemon.com", 3
        body = build_body(rep)
        print(f"Invio {count} mail: {subject}")
        if not dry_run:
            send_mail(subject, body, count=count, delay=spam_delay)
        state["notified_level"] = max(state["notified_level"], level)

    if not dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
