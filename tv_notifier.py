#!/usr/bin/env python3
"""TV sport obaveštenja (fudbal).

Tok rada:
  1. Zvanični rasporedi takmičenja (UEFA / ESPN JSON) kažu KOJE utakmice se
     igraju danas i u koliko sati počinju. Samo one dolaze u obzir, pa reprize
     ne mogu da prođu.
  2. naslovi.net TV raspored služi samo da se nađe KANAL na kom se utakmica
     prenosi (termin mora da se poklopi sa početkom utakmice).
  3. ~1h pre početka šalje se ntfy notifikacija.

Samo standardna biblioteka (Python 3.9+). Pokretanje:

    python tv_notifier.py                # normalan rad (koristi ga GitHub Actions)
    python tv_notifier.py --refresh      # ponovo preuzmi i upiši state/probe.txt
    python tv_notifier.py --list         # današnje utakmice koje prolaze filtere
    python tv_notifier.py --probe        # proveri da li svi izvori rade
    python tv_notifier.py --debug-parse  # šta je parser pročitao sa TV stranice
    python tv_notifier.py --dry-run      # kao normalan rad, ali ništa ne šalje
    python tv_notifier.py --test-notify  # probna notifikacija
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
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "state.json"
STATE_VERSION = 2
TZ = ZoneInfo("Europe/Belgrade")

USER_AGENT = "Mozilla/5.0 (compatible; TVSportNotifier/2.0)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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


# --------------------------------------------------------------------------
# TV raspored (naslovi.net): samo za nalaženje kanala
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

    for k, (text, href) in enumerate(items):
        if href and CHANNEL_HREF_RE.search(href):
            title = " ".join(t for t, _ in items[:k])
            title = re.sub(r"\s*[-–—]\s*$", "", title).strip()
            return (title, text) if title else None

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


FOOTBALL_PREFIX_RE = re.compile(r"^Fudbal\b\s*[-:]?\s*", re.IGNORECASE)
TEAM_SPLIT_RE = re.compile(r"\s+(?:-|–|—|vs\.?)\s+", re.IGNORECASE)
REPLAY_RE = re.compile(r"\(\s*R\s*\)")
SEASON_RE = re.compile(r"\b(?:19|20)\d{2}/\d{2,4}\b")


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


def tv_football(events: list[Event], cfg: dict) -> list[tuple[Event, FootballMatch]]:
    """Fudbalski termini sa kanala iz paketa, bez najava, pregleda i repriza."""
    allowed = {channel_key(c) for c in cfg.get("allowed_channels", [])}
    out = []
    for ev in events:
        if allowed and channel_key(ev.channel) not in allowed:
            continue
        fb = parse_football(ev.title)
        if fb is None:
            continue
        nt = norm(ev.title)
        if any(k in nt for k in cfg.get("not_live_keywords", [])) or SEASON_RE.search(ev.title) \
                or REPLAY_RE.search(ev.title):
            continue
        out.append((ev, fb))
    return out


# --------------------------------------------------------------------------
# Imena timova: poklapanje srpskih (TV) i engleskih (zvanični izvori) imena
# --------------------------------------------------------------------------

COUNTRIES_SR_EN = {
    "albanija": "albania", "andora": "andorra", "jermenija": "armenia", "austrija": "austria",
    "azerbejdzan": "azerbaijan", "belorusija": "belarus", "belgija": "belgium",
    "bosna i hercegovina": "bosnia and herzegovina", "bih": "bosnia and herzegovina",
    "bugarska": "bulgaria", "hrvatska": "croatia", "kipar": "cyprus", "ceska": "czechia|czech republic",
    "danska": "denmark", "engleska": "england", "estonija": "estonia", "farska ostrva": "faroe islands",
    "finska": "finland", "francuska": "france", "gruzija": "georgia", "nemacka": "germany",
    "gibraltar": "gibraltar", "grcka": "greece", "madjarska": "hungary", "island": "iceland",
    "izrael": "israel", "italija": "italy", "kazahstan": "kazakhstan", "kosovo": "kosovo",
    "letonija": "latvia", "lihtenstajn": "liechtenstein", "litvanija": "lithuania",
    "luksemburg": "luxembourg", "malta": "malta", "moldavija": "moldova", "crna gora": "montenegro",
    "holandija": "netherlands", "severna makedonija": "north macedonia",
    "severna irska": "northern ireland", "norveska": "norway", "poljska": "poland",
    "portugal": "portugal", "portugalija": "portugal", "irska": "republic of ireland|ireland",
    "rumunija": "romania", "rusija": "russia", "san marino": "san marino", "skotska": "scotland",
    "srbija": "serbia", "slovacka": "slovakia", "slovenija": "slovenia", "spanija": "spain",
    "svedska": "sweden", "svajcarska": "switzerland", "turska": "turkey|turkiye|turkiye",
    "ukrajina": "ukraine", "vels": "wales", "brazil": "brazil", "argentina": "argentina",
    "urugvaj": "uruguay", "kolumbija": "colombia", "meksiko": "mexico", "sad": "united states|usa",
    "japan": "japan", "juzna koreja": "south korea", "maroko": "morocco", "senegal": "senegal",
    "australija": "australia", "kanada": "canada", "egipat": "egypt", "nigerija": "nigeria",
}

GENERIC_TOKENS = {
    "fc", "cf", "ac", "as", "sc", "afc", "fk", "sk", "if", "bk", "united", "city", "club", "real",
    "sporting", "athletic", "inter", "the", "de", "la", "del", "of", "and", "dynamo", "dinamo",
    "olympique", "olympiakos", "sport", "town", "county", "rovers", "wanderers", "utd", "siti", "junajted", "milan", "milano",
}


# Grupe istih klubova (srpski nazivi sa TV-a i nazivi sa stranih sajtova)
CLUB_ALIASES = [
    "crvena zvezda|zvezda|crvena zvezda beograd|red star belgrade|red star|crvena zvezda belgrade|fk crvena zvezda",
    "partizan|partizan beograd|partizan belgrade|fk partizan",
    "vojvodina|vojvodina novi sad|fk vojvodina|vojvodina novi sad",
    "bajern|bajern minhen|bayern|bayern munich|bayern munchen",
    "mancester junajted|manchester united|man united|man utd",
    "mancester siti|manchester city|man city",
    "atletiko madrid|atletiko|atletico madrid|atletico",
    "borusija dortmund|borussia dortmund|dortmund",
    "pari sen žermen|pari sen zermen|psg|paris saint germain|paris saint-germain",
    "inter|internazionale|inter milan|inter milano",
    "juventus|juve",
    "ajaks|ajax|ajax amsterdam",
    "benfika|benfica|sl benfica",
    "porto|fc porto",
    "olimpijakos|olympiacos|olympiakos",
    "galatasaraj|galatasaray",
    "slavija prag|slavia prague|slavia praha",
    "sparta prag|sparta prague|sparta praha",
    "bodo glimt|bodo/glimt|bodoe glimt",
    "lask|lask linz",
    "aek atina|aek athens|aek",
    "kobenhavn|fc copenhagen|copenhagen|fc kobenhavn",
]
_ALIAS_INDEX: dict[str, set[str]] = {}
for _g in CLUB_ALIASES:
    _vs = {norm(x) for x in _g.split("|")}
    _vs |= {club_key(x) for x in _g.split("|")}
    for _v in _vs:
        _ALIAS_INDEX.setdefault(_v, set()).update(_vs)


def _name_variants(name: str) -> set[str]:
    base = norm(strip_parens(name))
    variants = {club_key(name), base}
    for alt in COUNTRIES_SR_EN.get(base, "").split("|"):
        if alt:
            variants.add(norm(alt))
    for v in list(variants):
        variants |= _ALIAS_INDEX.get(v, set())
    return {v for v in variants if v}


def _tokens(name: str) -> set[str]:
    return {t for t in name.split() if len(t) >= 3 and t not in GENERIC_TOKENS}


def teams_match(a: str, b: str) -> bool:
    """Da li su `a` (npr. TV, srpski) i `b` (zvanični izvor, engleski) isti tim."""
    va, vb = _name_variants(a), _name_variants(b)
    if va & vb:
        return True
    for x in va:
        for y in vb:
            if _tokens(x) & _tokens(y) or any(p != q and (p.startswith(q) or q.startswith(p))
                                              for p in _tokens(x) for q in _tokens(y)):
                gx = {t for t in x.split() if t in GENERIC_TOKENS and t not in CLUB_NOISE}
                gy = {t for t in y.split() if t in GENERIC_TOKENS and t not in CLUB_NOISE}
                if gx and gy and not (gx & gy):
                    continue  # Manchester City != Manchester United
                return True
            if min(len(x), len(y)) >= 3 and (x.startswith(y + " ") or y.startswith(x + " ")):
                return True
    return False


# --------------------------------------------------------------------------
# Zvanični rasporedi
# --------------------------------------------------------------------------

@dataclass
class Fixture:
    start: datetime
    home: str
    away: str
    competition: str  # ime takmičenja iz config.json
    top4: bool = False  # oba tima u vrhu tabele (samo za ligaška takmičenja)


def _parse_utc(text: str) -> datetime:
    t = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


SKIP_STATUS = ("POSTPONED", "CANCEL", "ABANDON", "SUSPEND")


def parse_espn(data: dict, competition: str) -> list[Fixture]:
    out: list[Fixture] = []
    for ev in (data or {}).get("events") or []:
        try:
            status = json.dumps(ev.get("status") or {}).upper()
            if any(s in status for s in SKIP_STATUS):
                continue
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors") or []
            names = {}
            for c in cs:
                team = c.get("team") or {}
                names[c.get("homeAway")] = team.get("displayName") or team.get("name") or ""
            home, away = names.get("home"), names.get("away")
            if not (home and away) and len(cs) == 2:
                home = (cs[0].get("team") or {}).get("displayName")
                away = (cs[1].get("team") or {}).get("displayName")
            start = _parse_utc(ev.get("date") or comp.get("date"))
        except (TypeError, ValueError, AttributeError, IndexError):
            continue
        if home and away:
            out.append(Fixture(start, home, away, competition))
    return out


def parse_uefa(data, competition: str) -> list[Fixture]:
    matches = data if isinstance(data, list) else (data or {}).get("matches") or []
    out: list[Fixture] = []
    for m in matches:
        try:
            status = str(m.get("status") or "").upper()
            if any(s in status for s in SKIP_STATUS):
                continue
            home = (m.get("homeTeam") or {}).get("internationalName") or (m.get("homeTeam") or {}).get("teamName")
            away = (m.get("awayTeam") or {}).get("internationalName") or (m.get("awayTeam") or {}).get("teamName")
            ko = m.get("kickOffTime") or {}
            stamp = ko.get("dateTime")
            if not stamp:
                continue
            start = _parse_utc(stamp)
        except (TypeError, ValueError, AttributeError):
            continue
        if home and away:
            out.append(Fixture(start, home, away, competition))
    return out


def parse_tsdb(data, competition: str) -> list[Fixture]:
    """TheSportsDB eventsday.php odgovor: {"events": [{strHomeTeam, strAwayTeam, strTimestamp(UTC)}]}"""
    out: list[Fixture] = []
    for e in (data or {}).get("events") or []:
        try:
            status = str(e.get("strStatus") or "").upper()
            if any(x in status for x in SKIP_STATUS):
                continue
            stamp = e.get("strTimestamp") or (f"{e['dateEvent']}T{e['strTime']}" if e.get("strTime") else None)
            if not stamp:
                continue
            start = _parse_utc(stamp)
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
        h, a = e.get("strHomeTeam"), e.get("strAwayTeam")
        if h and a:
            out.append(Fixture(start, h, a, competition))
    return out


def http_json(url: str, retries: int = 3):
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.uefa.com",
                "Referer": "https://www.uefa.com/",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = exc
            time_mod.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last))


def fetch_fixtures(spec: str, competition: str, days: list[date], http=http_json) -> list[Fixture]:
    kind, _, arg = spec.partition(":")
    out: list[Fixture] = []
    for d in days:
        if kind == "espn":
            url = (f"https://site.api.espn.com/apis/site/v2/sports/soccer/{arg}/scoreboard"
                   f"?dates={d:%Y%m%d}&limit=200")
            out += parse_espn(http(url), competition)
        elif kind == "tsdb":
            from urllib.parse import quote
            url = (f"https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d={d.isoformat()}"
                   f"&l={quote(arg)}")
            out += parse_tsdb(http(url), competition)
        elif kind == "uefa":
            url = (f"https://match.uefa.com/v5/matches?competitionId={arg}"
                   f"&fromDate={d.isoformat()}&toDate={d.isoformat()}&limit=100&offset=0&order=ASC")
            out += parse_uefa(http(url), competition)
        else:
            raise ValueError(f"nepoznat izvor: {spec}")
    return out


def parse_espn_standings(data: dict, top_n: int) -> list[str]:
    """Imena prvih `top_n` timova sa ESPN tabele (tolerantno prema obliku odgovora)."""
    entries: list = []
    for ch in (data or {}).get("children") or [data or {}]:
        entries += ((ch.get("standings") or {}).get("entries")) or []
    rows = []
    for pos, e in enumerate(entries):
        team = e.get("team") or {}
        name = team.get("displayName") or team.get("name")
        rank = pos + 1
        for st in e.get("stats") or []:
            if st.get("name") == "rank" and st.get("value") is not None:
                rank = int(st["value"])
        if name:
            rows.append((rank, pos, name))
    rows.sort()
    return [n for _, _, n in rows[:top_n]]


def fetch_top(spec: str, top_n: int, http=http_json) -> list[str]:
    kind, _, arg = spec.partition(":")
    if kind != "espn":
        raise ValueError(f"tabela nije podržana za izvor: {spec}")
    teams = parse_espn_standings(http(f"https://site.api.espn.com/apis/v2/sports/soccer/{arg}/standings"), top_n)
    if len(teams) < top_n:
        raise RuntimeError("tabela je prazna ili nepoznatog oblika")
    return teams


def get_fixtures(cfg: dict, today: date, http=http_json) -> tuple[list[Fixture], dict[str, str]]:
    """Za svako takmičenje probava izvore redom; vraća (utakmice, {takmičenje: greška})."""
    days = [today, today + timedelta(days=1)]
    out: list[Fixture] = []
    failed: dict[str, str] = {}
    for comp in cfg["competitions"]:
        errors = []
        for spec in comp["sources"]:
            try:
                out += fetch_fixtures(spec, comp["name"], days, http)
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{spec}: {exc}")
        else:
            failed[comp["name"] + (" (opciono)" if comp.get("optional") else "")] = "; ".join(errors)
            continue
        n = comp.get("top_n")
        if n:
            try:
                top = None
                for spec in comp["sources"]:
                    try:
                        top = fetch_top(spec, n, http)
                        break
                    except Exception as exc:  # noqa: BLE001
                        last = exc
                if top is None:
                    raise last
                for f in out:
                    if f.competition == comp["name"]:
                        f.top4 = (any(teams_match(f.home, t) for t in top)
                                  and any(teams_match(f.away, t) for t in top))
            except Exception as exc:  # noqa: BLE001
                failed[comp["name"] + " (tabela)"] = str(exc)
    return out, failed


def probe(cfg: dict, today: date, http=http_json, days_ahead: int = 6) -> str:
    """Izveštaj o tome koji izvori rade i šta vraćaju (za dijagnostiku)."""
    days = [today + timedelta(days=i) for i in range(days_ahead + 1)]
    lines = [f"Provera izvora, {datetime.now(TZ):%d.%m.%Y %H:%M}, dani {days[0]} - {days[-1]}", ""]
    for comp in cfg["competitions"]:
        for spec in comp["sources"]:
            try:
                fx = sorted(fetch_fixtures(spec, comp["name"], days, http), key=lambda f: f.start)
                lines.append(f"[OK] {comp['name']} <- {spec}: {len(fx)} utakmica")
                if comp.get("top_n"):
                    try:
                        lines.append(f"      tabela (prva {comp['top_n']}): {', '.join(fetch_top(spec, comp['top_n'], http))}")
                    except Exception as exc:  # noqa: BLE001
                        lines.append(f"      [GREŠKA] tabela: {exc}")
                for f in fx[:4]:
                    lines.append(f"      {f.start:%a %d.%m. %H:%M}  {f.home} - {f.away}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"[GREŠKA] {comp['name']} <- {spec}: {exc}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Filteri (šta nas zanima) i spajanje sa TV rasporedom
# --------------------------------------------------------------------------

def is_serbia(team: str, cfg: dict) -> bool:
    return norm(strip_parens(team)) in {norm(n) for n in cfg["serbia_names"]}


def is_famous(team: str, cfg: dict) -> bool:
    return norm(strip_parens(team)) in {norm(n) for n in cfg["famous_teams"]}


def is_derby(home: str, away: str, country: str, cfg: dict) -> bool:
    h, a = club_key(home), club_key(away)
    for d in cfg["derbies"]:
        if d["country"] != country:
            continue
        for side_a, side_b in d["pairs"]:
            A = {club_key(x) for x in side_a.split("|")}
            B = {club_key(x) for x in side_b.split("|")}
            if (h in A and a in B) or (h in B and a in A):
                return True
    return False


def classify_fixture(fx: Fixture, comp: dict, cfg: dict) -> list[str]:
    """Lista razloga zbog kojih utakmica prolazi filtere (prazna = ne prolazi)."""
    kind = comp["kind"]
    serbia = is_serbia(fx.home, cfg) or is_serbia(fx.away, cfg)
    reasons: list[str] = []
    if kind == "all":
        reasons.append(comp["name"])
    elif kind == "famous":
        if is_famous(fx.home, cfg) or is_famous(fx.away, cfg):
            reasons.append(f"{comp['name']} (poznata reprezentacija)")
    elif kind == "derby":
        if is_derby(fx.home, fx.away, comp["country"], cfg):
            reasons.append(f"Derbi ({comp['country']})")
        if fx.top4:
            reasons.append(f"Međusobni duel prva {comp.get('top_n', 4)} ({comp['country']})")
        return reasons  # Srbija se ovde ne računa
    if serbia:
        reasons.append("Reprezentacija Srbije")
    return reasons


def find_tv(fx: Fixture, tv: list[tuple[Event, FootballMatch]], cfg: dict, keywords: list[str] | None = None):
    """Kanali na kojima se utakmica prenosi i TV imena timova.

    1) termin u blizini početka + poklapanje imena timova;
    2) ako imena ne odgovaraju (srpska transkripcija klubova), termin tačno u
       vreme početka (+-5 min) čije se takmičenje poklapa sa ključnim rečima.
    """
    lo = fx.start - timedelta(minutes=cfg.get("tv_before_minutes", 30))
    hi = fx.start + timedelta(minutes=cfg.get("tv_after_minutes", 10))
    channels: list[str] = []
    names = None
    near = [(ev, fb) for ev, fb in tv if lo <= ev.start <= hi]
    for ev, fb in near:
        if (teams_match(fb.home, fx.home) or teams_match(fb.away, fx.away)
                or teams_match(fb.home, fx.away) or teams_match(fb.away, fx.home)):
            if ev.channel not in channels:
                channels.append(ev.channel)
            names = names or (fb.home, fb.away)
    if channels or not keywords:
        return channels, names
    kws = [norm(k) for k in keywords]
    exact = [(ev, fb) for ev, fb in near
             if abs((ev.start - fx.start).total_seconds()) <= 300
             and any(k in norm(fb.competition) for k in kws)]
    pairs = {(norm(fb.home), norm(fb.away)) for _, fb in exact}
    if len(pairs) == 1:  # nedvosmisleno: samo jedna utakmica takmičenja u tom terminu
        for ev, fb in exact:
            if ev.channel not in channels:
                channels.append(ev.channel)
            names = names or (fb.home, fb.away)
    return channels, names


def build_matches(fixtures: list[Fixture], tv_events: list[Event], cfg: dict, today: date) -> list[dict]:
    comps = {c["name"]: c for c in cfg["competitions"]}
    tv = tv_football(tv_events, cfg)
    seen: dict[str, dict] = {}
    for fx in fixtures:
        if fx.start.astimezone(TZ).date() != today:
            continue
        reasons = classify_fixture(fx, comps[fx.competition], cfg)
        if not reasons:
            continue
        channels, names = find_tv(fx, tv, cfg, comps[fx.competition].get("tv_keywords"))
        if not channels and cfg.get("require_tv_listing"):
            continue
        home, away = names if names else (fx.home, fx.away)
        start_iso = fx.start.isoformat()
        mid = hashlib.sha1(f"{start_iso}|{norm(fx.home)}|{norm(fx.away)}".encode()).hexdigest()[:12]
        seen[mid] = {
            "id": mid,
            "start": start_iso,
            "competition": fx.competition,
            "home": home,
            "away": away,
            "channels": channels,
            "reasons": reasons,
        }
    return sorted(seen.values(), key=lambda m: m["start"])


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
    lines = [m["competition"], f"Počinje u {start:%H:%M} (za {minutes} min)"]
    if m["channels"]:
        lines.append(f"Kanal: {', '.join(m['channels'])}")
    else:
        lines.append("Kanal: nije nađen u TV rasporedu (proveri Arena Sport)")
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
# Preuzimanje TV stranice i stanje
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
    if state.get("version") != STATE_VERSION:
        # stara verzija programa: odbaci spisak utakmica (mogao je da sadrži reprize)
        state["events"] = []
        state["date"] = None
        state["fetched_at"] = None
        state["version"] = STATE_VERSION
        state.pop("seen", None)
    state.setdefault("date", None)
    state.setdefault("fetched_at", None)
    state.setdefault("events", [])
    state.setdefault("notified", {})
    state.setdefault("alerts", {})
    return state


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


def fetch_tv_events(cfg: dict, today: date, fetch_fn, log) -> tuple[list[Event], str | None]:
    events: list[Event] = []
    errors: list[str] = []
    for url in cfg["urls"]:
        try:
            parsed, skipped = parse_schedule(fetch_fn(url), today)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
            continue
        log(f"{url}: pročitano {len(parsed)} termina (preskočeno {skipped}).")
        events += parsed
    return events, ("; ".join(errors) if errors else None)


# --------------------------------------------------------------------------
# Glavni tok
# --------------------------------------------------------------------------

def run(
    cfg: dict,
    *,
    now: datetime,
    fetch_fn=fetch_html,
    http_fn=http_json,
    notify_fn=send_ntfy,
    state_path: Path = STATE_PATH,
    dry_run: bool = False,
    force_refresh: bool = False,
    log=print,
) -> int:
    today = now.date()
    state = load_state(state_path)

    cutoff = (now - timedelta(days=2)).isoformat()
    state["notified"] = {k: v for k, v in state["notified"].items() if v >= cutoff}

    exit_code = 0
    if _needs_refresh(state, today, now, cfg, force_refresh):
        fixtures, failed = get_fixtures(cfg, today, http_fn)
        for name, err in failed.items():
            if name.endswith(("(tabela)", "(opciono)")):
                log(f"Izvor nije dostupan (nije kritično): {name}: {err}")
        # greška pri čitanju tabele ne smatra se greškom rasporeda (samo se ne primenjuje pravilo top 4)
        failed = {k: v for k, v in failed.items() if not k.endswith(("(tabela)", "(opciono)"))}
        all_failed = {c["name"] for c in cfg["competitions"] if not c.get("optional")} <= set(failed)
        for name, err in failed.items():
            log(f"Izvor nije dostupan: {name}: {err}")
        have_cache = state["date"] == today.isoformat()

        if all_failed:
            if not have_cache:
                exit_code = 1
                if not dry_run:
                    _alert_once(state, "sources", today, "TV obaveštenja: greška",
                                "Ne mogu da preuzmem zvanične rasporede utakmica. Pokušaću ponovo.",
                                notify_fn, log)
            else:
                log("Koristim sačuvani današnji raspored.")
        else:
            tv_events, tv_error = fetch_tv_events(cfg, today, fetch_fn, log)
            if tv_error:
                log(f"TV raspored nije dostupan: {tv_error}")
            matches = build_matches(fixtures, tv_events, cfg, today)
            if failed and have_cache:
                keep = [m for m in state["events"] if m["competition"] in failed]
                ids = {m["id"] for m in matches}
                matches += [m for m in keep if m["id"] not in ids]
                matches.sort(key=lambda m: m["start"])
            state["events"] = matches
            state["date"] = today.isoformat()
            # kod delimične greške fetched_at ostaje prazan, pa se pokušava ponovo pri sledećem pokretanju
            state["fetched_at"] = None if (failed or tv_error) else now.isoformat()
            log(f"Utakmica koje prolaze filtere: {len(matches)}.")
            if failed and not dry_run:
                _alert_once(state, "partial", today, "TV obaveštenja: delimična greška",
                            "Nedostupan raspored za: " + ", ".join(failed), notify_fn, log)

        if force_refresh and not dry_run:
            try:
                (state_path.parent).mkdir(parents=True, exist_ok=True)
                (state_path.parent / "probe.txt").write_text(probe(cfg, today, http_fn), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                log(f"Izveštaj o izvorima nije upisan: {exc}")

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
              f"{', '.join(m['channels']) or 'kanal nepoznat'}  ({', '.join(m['reasons'])})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TV sport obaveštenja (fudbal)")
    ap.add_argument("--list", action="store_true", help="prikaži današnje utakmice koje prolaze filtere")
    ap.add_argument("--probe", action="store_true", help="proveri da li svi izvori rade")
    ap.add_argument("--debug-parse", action="store_true", help="prikaži šta je parser pročitao sa TV stranice")
    ap.add_argument("--dry-run", action="store_true", help="ne šalji i ne pamti ništa")
    ap.add_argument("--test-notify", action="store_true", help="pošalji probnu notifikaciju")
    ap.add_argument("--refresh", action="store_true", help="ponovo preuzmi raspored i upiši state/probe.txt")
    ap.add_argument("--html", help="koristi lokalni HTML fajl umesto preuzimanja TV rasporeda")
    ap.add_argument("--now", help="lažno trenutno vreme, npr. 2026-11-24T19:00")
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

    if args.probe:
        print(probe(cfg, now.date()))
        return 0

    if args.debug_parse:
        events, err = fetch_tv_events(cfg, now.date(), fetch_fn, print)
        for ev in events[:15]:
            print(f"  {ev.start:%d.%m. %H:%M}  {ev.channel:<22} {ev.title}")
        print(f"Fudbalskih termina na tvojim kanalima: {len(tv_football(events, cfg))}")
        return 0

    if args.list:
        fixtures, failed = get_fixtures(cfg, now.date())
        for name, err in failed.items():
            print(f"Izvor nije dostupan: {name}: {err}")
        events, _ = fetch_tv_events(cfg, now.date(), fetch_fn, print)
        _print_list(build_matches(fixtures, events, cfg, now.date()))
        return 0

    return run(cfg, now=now, fetch_fn=fetch_fn, dry_run=args.dry_run, force_refresh=args.refresh)


if __name__ == "__main__":
    sys.exit(main())
