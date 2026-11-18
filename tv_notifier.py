#!/usr/bin/env python3
"""TV sport obaveštenja.

Čita TV raspored sa naslovi.net, izdvaja fudbalske utakmice koje odgovaraju
kriterijumima iz config.json i ~1h pre početka šalje ntfy notifikaciju.

Samo standardna biblioteka (Python 3.9+). Pokretanje:

    python tv_notifier.py                 # normalan rad (koristi ga GitHub Actions)
    python tv_notifier.py --list          # prikaži današnje utakmice koje prolaze filtere
    python tv_notifier.py --debug-parse   # prikaži šta je parser pročitao sa stranice
    python tv_notifier.py --dry-run       # kao normalan rad, ali ništa ne šalje i ne pamti
    python tv_notifier.py --test-notify   # pošalji probnu notifikaciju
    python tv_notifier.py --html fajl.html --now 2026-11-18T07:00   # lokalni test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time as time_mod
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "state.json"
TZ = ZoneInfo("Europe/Belgrade")

USER_AGENT = "Mozilla/5.0 (compatible; TVSportNotifier/1.0)"


# --------------------------------------------------------------------------
# Pomoćne funkcije za tekst
# --------------------------------------------------------------------------

def norm(text: str) -> str:
    """Mala slova, bez dijakritika, samo slova/cifre razdvojene razmakom."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("đ", "dj")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def strip_parens(text: str) -> str:
    return re.sub(r"\([^)]*\)", " ", text)


CLUB_NOISE = {"fc", "cf", "ac", "as", "sc", "afc", "fk", "ssc", "us", "ss", "ud", "cd", "rcd", "sd"}


def club_key(name: str) -> str:
    toks = norm(strip_parens(name)).split()
    while toks and toks[0] in CLUB_NOISE:
        toks.pop(0)
    while toks and toks[-1] in CLUB_NOISE:
        toks.pop()
    return " ".join(toks)


def channel_key(name: str) -> str:
    """Ujednačava imena kanala (SBB i naslovi.net ih pišu različito)."""
    key = norm(name)
    key = re.sub(r"\b(?:hd|uhd|fhd)\b", " ", key)
    key = key.replace("arena sport premium", "arena premium")
    key = re.sub(r"^arena sport (\d+x\d+)$", r"arena \1", key)
    return " ".join(key.split())


def comp_key(competition: str) -> str:
    return norm(strip_parens(competition))


# --------------------------------------------------------------------------
# Parsiranje HTML-a
# --------------------------------------------------------------------------

class _ScheduleParser(HTMLParser):
    """Pretvara HTML u listu čvorova (tekst, href_ako_je_link)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[tuple[str, str | None]] = []
        self._buf: list[str] = []
        self._hrefs: list[str] = []
        self._skip = 0

    def flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text and not self._skip:
            href = self._hrefs[-1] if self._hrefs else None
            self.nodes.append((text, href or None))

    def handle_starttag(self, tag, attrs):
        self.flush()
        if tag == "a":
            self._hrefs.append(dict(attrs).get("href") or "")
        elif tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        self.flush()
        if tag == "a" and self._hrefs:
            self._hrefs.pop()
        elif tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        self._buf.append(data)


TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
CHANNEL_HREF_RE = re.compile(r"/tv-program/([^/?#]+)/?$")
CATEGORY_WORDS = {"sport"}


@dataclass
class Event:
    start: datetime
    title: str
    channel: str


def _segment_to_title_channel(seg: list[tuple[str, str | None]]) -> tuple[str, str] | None:
    items = list(seg)
    while items and not items[0][1] and items[0][0].lower() in CATEGORY_WORDS:
        items.pop(0)

    # Glavni put: naslov je tekst pre linka kanala (/tv-program/<kanal>/).
    for k, (text, href) in enumerate(items):
        if href and CHANNEL_HREF_RE.search(href):
            title = " ".join(t for t, _ in items[:k])
            title = re.sub(r"\s*[-–—]\s*$", "", title).strip()
            return (title, text) if title else None

    # Rezerva: "Naslov - Kanal" u jednom tekstualnom čvoru.
    if items and not items[0][1]:
        m = re.match(r"^(.*\S)\s+[-–—]\s+([^-–—]+)$", items[0][0])
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None


def parse_schedule(html: str, base_date: date) -> tuple[list[Event], int]:
    """Vraća (događaji, broj_preskočenih_segmenata).

    Raspored na stranici ide hronološki; ako vreme "preskoči unazad"
    (npr. 23:30 pa 00:15), smatra se da je sledeći dan.
    """
    parser = _ScheduleParser()
    parser.feed(html)
    parser.close()
    parser.flush()
    nodes = parser.nodes

    time_idx = [i for i, (t, h) in enumerate(nodes) if not h and TIME_RE.match(t)]
    events: list[Event] = []
    skipped = 0
    day_offset = 0
    prev_minutes: int | None = None

    for n, i in enumerate(time_idx):
        j = time_idx[n + 1] if n + 1 < len(time_idx) else len(nodes)
        m = TIME_RE.match(nodes[i][0])
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            skipped += 1
            continue
        parsed = _segment_to_title_channel(nodes[i + 1:j])
        if parsed is None:
            skipped += 1
            continue
        minutes = hh * 60 + mm
        if prev_minutes is not None and minutes < prev_minutes - 60:
            day_offset += 1
        prev_minutes = minutes
        start = datetime.combine(base_date + timedelta(days=day_offset), time(hh, mm), tzinfo=TZ)
        events.append(Event(start=start, title=parsed[0], channel=parsed[1]))
    return events, skipped


# --------------------------------------------------------------------------
# Fudbal: prepoznavanje utakmice i filteri
# --------------------------------------------------------------------------

FOOTBALL_PREFIX_RE = re.compile(r"^Fudbal\b\s*[-:]?\s*", re.IGNORECASE)
TEAM_SPLIT_RE = re.compile(r"\s+(?:-|–|—|vs\.?)\s+", re.IGNORECASE)
SEASON_RE = re.compile(r"\b(?:19|20)\d{2}/\d{2,4}\b")
REPLAY_RE = re.compile(r"\(\s*R\s*\)")
WOMEN_RE = re.compile(r"\(\s*[ŽW]\s*\)|\bžen(?:e|ski)\b|\bzen(?:e|ski)\b|\bwomen\b", re.IGNORECASE)


@dataclass
class FootballMatch:
    competition: str
    home: str
    away: str


def parse_football(title: str) -> FootballMatch | None:
    """Vraća utakmicu ako je naslov fudbalska utakmica ("Fudbal - Liga: A - B")."""
    m = FOOTBALL_PREFIX_RE.match(title)
    if not m:
        return None
    rest = title[m.end():].strip()
    if ":" in rest:
        competition, _, teams = rest.partition(":")
    else:
        competition, teams = "", rest
    parts = TEAM_SPLIT_RE.split(teams.strip())
    if len(parts) != 2:
        return None
    home = re.sub(r",.*$", "", parts[0]).strip()
    away = re.sub(r",.*$", "", parts[1]).strip()
    if not home or not away:
        return None
    return FootballMatch(competition.strip(), home, away)


def _rule_hit(rule: dict, ck: str) -> bool:
    return any(k in ck for k in rule["include"]) and not any(k in ck for k in rule.get("exclude", []))


def is_serbia(team: str, cfg: dict) -> bool:
    key = norm(strip_parens(team))
    youth = re.search(r"\bu\d{2}\b", key) is not None
    base = re.sub(r"\bu\d{2}\b", "", key).strip()
    if base != norm(cfg["serbia_team"]):
        return False
    return cfg.get("include_serbia_youth", True) or not youth


def derby_country(fb: FootballMatch, ck: str, cfg: dict) -> str | None:
    home, away = club_key(fb.home), club_key(fb.away)
    for d in cfg["derbies"]:
        if ck not in {comp_key(c) for c in d["competitions"]}:
            continue
        for side_a, side_b in d["pairs"]:
            a = {club_key(x) for x in side_a.split("|")}
            b = {club_key(x) for x in side_b.split("|")}
            if (home in a and away in b) or (home in b and away in a):
                return d["country"]
    return None


def classify(fb: FootballMatch, raw_title: str, cfg: dict) -> list[str]:
    """Lista razloga zbog kojih utakmica prolazi filtere (prazna = ne prolazi)."""
    nt = norm(raw_title)
    if (
        any(k in nt for k in cfg["not_live_keywords"])
        or SEASON_RE.search(raw_title)
        or REPLAY_RE.search(raw_title)
    ):
        return []
    if cfg.get("exclude_women", True) and WOMEN_RE.search(raw_title):
        return []

    ck = comp_key(fb.competition)
    rules = cfg["rules"]
    reasons: list[str] = []
    if _rule_hit(rules["champions_league"], ck):
        reasons.append("Liga šampiona")
    if _rule_hit(rules["nations_league"], ck):
        reasons.append("Liga nacija")
    if _rule_hit(rules["championships"], ck):
        reasons.append("Svetsko/evropsko prvenstvo")
    q = rules.get("qualifiers")
    if (
        q
        and "kvalifik" in ck
        and any(k in ck for k in q["include"])
        and not any(k in ck for k in q.get("exclude", []))
    ):
        famous = {norm(t) for t in q["teams"]}
        if norm(strip_parens(fb.home)) in famous or norm(strip_parens(fb.away)) in famous:
            reasons.append("Kvalifikacije (poznata reprezentacija)")
    if is_serbia(fb.home, cfg) or is_serbia(fb.away, cfg):
        reasons.append("Reprezentacija Srbije")
    country = derby_country(fb, ck, cfg)
    if country:
        reasons.append(f"Derbi ({country})")
    return reasons


def select_matches(events: list[Event], cfg: dict) -> list[dict]:
    """Filtrira i spaja iste utakmice sa više kanala / reprize u jednu stavku."""
    allowed = {channel_key(c) for c in cfg.get("allowed_channels", [])}
    groups: dict[str, list[tuple[Event, FootballMatch, list[str]]]] = {}
    for ev in events:
        if allowed and channel_key(ev.channel) not in allowed:
            continue
        fb = parse_football(ev.title)
        if fb is None:
            continue
        reasons = classify(fb, ev.title, cfg)
        if reasons:
            groups.setdefault(norm(ev.title), []).append((ev, fb, reasons))

    out: list[dict] = []
    for items in groups.values():
        items.sort(key=lambda x: x[0].start)
        first_ev, fb, reasons = items[0]
        channels: list[str] = []
        for ev, _, _ in items:
            # Isti termin na više kanala se spaja; kasnija repriza se ignoriše.
            if ev.start - first_ev.start <= timedelta(minutes=30) and ev.channel not in channels:
                channels.append(ev.channel)
        start_iso = first_ev.start.isoformat()
        out.append({
            "id": hashlib.sha1(f"{start_iso}|{norm(first_ev.title)}".encode()).hexdigest()[:12],
            "start": start_iso,
            "title": first_ev.title,
            "competition": fb.competition,
            "home": fb.home,
            "away": fb.away,
            "channels": channels,
            "reasons": reasons,
        })
    out.sort(key=lambda d: d["start"])
    return out


# --------------------------------------------------------------------------
# Obaveštenja
# --------------------------------------------------------------------------

def due_matches(matches: list[dict], now: datetime, cfg: dict, notified: dict) -> list[dict]:
    """Utakmice koje počinju za najviše lead+tolerancija minuta i još nisu javljene."""
    upper = cfg["lead_minutes"] + cfg["early_tolerance_minutes"]
    lower = cfg["min_minutes_before"]
    due = []
    for m in matches:
        if m["id"] in notified:
            continue
        minutes = (datetime.fromisoformat(m["start"]) - now).total_seconds() / 60
        if lower <= minutes <= upper:
            due.append(m)
    return due


def build_message(m: dict, now: datetime) -> tuple[str, str]:
    start = datetime.fromisoformat(m["start"]).astimezone(TZ)
    minutes = max(0, round((start - now).total_seconds() / 60))
    title = f"{m['home']} – {m['away']}"
    lines = []
    if m["competition"]:
        lines.append(m["competition"])
    lines.append(f"Počinje u {start:%H:%M} (za {minutes} min)")
    lines.append(f"Kanal: {', '.join(m['channels'])}")
    lines.append(f"Razlog: {', '.join(m['reasons'])}")
    return title, "\n".join(lines)


def send_ntfy(title: str, message: str, *, priority: int = 4, tags: list[str] | None = None) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError("Nije podešena promenljiva okruženja NTFY_TOPIC.")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or ["soccer"],
    }
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(server + "/", data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


# --------------------------------------------------------------------------
# Preuzimanje i stanje
# --------------------------------------------------------------------------

def fetch_html(url: str, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "sr,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time_mod.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Preuzimanje nije uspelo: {last}")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("date", None)
    state.setdefault("fetched_at", None)
    state.setdefault("events", [])
    state.setdefault("notified", {})
    state.setdefault("alerts", {})
    state.setdefault("seen", {})
    return state


def drop_reruns(matches: list[dict], state: dict, today: date, cfg: dict, log) -> list[dict]:
    """Odbacuje utakmice koje su već bile na rasporedu prethodnih dana (to su reprize).

    Prvi put kad se utakmica pojavi u rasporedu pamti se datum; ako se ista utakmica
    pojavi kasnijeg dana (u okviru rerun_memory_days), smatra se reprizom.
    """
    window = timedelta(days=cfg.get("rerun_memory_days", 3))
    seen = state["seen"]
    kept = []
    for m in matches:
        key = norm(m["title"])
        first = date.fromisoformat(seen[key]) if key in seen else None
        if first is not None and first < today and today - first <= window:
            log(f"Preskačem reprizu: {m['title']} (već viđeno {first.isoformat()}).")
            continue
        if first is None or today - first > window:
            seen[key] = today.isoformat()
        kept.append(m)
    old = (today - timedelta(days=10)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= old}
    return kept


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def _needs_refresh(state: dict, today: date, now: datetime, cfg: dict, force: bool) -> bool:
    if force or state["date"] != today.isoformat() or not state["fetched_at"]:
        return True
    age = now - datetime.fromisoformat(state["fetched_at"])
    return age >= timedelta(hours=cfg["refresh_hours"])


def _alert_once(state: dict, kind: str, today: date, title: str, message: str, notify, log) -> None:
    if state["alerts"].get(kind) == today.isoformat():
        return
    try:
        notify(title, message, priority=3, tags=["warning"])
        state["alerts"][kind] = today.isoformat()
    except Exception as exc:  # noqa: BLE001
        log(f"Upozorenje nije poslato: {exc}")


# --------------------------------------------------------------------------
# Glavni tok
# --------------------------------------------------------------------------

def run(
    cfg: dict,
    *,
    now: datetime,
    fetch_fn=fetch_html,
    notify_fn=send_ntfy,
    state_path: Path = STATE_PATH,
    dry_run: bool = False,
    force_refresh: bool = False,
    log=print,
) -> int:
    today = now.date()
    state = load_state(state_path)

    # Očisti stare zapise o poslatim obaveštenjima.
    cutoff = (now - timedelta(days=2)).isoformat()
    state["notified"] = {k: v for k, v in state["notified"].items() if v >= cutoff}

    exit_code = 0
    if _needs_refresh(state, today, now, cfg, force_refresh):
        events: list[Event] = []
        errors: list[str] = []
        for url in cfg["urls"]:
            try:
                html = fetch_fn(url)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{url}: {exc}")
                continue
            parsed, skipped = parse_schedule(html, today)
            log(f"{url}: pročitano {len(parsed)} termina (preskočeno {skipped}).")
            events.extend(parsed)

        if events:
            state["events"] = drop_reruns(select_matches(events, cfg), state, today, cfg, log)
            state["date"] = today.isoformat()
            state["fetched_at"] = now.isoformat()
            log(f"Utakmica koje prolaze filtere: {len(state['events'])}.")
        else:
            have_cache = state["date"] == today.isoformat()
            if errors:
                log("Greška pri preuzimanju: " + "; ".join(errors))
                if have_cache:
                    log("Koristim sačuvani današnji raspored.")
                elif not dry_run:
                    _alert_once(state, "fetch", today, "TV obaveštenja: greška",
                                "Ne mogu da preuzmem TV raspored sa stranice. " + errors[0][:200], notify_fn, log)
                    exit_code = 1
                else:
                    exit_code = 1
            else:
                log("Stranica je učitana, ali parser nije našao nijedan termin.")
                if not have_cache:
                    if not dry_run:
                        _alert_once(state, "parse", today, "TV obaveštenja: greška",
                                    "Stranica je učitana ali ne mogu da pročitam raspored. "
                                    "Verovatno se promenio izgled stranice.", notify_fn, log)
                    exit_code = 1

    due = due_matches(state["events"], now, cfg, state["notified"])
    for m in due:
        title, message = build_message(m, now)
        if dry_run:
            log(f"[dry-run] Poslao bih: {title}\n{message}\n")
            continue
        try:
            notify_fn(title, message, priority=cfg["priority"], tags=["soccer"])
        except Exception as exc:  # noqa: BLE001
            log(f"Slanje nije uspelo za {title}: {exc}")
            exit_code = 1
            continue
        state["notified"][m["id"]] = m["start"]
        log(f"Poslato: {title}")
    if not due:
        log("Nema obaveštenja za slanje u ovom trenutku.")

    if not dry_run:
        save_state(state_path, state)
    return exit_code


def _print_list(matches: list[dict]) -> None:
    if not matches:
        print("Nema utakmica koje prolaze filtere.")
    for m in matches:
        start = datetime.fromisoformat(m["start"]).astimezone(TZ)
        print(f"{start:%d.%m. %H:%M}  {m['home']} – {m['away']}  [{m['competition']}]  "
              f"{', '.join(m['channels'])}  ({', '.join(m['reasons'])})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TV sport obaveštenja (fudbal)")
    ap.add_argument("--list", action="store_true", help="prikaži današnje utakmice koje prolaze filtere")
    ap.add_argument("--debug-parse", action="store_true", help="prikaži šta je parser pročitao")
    ap.add_argument("--dry-run", action="store_true", help="ne šalji i ne pamti ništa")
    ap.add_argument("--test-notify", action="store_true", help="pošalji probnu notifikaciju")
    ap.add_argument("--refresh", action="store_true", help="ponovo preuzmi raspored")
    ap.add_argument("--html", help="koristi lokalni HTML fajl umesto preuzimanja")
    ap.add_argument("--now", help="lažno trenutno vreme, npr. 2026-11-18T07:00")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    now = datetime.fromisoformat(args.now).replace(tzinfo=TZ) if args.now else datetime.now(TZ)

    if args.test_notify:
        send_ntfy("Probna notifikacija", "Ako ovo vidiš, ntfy radi.", priority=3, tags=["white_check_mark"])
        print("Poslato.")
        return 0

    if args.html:
        html_text = Path(args.html).read_text(encoding="utf-8")
        fetch_fn = lambda url: html_text  # noqa: E731
    else:
        fetch_fn = fetch_html

    if args.debug_parse or args.list:
        events: list[Event] = []
        for url in cfg["urls"]:
            parsed, skipped = parse_schedule(fetch_fn(url), now.date())
            print(f"{url}: {len(parsed)} termina, preskočeno {skipped}")
            events.extend(parsed)
        if args.debug_parse:
            for ev in events[:15]:
                print(f"  {ev.start:%d.%m. %H:%M}  {ev.channel:<22} {ev.title}")
            football = [e for e in events if parse_football(e.title)]
            print(f"Fudbalskih utakmica (A - B): {len(football)}")
        if args.list:
            selected = select_matches(events, cfg)
            _print_list(selected)
            if cfg.get("allowed_channels"):
                have = {norm(m["title"]) for m in selected}
                others = [m for m in select_matches(events, dict(cfg, allowed_channels=[]))
                          if norm(m["title"]) not in have]
                if others:
                    print("\nProlaze filtere, ali samo na kanalima koji nisu u allowed_channels:")
                    _print_list(others)
        return 0

    return run(cfg, now=now, fetch_fn=fetch_fn, dry_run=args.dry_run, force_refresh=args.refresh)


if __name__ == "__main__":
    sys.exit(main())
