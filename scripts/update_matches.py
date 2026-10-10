#!/usr/bin/env python3
"""
PAO Zone – fixture updater.

Builds matches.json from several free public feeds and then refreshes the
SEO blocks in index.html (pre-rendered fixtures + JSON-LD) and sitemap.xml.

Sources
  FC  Super League 1      -> ESPN public API      (league slug gre.1)
  FC  Conference League   -> ESPN public API      (league slug uefa.europa.conf)
  BC  EuroLeague          -> official EuroLeague feed (incrowdsports)
  BC  Greek Basket League -> esake.gr team page (scraped), TheSportsDB fallback

Safety: if a source fails or returns nothing, the matches it produced on the
previous run are kept, so one broken feed never wipes part of the schedule.

TV channels: set automatically only when the source provides them (esake.gr).
For any other match, add it to tv-overrides.json:  { "<match id>": "Channel" }
"""

import datetime as dt
import html
import json
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MATCHES_FILE = ROOT / "matches.json"
OVERRIDES_FILE = ROOT / "tv-overrides.json"
INDEX_FILE = ROOT / "index.html"
SITEMAP_FILE = ROOT / "sitemap.xml"
SITE_URL = "https://www.paozone.gr/"

ATHENS = ZoneInfo("Europe/Athens")
UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
# ESPN's CDN rejects bot-style User-Agents from cloud IPs (e.g. GitHub runners), so send a browser one.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

ESPN_TEAM_ID = "443"  # Panathinaikos on ESPN
ESPN_LEAGUES = [
    ("gre.1", "Super League"),
    ("uefa.europa.conf", "Conference League"),
]
EUROLEAGUE_TEAM = "PAN"
ESAKE_TEAM_ID = "00000001"
SPORTSDB_BC_ID = "135636"


# ---------------------------------------------------------------- helpers
def fetch(url, as_json=True):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if as_json else raw


def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def season_start_year(now=NOW):
    """European seasons start in summer: Aug–Dec -> this year, Jan–Jul -> last year."""
    return now.year if now.month >= 7 else now.year - 1


def is_pao(name):
    n = (name or "").lower()
    return "panathinaikos" in n or "παναθηναϊκ" in n or "παναθηναικ" in n


def make_match(*, mid, team, opponent, competition, home, when, score="", tv="",
               venue="", source, friendly=False, time_tbc=False):
    return {
        "id": mid,
        "team": team,
        "opponent": opponent or "TBD",
        "competition": competition,
        "isFriendly": friendly,
        "home": bool(home),
        "isoDate": iso(when),
        "tv": tv,
        "venue": venue,
        "score": score,
        "source": source,
        **({"timeTBC": True} if time_tbc else {}),
    }


# ---------------------------------------------------------------- ESPN (football)
def espn_score(c):
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("displayValue", s.get("value"))
    if s is None or s == "":
        return None
    try:
        return str(int(float(s)))
    except (TypeError, ValueError):
        return str(s)


def fetch_espn(slug, label):
    base = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams/{ESPN_TEAM_ID}/schedule"
    events = {}
    for url in (base, base + "?fixture=true"):  # results + upcoming fixtures
        try:
            for ev in fetch(url).get("events") or []:
                events[ev["id"]] = ev
        except Exception as e:  # noqa: BLE001
            print(f"  ESPN {slug} {url[-12:]}: {e}")
    out = []
    for ev in events.values():
        comp = (ev.get("competitions") or [{}])[0]
        teams = comp.get("competitors") or []
        home_c = next((c for c in teams if c.get("homeAway") == "home"), None)
        away_c = next((c for c in teams if c.get("homeAway") == "away"), None)
        if not home_c or not away_c:
            continue
        home_name = home_c["team"].get("displayName", "")
        away_name = away_c["team"].get("displayName", "")
        pao_home = is_pao(home_name)
        if not pao_home and not is_pao(away_name):
            continue
        when = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        state = (comp.get("status") or ev.get("status") or {}).get("type", {}).get("state")
        score = ""
        hs, as_ = espn_score(home_c), espn_score(away_c)
        if state in ("post", "in") and hs is not None and as_ is not None:
            score = f"{hs} - {as_}"
        venue = (comp.get("venue") or {}).get("fullName", "")
        tv = ", ".join(b.get("media", {}).get("shortName", "") for b in comp.get("broadcasts") or []
                       if b.get("media", {}).get("shortName"))
        out.append(make_match(
            mid=f"fc-espn-{ev['id']}", team="FC",
            opponent=away_name if pao_home else home_name,
            competition=label, home=pao_home, when=when, score=score,
            tv=tv, venue=venue, source=f"espn-{slug}",
        ))
    return out


# ---------------------------------------------------------------- EuroLeague
def fetch_euroleague():
    season = f"E{season_start_year()}"
    url = (f"https://feeds.incrowdsports.com/provider/euroleague-feeds/v2/competitions/E/"
           f"seasons/{season}/games?teamCode={EUROLEAGUE_TEAM}")
    data = fetch(url)
    games = data.get("data") if isinstance(data, dict) else data
    out = []
    for g in games or []:
        home, away = g.get("home") or {}, g.get("away") or {}
        pao_home = home.get("code") == EUROLEAGUE_TEAM
        opp = away if pao_home else home
        when = dt.datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        score = ""
        if g.get("status") in ("result", "live") and home.get("score") is not None:
            score = f"{home.get('score')} - {away.get('score')}"
        rnd = (g.get("round") or {}).get("round")
        phase = (g.get("phaseType") or {}).get("code", "RS")
        label = "EuroLeague" if phase == "RS" else f"EuroLeague {(g.get('phaseType') or {}).get('name', '')}".strip()
        if rnd and phase == "RS":
            label += f" · Round {rnd}"
        out.append(make_match(
            mid=f"bc-el-{g.get('identifier') or g.get('id')}", team="BC",
            opponent=opp.get("editorialName") or opp.get("name"),
            competition=label, home=pao_home, when=when, score=score,
            tv=", ".join(b.get("name", "") for b in g.get("broadcasters") or [] if b.get("name")),
            venue=(g.get("venue") or {}).get("name", "").title(), source="euroleague",
        ))
    return out


# ---------------------------------------------------------------- GBL (esake.gr)
GR_MONTHS = {"ιαν": 1, "φεβ": 2, "μαρ": 3, "απρ": 4, "μαι": 5, "μαϊ": 5, "ιουν": 6,
             "ιουλ": 7, "αυγ": 8, "σεπ": 9, "οκτ": 10, "νοε": 11, "δεκ": 12}
DATE_RE = re.compile(r"^(?:Δευ|Τρι|Τετ|Πεμ|Πέμ|Παρ|Σαβ|Σάβ|Κυρ)\w*\.?\s+(\d{1,2})\s+([Α-Ωα-ωάέήίόύώϊϋΐΰ]+)\.?"
                     r"(?:\s*-\s*(\d{1,2}):(\d{2}))?", re.I)


def _strip_accents(s):
    return s.translate(str.maketrans("άέήίόύώϊϋΐΰ", "αεηιουωιυιυ"))


# esake.gr writes sponsor names after the club and sometimes mixes Latin look-alike letters.
_LATIN_TO_GREEK = str.maketrans("ABEHIKMNOPTXYZ", "ΑΒΕΗΙΚΜΝΟΡΤΧΥΖ")
GBL_CLUBS = [  # (prefix of the normalised Greek name, display name)
    ("ΠΑΝΑΘΗΝΑΙΚΟΣ", "Panathinaikos"), ("ΟΛΥΜΠΙΑΚΟΣ", "Olympiacos"), ("ΠΑΟΚ", "PAOK"),
    ("ΑΕΚ", "AEK"), ("ΑΡΗΣ", "Aris"), ("ΠΡΟΜΗΘΕΑΣ", "Promitheas Patras"),
    ("ΠΕΡΙΣΤΕΡΙ", "Peristeri"), ("ΚΑΡΔΙΤΣΑ", "Karditsa"), ("ΚΟΛΟΣΣΟΣ", "Kolossos Rhodes"),
    ("ΜΑΡΟΥΣΙ", "Maroussi"), ("ΜΥΚΟΝΟΣ", "Mykonos"), ("ΗΡΑΚΛΗΣ", "Iraklis"),
    ("ΔΟΞΑ", "Doxa Lefkadas"), ("ΛΑΥΡΙΟ", "Lavrio"), ("ΑΠΟΛΛΩΝ", "Apollon Patras"),
    ("VIKOS", "Vikos Falcons"), ("ΒΙΚΟΣ", "Vikos Falcons"),
]


def gbl_club_name(raw):
    raw = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw)).split())
    key = _strip_accents(raw.upper()).translate(_LATIN_TO_GREEK)
    for prefix, name in GBL_CLUBS:
        if key.startswith(prefix.translate(_LATIN_TO_GREEK)):
            return name
    return raw.title()


GAME_BLOCK = '<div class="esake-program-game">'


def parse_esake(page, start_year):
    """Parse the 'games' tab of the Panathinaikos team page on esake.gr (one block per game)."""
    games = []
    for b in page.split(GAME_BLOCK)[1:]:
        b = " ".join(b.split())
        rm = re.search(r"<h5>\s*(\d{1,2})η\s+Αγωνιστική", b)
        info = dict(re.findall(r"/skn/(clock|pointer|tv)\.svg[^>]*>([^<]*)", b))
        cols = re.search(r'esake-program-game-final-score row equal">(.*?)</div>\s*</div>\s*</div>', b)
        if not (rm and info.get("clock") and cols):
            continue
        dm = DATE_RE.match(html.unescape(info["clock"]).strip())
        if not dm:
            continue
        month_key = _strip_accents(dm.group(2).lower())
        mon = GR_MONTHS.get(month_key[:4]) or GR_MONTHS.get(month_key[:3])
        if not mon:
            continue
        # three columns: home (name + logo), score, away (logo + name)
        teams = re.findall(r"esaketeam/([0-9A-Fa-f]{8})/", cols.group(1))
        spans = re.findall(r"<span>(.*?)</span>", cols.group(1))
        if len(teams) < 2 or len(spans) < 3:
            continue
        score_txt = html.unescape(re.sub(r"<[^>]+>", "", spans[1])).replace("\xa0", " ")
        sm = re.search(r"(\d{2,3})\s*-\s*(\d{2,3})", score_txt)
        idg = re.search(r"idgame=([A-Za-z0-9]+)", b)
        year = start_year if mon >= 7 else start_year + 1
        hh, mm = (int(dm.group(3)), int(dm.group(4))) if dm.group(3) else (20, 0)
        games.append({
            "round": int(rm.group(1)),
            "when": dt.datetime(year, mon, int(dm.group(1)), hh, mm, tzinfo=ATHENS),
            "tbc": not dm.group(3),
            "home_id": teams[0].upper(), "away_id": teams[1].upper(),
            "home": gbl_club_name(spans[0]), "away": gbl_club_name(spans[2]),
            "score": f"{sm.group(1)} - {sm.group(2)}" if sm else "",
            "venue": html.unescape(info.get("pointer", "")).split(" - ")[0].strip(),
            "tv": html.unescape(info.get("tv", "")).strip(),
            "idgame": idg.group(1) if idg else None,
        })

    # The page also lists the Super Cup as a second "Round 1": a repeated round number on an
    # earlier date than the league game with that number is the Super Cup.
    latest = {}
    for g in games:
        latest[g["round"]] = max(latest.get(g["round"], g["when"]), g["when"])

    out, seen = [], set()
    for g in sorted(games, key=lambda x: x["when"]):
        pao_home = g["home_id"] == ESAKE_TEAM_ID
        if not pao_home and g["away_id"] != ESAKE_TEAM_ID:
            continue
        key = g["idgame"] or f"{g['when'].date()}-{g['round']}"
        if key in seen:
            continue
        seen.add(key)
        comp = ("Greek Super Cup" if g["when"] < latest[g["round"]]
                else f"Greek Basket League · Round {g['round']}")
        out.append(make_match(
            mid=f"bc-gbl-{key}", team="BC", opponent=g["away"] if pao_home else g["home"],
            competition=comp, home=pao_home, when=g["when"].astimezone(UTC),
            score=g["score"], tv=g["tv"], venue=g["venue"], source="gbl", time_tbc=g["tbc"],
        ))
    return out


def fetch_gbl():
    url = f"https://www.esake.gr/el/action/EsaketeamView?idteam={ESAKE_TEAM_ID}&mode=3"
    games = parse_esake(fetch(url, as_json=False), season_start_year())
    if len(games) >= 3:
        return games
    print(f"  esake.gr parse returned {len(games)} games – trying TheSportsDB fallback")
    out = []
    for ep in ("eventsnext", "eventslast"):
        data = fetch(f"https://www.thesportsdb.com/api/v1/json/3/{ep}.php?id={SPORTSDB_BC_ID}")
        for ev in data.get("events") or data.get("results") or []:
            if "Greek" not in (ev.get("strLeague") or ""):
                continue
            pao_home = is_pao(ev.get("strHomeTeam"))
            ts = ev.get("strTimestamp") or f"{ev.get('dateEvent')}T{ev.get('strTime') or '18:00:00'}"
            when = dt.datetime.fromisoformat(ts[:19]).replace(tzinfo=UTC)
            score = ""
            if ev.get("intHomeScore") not in (None, ""):
                score = f"{ev['intHomeScore']} - {ev['intAwayScore']}"
            out.append(make_match(
                mid=f"bc-gbl-tsdb-{ev['idEvent']}", team="BC",
                opponent=ev.get("strAwayTeam") if pao_home else ev.get("strHomeTeam"),
                competition="Greek Basket League", home=pao_home, when=when,
                score=score, venue=ev.get("strVenue") or "", source="gbl",
            ))
    return out


# ---------------------------------------------------------------- SEO output
def esc(s):
    return html.escape(str(s or ""), quote=True)


def card_html(m):
    when = dt.datetime.fromisoformat(m["isoDate"].replace("Z", "+00:00")).astimezone(ATHENS)
    pao = "Παναθηναϊκός" + (" BC" if m["team"] == "BC" else " FC")
    home, away = (pao, m["opponent"]) if m["home"] else (m["opponent"], pao)
    return (
        f'<article class="match-card"><div><div class="card-top">'
        f'<span class="competition-badge">{esc(m["competition"])}</span>'
        f'<span class="team-type-tag tag-{m["team"].lower()}">{m["team"]}</span></div>'
        f'<h3 class="teams-container" style="font-size:15px">{esc(home)} – {esc(away)}</h3>'
        f'<div class="tv-badge">📺 {esc(m["tv"] or "TBA")}</div></div>'
        f'<div class="card-bottom"><span>{esc(m.get("venue"))}</span>'
        f'<time class="match-time-local" datetime="{m["isoDate"]}">'
        f'{when.strftime("%d/%m/%Y") if m.get("timeTBC") else when.strftime("%d/%m/%Y %H:%M")}'
        f'</time></div></article>'
    )


def event_ld(m):
    pao = {"@type": "SportsTeam", "name": "Panathinaikos " + ("BC" if m["team"] == "BC" else "FC"),
           "sport": "Basketball" if m["team"] == "BC" else "Soccer"}
    opp = {"@type": "SportsTeam", "name": m["opponent"]}
    home, away = (pao, opp) if m["home"] else (opp, pao)
    ev = {
        "@type": "SportsEvent",
        "name": f"{home['name']} vs {away['name']}",
        "startDate": m["isoDate"][:10] if m.get("timeTBC") else m["isoDate"],
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "sport": pao["sport"],
        "description": f"{m['competition']}: {home['name']} – {away['name']}",
        "homeTeam": home,
        "awayTeam": away,
        "competitor": [home, away],
        "url": SITE_URL,
    }
    if m.get("venue"):
        ev["location"] = {"@type": "Place", "name": m["venue"]}
    return ev


def replace_block(text, name, content):
    pat = re.compile(rf"(<!--{name}:START-->).*?(<!--{name}:END-->)", re.S)
    if not pat.search(text):
        print(f"  marker {name} not found in index.html")
        return text
    return pat.sub(lambda m: m.group(1) + content + m.group(2), text)


def update_index(matches, write_sitemap=True):
    upcoming = sorted((m for m in matches if m["isoDate"] >= iso(NOW)), key=lambda m: m["isoDate"])
    text = INDEX_FILE.read_text(encoding="utf-8")
    text = replace_block(text, "PRERENDER", "\n" + "\n".join(card_html(m) for m in upcoming[:12]) + "\n")
    ld = {"@context": "https://schema.org", "@graph": [event_ld(m) for m in upcoming[:20]]}
    ld_json = json.dumps(ld, ensure_ascii=False, indent=1).replace("</", "<\\/")
    text = replace_block(text, "EVENTS_JSONLD",
                         f'\n<script type="application/ld+json">\n{ld_json}\n</script>\n')
    INDEX_FILE.write_text(text, encoding="utf-8")
    if not write_sitemap:
        return
    SITEMAP_FILE.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE_URL}</loc><lastmod>{NOW.date()}</lastmod>"
        "<changefreq>daily</changefreq><priority>1.0</priority></url>\n</urlset>\n",
        encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    try:
        previous = json.loads(MATCHES_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        previous = []
    try:
        overrides = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        overrides = {}

    sources = [(f"espn-{slug}", (lambda s=slug, l=label: fetch_espn(s, l))) for slug, label in ESPN_LEAGUES]
    sources += [("euroleague", fetch_euroleague), ("gbl", fetch_gbl)]

    matches = []
    for name, fn in sources:
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: FAILED ({e})")
            got = []
        if got:
            print(f"  {name}: {len(got)} matches")
            matches += got
        else:
            kept = [m for m in previous if m.get("source") == name]
            print(f"  {name}: no data – keeping {len(kept)} from last run")
            matches += kept

    for m in matches:
        if overrides.get(m["id"]):
            m["tv"] = overrides[m["id"]]

    matches.sort(key=lambda m: m["isoDate"])
    if not matches:
        print("No matches from any source – leaving files untouched.")
        return 1
    new_json = json.dumps(matches, ensure_ascii=False, indent=2) + "\n"
    changed = not MATCHES_FILE.exists() or MATCHES_FILE.read_text(encoding="utf-8") != new_json
    MATCHES_FILE.write_text(new_json, encoding="utf-8")
    update_index(matches, write_sitemap=changed or not SITEMAP_FILE.exists())
    print(f"Wrote {len(matches)} matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
