"""
Step 8: Live match dashboard, phase 1.
Competitions: top 5 leagues, European cups, domestic cups, second tiers, other leagues,
national teams and friendlies (see COMPS).

For every match in the next 3 days:
  - fair probabilities from the sharpest price available (Pinnacle first, else market average)
  - confidence label
  - injured / suspended players
  - official lineups (released about 1 hour before kick-off)
  - price movement since the match first appeared, with an alert on big moves
  - highlights, filters, and a record of every prediction vs the real result (daily hit rate)
Builds dashboard.html (open it in any browser, works on phone too).
Needs API-Football Pro (current season). Run:  python step8_dashboard.py
"""
import os
import sys
import json
import html
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://v3.football.api-sports.io"
KEY_FILE = "api_key.txt"
SEASON = 2026
DAYS_AHEAD = 3
AMMAN = timezone(timedelta(hours=3))          # Jordan is UTC+3 all year
PINNACLE_ID = 4
LINEUP_WINDOW_H = 2          # check lineups for matches starting within 2 hours
BIG_MOVE = 0.05              # 5 percentage points = big move
HISTORY = os.path.join("data", "price_history.json")
CACHE = os.path.join("data", "match_cache.json")
RESULTS = os.path.join("data", "results_log.json")
ALERTS = os.path.join("data", "alerts_state.json")
TG_CFG = "telegram.txt"
NTFY_CFG = "ntfy.txt"
PRE_ALERT_MIN = 75           # pre-match alert when kick-off is within 75 minutes (checks run every 15 min)
FINISHED = ("FT", "AET", "PEN")
LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "INT", "LIVE", "SUSP")
NEAR_H = 24                  # matches within 24h: prices refreshed every run (hourly)
FAR_REFRESH_H = 6            # matches further away: refreshed every 6 hours

# id: (Arabic name, English name, type)   type decides order and confidence
ALL_COMPS = {
    39: ("الدوري الإنجليزي", "Premier League", "top"), 140: ("الدوري الإسباني", "La Liga", "top"),
    135: ("الدوري الإيطالي", "Serie A", "top"), 78: ("الدوري الألماني", "Bundesliga", "top"),
    61: ("الدوري الفرنسي", "Ligue 1", "top"),
    2: ("دوري أبطال أوروبا", "Champions League", "euro"), 3: ("الدوري الأوروبي", "Europa League", "euro"),
    848: ("دوري المؤتمر الأوروبي", "Conference League", "euro"),
    45: ("كأس الاتحاد الإنجليزي", "FA Cup", "cup"), 48: ("كأس الرابطة الإنجليزية", "League Cup", "cup"),
    143: ("كأس ملك إسبانيا", "Copa del Rey", "cup"), 137: ("كأس إيطاليا", "Coppa Italia", "cup"),
    81: ("كأس ألمانيا", "DFB Pokal", "cup"), 66: ("كأس فرنسا", "Coupe de France", "cup"),
    40: ("الدرجة الثانية الإنجليزية", "Championship", "second"), 141: ("الدرجة الثانية الإسبانية", "Segunda", "second"),
    136: ("الدرجة الثانية الإيطالية", "Serie B", "second"), 79: ("الدرجة الثانية الألمانية", "2. Bundesliga", "second"),
    62: ("الدرجة الثانية الفرنسية", "Ligue 2", "second"),
    88: ("الدوري الهولندي", "Eredivisie", "other"), 94: ("الدوري البرتغالي", "Primeira Liga", "other"),
    203: ("الدوري التركي", "Super Lig", "other"), 307: ("الدوري السعودي", "Saudi Pro League", "other"),
    5: ("دوري الأمم الأوروبية", "Nations League", "national"), 960: ("تصفيات أمم أوروبا", "Euro Qualifiers", "national"),
    32: ("تصفيات كأس العالم: أوروبا", "WC Qualifiers Europe", "national"),
    10: ("مباريات ودية دولية", "International Friendlies", "friendly"),
    667: ("مباريات ودية للأندية", "Club Friendlies", "friendly"),
}
COMPS = {}                   # filled at start of each run by build_comps()
UNFINISHED_TODAY = []        # filled by collect(): today's matches still live or not started
TOP5 = (39, 140, 135, 78, 61)
EURO_CUPS = (2, 3, 848)
LEAGUES_CACHE = os.path.join("data", "leagues_cache.json")

# Arab top divisions: API country name -> (accepted league names in priority order, Arabic label)
ARAB = {
    "Saudi-Arabia": (["Pro League"], "الدوري السعودي"),
    "Egypt": (["Premier League"], "الدوري المصري"),
    "Morocco": (["Botola Pro"], "الدوري المغربي"),
    "Algeria": (["Ligue 1"], "الدوري الجزائري"),
    "Tunisia": (["Ligue 1", "Ligue Professionnelle 1"], "الدوري التونسي"),
    "Qatar": (["Stars League", "QSL"], "الدوري القطري"),
    "United-Arab-Emirates": (["Pro League", "UAE Pro League"], "الدوري الإماراتي"),
    "Jordan": (["League", "Pro League", "Premier League"], "الدوري الأردني"),
    "Kuwait": (["Premier League"], "الدوري الكويتي"),
    "Iraq": (["Iraqi League", "Stars League", "Premier League"], "الدوري العراقي"),
    "Bahrain": (["Premier League"], "الدوري البحريني"),
    "Oman": (["Professional League"], "الدوري العُماني"),
    "Lebanon": (["Premier League"], "الدوري اللبناني"),
    "Syria": (["Premier League"], "الدوري السوري"),
    "Libya": (["Premier League"], "الدوري الليبي"),
}


def build_comps(key, cfg):
    """Active competitions: top 5 first divisions, plus optional European cups and Arab top divisions."""
    comps = {lid: ALL_COMPS[lid] for lid in TOP5}
    if cfg.get("include_european_cups"):
        comps.update({lid: ALL_COMPS[lid] for lid in EURO_CUPS})
    if cfg.get("include_arab_leagues"):
        cache = {}
        if os.path.exists(LEAGUES_CACHE):
            try:
                cache = json.load(open(LEAGUES_CACHE, encoding="utf-8"))
            except (ValueError, OSError):
                cache = {}
        fresh = cache.get("saved") and (datetime.now(timezone.utc) -
                                        datetime.fromisoformat(cache["saved"])).days < 30
        if not fresh:
            found = {}
            for country, (names, label) in ARAB.items():
                try:
                    res = api("leagues", {"country": country, "type": "league", "current": "true"}, key)
                except RuntimeError:
                    continue
                by_name = {x["league"]["name"]: x["league"]["id"] for x in res}
                for n in names:
                    if n in by_name:
                        found[str(by_name[n])] = [label, f"{country} {n}"]
                        break
                else:
                    print(f"   Arab league not found for {country}: {sorted(by_name)[:6]}")
            cache = {"saved": datetime.now(timezone.utc).isoformat(), "leagues": found}
            os.makedirs("data", exist_ok=True)
            json.dump(cache, open(LEAGUES_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"   Arab leagues found: {len(found)}")
        for lid, (label, en) in cache.get("leagues", {}).items():
            comps[int(lid)] = (label, en, "arab")
    return comps


ORDER = ["top", "euro", "arab", "national", "cup", "second", "other", "friendly"]

calls = 0


def get_key():
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key and os.path.exists(KEY_FILE):
        key = open(KEY_FILE, encoding="utf-8").read().strip()
    if not key:
        raise RuntimeError("No key found in api_key.txt")
    return key


def api(endpoint, params, key):
    global calls
    r = requests.get(f"{BASE_URL}/{endpoint}", headers={"x-apisports-key": key}, params=params, timeout=30)
    calls += 1
    if r.status_code != 200:
        raise RuntimeError(f"Server said {r.status_code} on /{endpoint}: {r.text[:300]}")
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"API error on /{endpoint}: {data['errors']}")
    return data["response"]


def fair_probs(odds_resp):
    """Returns (probabilities [H, D, A], source text) or (None, None)."""
    books = odds_resp[0]["bookmakers"] if odds_resp else []
    sets = []
    for b in books:
        for bet in b.get("bets", []):
            if bet.get("id") != 1:                      # 1 = Match Winner
                continue
            v = {x["value"]: x["odd"] for x in bet.get("values", [])}
            try:
                o = [float(v["Home"]), float(v["Draw"]), float(v["Away"])]
            except (KeyError, ValueError, TypeError):
                continue
            if min(o) <= 1:
                continue
            inv = [1 / x for x in o]
            s = sum(inv)
            sets.append((b["id"], [x / s for x in inv]))
    if not sets:
        return None, None
    for bid, p in sets:
        if bid == PINNACLE_ID:
            return p, "Pinnacle"
    avg = [sum(p[k] for _, p in sets) / len(sets) for k in range(3)]
    return avg, f"متوسط {len(sets)} شركات"


VALUE_EDGE = 0.02            # best price at least 2% above fair = worth flagging
MAX_EDGE = 0.15              # above 15% is almost always a stale or wrong price, ignore


def _devig(o):
    inv = [1 / x for x in o]
    s = sum(inv)
    return [x / s for x in inv]


def parse_odds(odds_resp):
    """Fair probabilities for match result, over/under 2.5 goals and both teams to score,
    plus the best price available for each match result outcome."""
    books = odds_resp[0]["bookmakers"] if odds_resp else []
    per = {1: [], 5: [], 8: []}       # 1 = match winner, 5 = goals over/under, 8 = both teams score
    for b in books:
        for bet in b.get("bets", []):
            bid = bet.get("id")
            if bid not in per:
                continue
            v = {str(x["value"]): x["odd"] for x in bet.get("values", [])}
            try:
                if bid == 1:
                    o = [float(v["Home"]), float(v["Draw"]), float(v["Away"])]
                elif bid == 5:
                    o = [float(v["Over 2.5"]), float(v["Under 2.5"])]
                else:
                    o = [float(v["Yes"]), float(v["No"])]
            except (KeyError, ValueError, TypeError):
                continue
            if min(o) > 1:
                per[bid].append((b["id"], b.get("name", "?"), o))

    def fair(lst):
        if not lst:
            return None, None
        for bid, _, o in lst:
            if bid == PINNACLE_ID:
                return _devig(o), "Pinnacle"
        ps = [_devig(o) for _, _, o in lst]
        n = len(o)
        return [sum(p[k] for p in ps) / len(ps) for k in range(n)], f"متوسط {len(ps)} شركات"

    p, src = fair(per[1])
    ou, _ = fair(per[5])
    btts, _ = fair(per[8])
    best = None
    if p:
        best = []
        for k in range(3):
            bid, name, o = max(per[1], key=lambda t: t[2][k])
            best.append({"book": name, "odd": o[k], "edge": o[k] * p[k] - 1})
    return {"p": p, "source": src, "ou": ou, "btts": btts, "best": best, "nbooks": len(per[1])}


def has_value(m):
    return any(VALUE_EDGE <= b["edge"] <= MAX_EDGE for b in (m.get("best") or []))


def best_html(m):
    """Highest price on offer for each outcome, compared with the fair price."""
    if not m.get("best"):
        return ""
    e = html.escape
    names = [m["home"], "تعادل", m["away"]]
    cells = ""
    for k, b in enumerate(m["best"]):
        edge = b["edge"]
        if VALUE_EDGE <= edge <= MAX_EDGE:
            tag = f'<em class="up">+{edge:.0%} فوق العادل</em>'
        elif edge > MAX_EDGE:
            tag = '<em class="odd">سعر مشكوك فيه</em>'
        else:
            tag = ""
        cells += (f'<span><b>{e(names[k])}</b> {b["odd"]:.2f} <small>({american(1 / b["odd"])})</small> '
                  f'<small>{e(b["book"])}</small> {tag}</span>')
    return (f'<div class="fair best"><span class="fl">أعلى سعر متاح بين {m.get("nbooks", 0)} شركات</span>'
            f'{cells}</div>')


def extra_html(m):
    """Over/under 2.5 goals and both teams to score, as fair probabilities and prices."""
    parts = []
    if m.get("ou"):
        o, u = m["ou"]
        parts.append(f'<span><b>أكثر من 2.5 هدف</b> {o:.0%} <small>({1 / o:.2f})</small></span>'
                     f'<span><b>أقل من 2.5</b> {u:.0%} <small>({1 / u:.2f})</small></span>')
    if m.get("btts"):
        y, n = m["btts"]
        parts.append(f'<span><b>الفريقين بيسجلوا</b> {y:.0%} <small>({1 / y:.2f})</small></span>'
                     f'<span><b>لا</b> {n:.0%} <small>({1 / n:.2f})</small></span>')
    if not parts:
        return ""
    return '<div class="fair extra"><span class="fl">أسواق ثانية</span>' + "".join(parts) + "</div>"


def american(prob):
    """Fair price in American format, e.g. 0.70 -> -233, 0.20 -> +400."""
    d = 1 / prob
    if d >= 2:
        return f"+{round((d - 1) * 100)}"
    return f"-{round(100 / (d - 1))}"


def confidence(p, source, mtype=""):
    if mtype == "friendly":
        return "ودية: ثقة منخفضة", "lo"
    top = max(p)
    sharp = source == "Pinnacle"
    if top >= 0.60:
        label, cls = "صورة واضحة", "hi"
    elif top >= 0.45:
        label, cls = "صورة متوسطة", "mid"
    else:
        label, cls = "مباراة مفتوحة", "lo"
    if not sharp and cls == "hi":
        label, cls = "صورة متوسطة", "mid"
    return label, cls


def collect(key):
    cache = {}
    if os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE, encoding="utf-8"))
        except (ValueError, OSError):
            cache = {}
    now = datetime.now(timezone.utc)
    today = datetime.now(AMMAN).date()
    fixtures = []
    log = load_results()
    new_results = 0
    for i in range(-1, DAYS_AHEAD + 1):                  # one request per day (yesterday to +3 days)
        day = (today + timedelta(days=i)).isoformat()
        fixtures += [f for f in api("fixtures", {"date": day, "timezone": "Asia/Amman"}, key)
                     if f["league"]["id"] in COMPS]
    matches, new_cache, refreshed = [], {}, 0
    UNFINISHED_TODAY.clear()
    today_s = today.isoformat()
    for f in fixtures:
        st = f["fixture"]["status"]["short"]
        fid = str(f["fixture"]["id"])
        ko_day = datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00")).astimezone(AMMAN).date().isoformat()
        if ko_day == today_s and (st in LIVE or st in ("NS", "TBD")):
            UNFINISHED_TODAY.append(fid)
        if st in FINISHED:
            c = cache.get(fid)
            if fid not in log and c and c.get("p"):
                log[fid] = make_record(f, c)
                new_results += 1
            continue
        if st in LIVE:
            if fid in cache:
                new_cache[fid] = cache[fid]           # keep last pre-match price until it finishes
            continue
        if st not in ("NS", "TBD"):
            continue
        ko = datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00"))
        hours_to_ko = (ko - now).total_seconds() / 3600
        c = cache.get(fid)
        age_h = (now - datetime.fromisoformat(c["fetched"])).total_seconds() / 3600 if c else 99
        if c is None or (hours_to_ko <= NEAR_H and age_h >= 0.9) or age_h >= FAR_REFRESH_H:
            od = parse_odds(api("odds", {"fixture": fid}, key))
            out = {"home": [], "away": []}
            for i in api("injuries", {"fixture": fid}, key):
                side = "home" if i["team"]["id"] == f["teams"]["home"]["id"] else "away"
                out[side].append(i["player"]["name"])
            c = {**od, "out": out, "fetched": now.isoformat(), "lineups": (c or {}).get("lineups")}
            refreshed += 1
        if not c.get("lineups") and hours_to_ko <= LINEUP_WINDOW_H:
            c["lineups"] = get_lineups(api("fixtures/lineups", {"fixture": fid}, key), f)
        new_cache[fid] = c
        lid = f["league"]["id"]
        matches.append({"league": COMPS[lid][0], "type": COMPS[lid][2], "en": COMPS[lid][1],
                        "id": int(fid), "kickoff": f["fixture"]["date"],
                        "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
                        "p": c["p"], "source": c["source"], "out": c["out"], "lineups": c.get("lineups"),
                        "ou": c.get("ou"), "btts": c.get("btts"), "best": c.get("best"),
                        "nbooks": c.get("nbooks", 0)})
    os.makedirs("data", exist_ok=True)
    json.dump(new_cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    counts = {}
    for m in matches:
        counts[m["en"]] = counts.get(m["en"], 0) + 1
    for name, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"   {name}: {n} matches")
    print(f"   Prices refreshed this run: {refreshed}")
    save_results(log)
    print(f"   Finished matches added to the record: {new_results} (total {len(log)})")
    return matches, log


def load_results():
    if os.path.exists(RESULTS):
        try:
            return json.load(open(RESULTS, encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_results(log):
    os.makedirs("data", exist_ok=True)
    json.dump(log, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False)


def make_record(f, c):
    """Final pre-match prediction vs the 90-minute result."""
    ft = (f.get("score") or {}).get("fulltime") or {}
    hg = ft.get("home") if ft.get("home") is not None else f["goals"]["home"]
    ag = ft.get("away") if ft.get("away") is not None else f["goals"]["away"]
    res = 0 if hg > ag else (1 if hg == ag else 2)
    pick = max(range(3), key=lambda k: c["p"][k])
    lid = f["league"]["id"]
    label, cls = confidence(c["p"], c["source"], COMPS[lid][2])
    day = datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00")).astimezone(AMMAN).date().isoformat()
    return {"day": day, "comp": COMPS[lid][0], "type": COMPS[lid][2],
            "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
            "p": c["p"], "pick": pick, "res": res, "score": f"{hg}-{ag}",
            "ok": pick == res, "conf": cls}


def get_lineups(resp, f):
    """Returns {"home": {...}, "away": {...}} or None if not released yet."""
    if not resp:
        return None
    out = {}
    for t in resp:
        side = "home" if t["team"]["id"] == f["teams"]["home"]["id"] else "away"
        out[side] = {"formation": t.get("formation") or "",
                     "xi": [x["player"]["name"] for x in t.get("startXI", [])]}
    return out if out.get("home") and out.get("away") and out["home"]["xi"] else None


def track_prices(matches):
    """Remembers the first and latest probabilities per match; adds m['move']."""
    hist = {}
    if os.path.exists(HISTORY):
        try:
            hist = json.load(open(HISTORY, encoding="utf-8"))
        except (ValueError, OSError):
            hist = {}
    now = datetime.now(AMMAN).strftime("%d/%m %H:%M")
    for m in matches:
        k = str(m["id"])
        m["move"] = None
        if not m["p"]:
            continue
        if k not in hist:
            hist[k] = {"first": m["p"], "first_at": now, "lineup_at": None}
        else:
            m["move"] = {"diff": [m["p"][i] - hist[k]["first"][i] for i in range(3)],
                         "since": hist[k]["first_at"]}
        if m.get("lineups") and not hist[k].get("lineup_p"):
            hist[k]["lineup_p"] = m["p"]          # price at the moment lineups appeared
        hist[k]["last"] = m["p"]
    if len(hist) > 2000:                           # keep the file small: drop finished matches
        live = {str(m["id"]) for m in matches}
        hist = {k: v for k, v in hist.items() if k in live}
    os.makedirs("data", exist_ok=True)
    json.dump(hist, open(HISTORY, "w", encoding="utf-8"))


def fmt_time(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(AMMAN)
    days = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    return dt, f"{days[dt.weekday()]} {dt.day}/{dt.month}", dt.strftime("%H:%M")


def card(m):
    e = html.escape
    _, _, t = fmt_time(m["kickoff"])
    conf_cls, big_flag = "none", 0
    if m["p"]:
        h, d, a = (round(x * 100) for x in m["p"])
        a = 100 - h - d
        label, cls = confidence(m["p"], m["source"], m.get("type", ""))
        conf_cls = cls
        fair_h, fair_d, fair_a = (f"{1 / x:.2f}" for x in m["p"])
        amer_h, amer_d, amer_a = (american(x) for x in m["p"])
        body = f"""<div class="bar"><span class="w" style="flex:{h}">{h}%</span><span class="d" style="flex:{d}">{d}%</span><span class="l" style="flex:{a}">{a}%</span></div>
<div class="legend"><span>فوز {e(m['home'])}</span><span>تعادل</span><span>فوز {e(m['away'])}</span></div>
<div class="meta"><span class="chip {cls}">{label}</span><span>المصدر: {e(m['source'])}</span></div>
<div class="fair"><span class="fl">السعر العادل</span>
<span><b>{e(m['home'])}</b> {fair_h} <small>({amer_h})</small></span>
<span><b>تعادل</b> {fair_d} <small>({amer_d})</small></span>
<span><b>{e(m['away'])}</b> {fair_a} <small>({amer_a})</small></span></div>""" + best_html(m) + extra_html(m)
    else:
        body = '<div class="meta"><span class="chip lo">لا توجد أسعار بعد</span></div>'
    move = ""
    if m.get("move"):
        dh, _, da = m["move"]["diff"]
        big = max(abs(dh), abs(da)) >= BIG_MOVE
        if max(abs(dh), abs(da)) >= 0.01:
            who = m["home"] if dh > 0 else m["away"]
            amt = round(max(abs(dh), abs(da)) * 100)
            mcls = "alert" if big else "move"
            big_flag = 1 if big else 0
            icon = "⚠️ " if big else ""
            move = (f'<div class="{mcls}">{icon}السعر تحرك لصالح {e(who)} بـ {amt} نقاط '
                    f'منذ {e(m["move"]["since"])}' + (" (حركة كبيرة، غالباً في خبر جديد)" if big else "") + "</div>")
    lu = ""
    if m.get("lineups"):
        for side in ("home", "away"):
            team = m["home"] if side == "home" else m["away"]
            x = m["lineups"][side]
            lu += (f'<div class="lu"><b>تشكيلة {e(team)} الرسمية'
                   + (f" ({e(x['formation'])})" if x["formation"] else "") + ":</b> "
                   + "، ".join(e(n) for n in x["xi"]) + "</div>")
    elif m["p"]:
        lu = '<div class="lu wait">التشكيلة الرسمية تنزل قبل المباراة بساعة تقريباً</div>' if _soon(m) else ""
    outs = ""
    for side in ("home", "away"):
        if m["out"][side]:
            team = m["home"] if side == "home" else m["away"]
            names = "، ".join(e(n) for n in m["out"][side][:8])
            outs += f'<div class="out"><b>غيابات {e(team)}:</b> {names}</div>'
    return f"""<article data-conf="{conf_cls}" data-move="{big_flag}" data-value="{1 if has_value(m) else 0}"><div class="top"><span class="teams">{e(m['home'])} <small>ضد</small> {e(m['away'])}</span><span class="time">{t}</span></div>{body}{move}{lu}{outs}</article>"""


def _soon(m):
    ko = datetime.fromisoformat(m["kickoff"].replace("Z", "+00:00"))
    return ko - datetime.now(timezone.utc) <= timedelta(hours=LINEUP_WINDOW_H)


PICK_TXT = ["فوز {h}", "تعادل", "فوز {a}"]


def results_html(log):
    e = html.escape
    recs = sorted(log.values(), key=lambda r: r["day"], reverse=True)
    if not recs:
        return ("<p class='empty'>لا توجد نتائج بعد. أول ما تنتهي مباراة كانت على اللوحة قبل بدايتها، "
                "بتنضاف هون تلقائياً مع توقعنا ونتيجتها.</p>")

    def summary(rs):
        n = len(rs)
        ok = sum(1 for r in rs if r["ok"])
        exp = sum(max(r["p"]) for r in rs) / n
        return n, ok, ok / n, exp

    n, ok, pct, exp = summary(recs)
    out = f"""<div class="kpis">
<div><b>{n}</b><span>مباراة مسجلة</span></div>
<div><b>{ok}</b><span>توقع صحيح</span></div>
<div><b>{pct:.0%}</b><span>نسبة النجاح الفعلية</span></div>
<div><b>{exp:.0%}</b><span>النسبة المتوقعة</span></div></div>
<p class="hint">النسبة المتوقعة هي متوسط احتمال الخيار الأرجح. إذا كانت قريبة من النسبة الفعلية، معناها إنه الأرقام صادقة.
والحكم الحقيقي بيحتاج مئات المباريات، فلا تحكم من يوم أو يومين.</p>"""

    out += "<h3>حسب درجة الثقة</h3><table><tr><th>الدرجة</th><th>مباريات</th><th>صح</th><th>الفعلية</th><th>المتوقعة</th></tr>"
    for cls, name in (("hi", "صورة واضحة"), ("mid", "صورة متوسطة"), ("lo", "مفتوحة أو ودية")):
        rs = [r for r in recs if r["conf"] == cls]
        if rs:
            a_, b_, c_, d_ = summary(rs)
            out += f"<tr><td>{name}</td><td>{a_}</td><td>{b_}</td><td>{c_:.0%}</td><td>{d_:.0%}</td></tr>"
    out += "</table>"

    days = {}
    for r in recs:
        days.setdefault(r["day"], []).append(r)
    out += "<h3>حصيلة كل يوم</h3><table><tr><th>اليوم</th><th>مباريات</th><th>صح</th><th>الفعلية</th><th>المتوقعة</th></tr>"
    for day, rs in list(days.items())[:14]:
        a_, b_, c_, d_ = summary(rs)
        dd = datetime.fromisoformat(day)
        out += f"<tr><td>{dd.day}/{dd.month}</td><td>{a_}</td><td>{b_}</td><td>{c_:.0%}</td><td>{d_:.0%}</td></tr>"
    out += "</table>"

    for day, rs in list(days.items())[:3]:
        dd = datetime.fromisoformat(day)
        out += f"<h3>نتائج {dd.day}/{dd.month}</h3>"
        for r in rs:
            pick = PICK_TXT[r["pick"]].format(h=r["home"], a=r["away"])
            icon = "✅" if r["ok"] else "❌"
            out += (f'<div class="res {"ok" if r["ok"] else "no"}"><span class="ic">{icon}</span>'
                    f'<span class="rt"><b>{e(r["home"])} {e(r["score"])} {e(r["away"])}</b>'
                    f'<small>{e(r["comp"])} · توقعنا: {e(pick)} ({round(max(r["p"]) * 100)}%)</small></span></div>')
    return out


def highlights(matches):
    e = html.escape
    now = datetime.now(timezone.utc)
    soon = [m for m in matches if m["p"] and
            datetime.fromisoformat(m["kickoff"].replace("Z", "+00:00")) - now <= timedelta(hours=24)]
    moves = [m for m in soon if m.get("move") and max(abs(m["move"]["diff"][0]), abs(m["move"]["diff"][2])) >= BIG_MOVE]
    clear = [m for m in soon if confidence(m["p"], m["source"], m.get("type", ""))[1] == "hi"]
    clear.sort(key=lambda m: -max(m["p"]))
    rows = ""
    for m in moves[:5]:
        _, _, t = fmt_time(m["kickoff"])
        rows += f'<li>⚠️ <b>{e(m["home"])} ضد {e(m["away"])}</b> <small>{t}، حركة سعر كبيرة</small></li>'
    for m in clear[:6]:
        _, _, t = fmt_time(m["kickoff"])
        k = max(range(3), key=lambda i: m["p"][i])
        pick = PICK_TXT[k].format(h=m["home"], a=m["away"])
        rows += f'<li>🟢 <b>{e(m["home"])} ضد {e(m["away"])}</b> <small>{t}، الأرجح {e(pick)} {round(m["p"][k] * 100)}%</small></li>'
    vals = [m for m in soon if has_value(m)]
    vals.sort(key=lambda m: -max(b["edge"] for b in m["best"] if b["edge"] <= MAX_EDGE))
    for m in vals[:5]:
        _, _, t = fmt_time(m["kickoff"])
        k = max((i for i in range(3) if m["best"][i]["edge"] <= MAX_EDGE), key=lambda i: m["best"][i]["edge"])
        b = m["best"][k]
        pick = PICK_TXT[k].format(h=m["home"], a=m["away"])
        rows += (f'<li>💰 <b>{e(m["home"])} ضد {e(m["away"])}</b> <small>{t}، {e(pick)}: '
                 f'{e(b["book"])} عارضة {b["odd"]:.2f}، يعني {b["edge"]:.0%} فوق العادل</small></li>')
    if not rows:
        return ""
    return f'<section class="hl"><h2>أبرز الـ 24 ساعة الجاية</h2><ul>{rows}</ul></section>'


def build_html(matches, log=None, pf=None):
    log = log or {}
    e = html.escape
    now = datetime.now(AMMAN).strftime("%d/%m/%Y %H:%M")
    groups = {}
    for m in sorted(matches, key=lambda m: m["kickoff"]):
        _, day, _ = fmt_time(m["kickoff"])
        groups.setdefault(day, {}).setdefault(m["league"], []).append(m)
    sections = ""
    comps = []
    for day, leagues in groups.items():
        sections += f'<div class="day" data-day="{e(day)}"><h2>{day}</h2>'
        for lname, ms in sorted(leagues.items(), key=lambda kv: ORDER.index(kv[1][0].get("type", "other"))):
            if lname not in comps:
                comps.append(lname)
            sections += f'<div class="grp" data-comp="{e(lname)}"><h3>{lname}</h3>' + "".join(card(m) for m in ms) + "</div>"
        sections += "</div>"
    if not sections:
        sections = "<p class='empty'>لا توجد مباريات خلال الأيام القادمة.</p>"
    comp_opts = "".join(f'<option value="{e(c)}">{e(c)}</option>' for c in comps)
    day_opts = "".join(f'<option value="{e(d)}">{e(d)}</option>' for d in groups)
    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>لوحة المباريات</title><style>
:root{{--bg:#F3F5F2;--card:#fff;--ink:#16221B;--muted:#5D6B63;--line:#DCE3DE;--w:#2F7A50;--d:#C98E2B;--l:#B9473D}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0F1612;--card:#18221C;--ink:#E7EEE9;--muted:#9AAAA1;--line:#2A3A31;--w:#45A872;--d:#E0A445;--l:#DE675C}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:Tahoma,"Segoe UI",sans-serif;line-height:1.5}}
header{{background:#1E5A3A;color:#fff;padding:18px 16px 0}} header h1{{margin:0;font-size:1.4rem}} header p{{margin:4px 0 0;opacity:.85;font-size:.85rem}}
.tabs{{display:flex;gap:4px;margin-top:12px}} .tabs button{{border:0;background:transparent;color:#fff;opacity:.75;padding:10px 14px;font:inherit;font-weight:bold;cursor:pointer;border-bottom:3px solid transparent}}
.tabs button.on{{opacity:1;border-bottom-color:#fff}}
main{{max-width:760px;margin:0 auto;padding:8px 14px 40px}}
h2{{font-size:1.1rem;margin:22px 0 4px}} h3{{font-size:.9rem;color:var(--muted);margin:14px 0 6px}}
article{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}}
.top{{display:flex;justify-content:space-between;gap:8px;font-weight:bold}} .top small{{color:var(--muted);font-weight:normal}} .time{{color:var(--muted)}}
.bar{{display:flex;height:30px;border-radius:8px;overflow:hidden;margin-top:10px}}
.bar span{{display:flex;align-items:center;justify-content:center;color:#fff;font-size:.85rem;font-weight:bold;min-width:30px}}
.w{{background:var(--w)}} .d{{background:var(--d)}} .l{{background:var(--l)}}
.legend{{display:flex;justify-content:space-between;font-size:.75rem;color:var(--muted);margin-top:3px}}
.meta{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:.78rem;color:var(--muted);margin-top:8px}}
.chip{{padding:1px 9px;border-radius:99px;font-weight:bold;color:#fff}} .chip.hi{{background:var(--w)}} .chip.mid{{background:var(--d)}} .chip.lo{{background:#7A857F}}
.out{{font-size:.8rem;margin-top:6px}}
.fair{{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;font-size:.82rem;margin-top:8px;padding:6px 8px;border-radius:8px;border:1px dashed var(--line)}}
.fair .fl{{color:var(--muted)}}
.fair.best{{border-style:solid;border-color:transparent;background:rgba(127,127,127,.07)}} .fair.extra{{border:0;padding:0 8px}}
.fair em{{font-style:normal;font-weight:bold;font-size:.75rem;padding:0 6px;border-radius:99px}}
.fair em.up{{background:rgba(47,122,80,.18);color:var(--w)}} .fair em.odd{{background:rgba(127,127,127,.18);color:var(--muted)}} .fair small{{color:var(--muted);direction:ltr;unicode-bidi:isolate}} .empty{{color:var(--muted)}}
.lu{{font-size:.8rem;margin-top:6px;padding:6px 8px;border-radius:8px;background:rgba(47,122,80,.10)}} .lu.wait{{background:transparent;color:var(--muted);padding:0}}
.move{{font-size:.8rem;margin-top:6px;color:var(--muted)}}
.alert{{font-size:.82rem;margin-top:8px;padding:6px 8px;border-radius:8px;background:rgba(201,142,43,.18);font-weight:bold}}
.filters{{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}}
.filters button,.filters select{{font:inherit;font-size:.85rem;padding:6px 12px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}}
.filters button.on{{background:#1E5A3A;color:#fff;border-color:#1E5A3A}}
.hl{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:4px 14px 8px;margin-top:14px}} .hl h2{{margin-top:10px}}
.hl ul{{margin:0;padding:0;list-style:none}} .hl li{{padding:6px 0;border-bottom:1px solid var(--line);font-size:.9rem}} .hl li:last-child{{border:0}} .hl small{{color:var(--muted)}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:16px}}
.kpis div{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;text-align:center}}
.kpis b{{display:block;font-size:1.4rem}} .kpis span{{font-size:.75rem;color:var(--muted)}}
@media (max-width:520px){{.kpis{{grid-template-columns:repeat(2,1fr)}}}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:.85rem}}
th,td{{padding:7px 8px;text-align:right;border-bottom:1px solid var(--line)}} th{{color:var(--muted);font-weight:normal}}
.res{{display:flex;gap:10px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin-bottom:6px}}
.res .rt{{display:flex;flex-direction:column}} .res small{{color:var(--muted)}}
.hint{{font-size:.8rem;color:var(--muted)}}
.note{{font-size:.78rem;color:var(--muted);margin-top:26px}}
[hidden]{{display:none!important}}
</style></head><body><header><h1>لوحة المباريات</h1><p>آخر تحديث: {now} بتوقيت عمّان</p>
<div class="tabs"><button class="on" data-tab="up">المباريات القادمة</button><button data-tab="rec">سجل التوقعات</button><button data-tab="pf">المحفظة التجريبية</button></div></header>
<main>
<div id="up">
{highlights(matches)}
<div class="filters">
<button class="on" data-f="all">الكل</button><button data-f="hi">صورة واضحة</button><button data-f="move">حركة كبيرة ⚠️</button><button data-f="value">سعر فوق العادل 💰</button>
<select id="comp"><option value="">كل البطولات</option>{comp_opts}</select>
<select id="dayf"><option value="">كل الأيام</option>{day_opts}</select>
</div>
<p class="empty" id="none" hidden>لا توجد مباريات بهالفلتر.</p>
{sections}
</div>
<div id="rec" hidden>{results_html(log)}</div>
<div id="pf" hidden>{portfolio_html(pf)}</div>
<p class="note">الاحتمالات مأخوذة من أحدّ أسعار السوق بعد إزالة هامش الشركة، وهي أدق تقدير متاح لكنها ليست يقيناً. المباريات الودية أقل دقة بكثير لأن المدربين يجربون لاعبين.</p>
</main>
<script>
var f="all";
function apply(){{
  var c=document.getElementById("comp").value, d=document.getElementById("dayf").value, any=false;
  document.querySelectorAll(".day").forEach(function(day){{
    var dayOk=!d||day.dataset.day===d, dayAny=false;
    day.querySelectorAll(".grp").forEach(function(g){{
      var gAny=false;
      g.querySelectorAll("article").forEach(function(a){{
        var ok=dayOk&&(!c||g.dataset.comp===c)&&(f==="all"||(f==="hi"&&a.dataset.conf==="hi")||(f==="move"&&a.dataset.move==="1")||(f==="value"&&a.dataset.value==="1"));
        a.hidden=!ok; if(ok)gAny=true;
      }});
      g.hidden=!gAny; if(gAny)dayAny=true;
    }});
    day.hidden=!dayAny; if(dayAny)any=true;
  }});
  document.getElementById("none").hidden=any;
}}
document.querySelectorAll(".filters button").forEach(function(b){{b.onclick=function(){{
  document.querySelectorAll(".filters button").forEach(function(x){{x.classList.remove("on")}});b.classList.add("on");f=b.dataset.f;apply();}}}});
document.getElementById("comp").onchange=apply;document.getElementById("dayf").onchange=apply;
document.querySelectorAll(".tabs button").forEach(function(b){{b.onclick=function(){{
  document.querySelectorAll(".tabs button").forEach(function(x){{x.classList.remove("on")}});b.classList.add("on");
  ["up","rec","pf"].forEach(function(id){{document.getElementById(id).hidden=b.dataset.tab!==id;}});}}}});
</script>
</body></html>"""


# ---------------- Telegram alerts ----------------
def alerts_ready():
    return os.path.exists(TG_CFG) or os.path.exists(NTFY_CFG) or bool(os.environ.get("NTFY_TOPIC"))


def tg_send(text):
    """Sends through Telegram if set up, otherwise through the ntfy app."""
    if os.path.exists(TG_CFG):
        lines = open(TG_CFG, encoding="utf-8").read().split()
        if len(lines) >= 2:
            r = requests.post(f"https://api.telegram.org/bot{lines[0]}/sendMessage",
                              data={"chat_id": lines[1], "text": text}, timeout=30)
            return r.ok
    topic = ntfy_topic()
    if topic:
        if True:
            r = requests.post(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                              headers={"Title": "Football Dashboard"}, timeout=30)
            return r.ok
    return False


def pct(x):
    return f"{round(x * 100)}%"


def is_big(m):
    return bool(m.get("move")) and max(abs(m["move"]["diff"][0]), abs(m["move"]["diff"][2])) >= BIG_MOVE


def send_alerts(matches, log):
    if not alerts_ready():
        print("   Phone alerts not set up yet (run step9_ntfy_setup.py).")
        return None
    state = {"pre": [], "move": [], "post": [], "opp": [], "opp_day": [], "opp_info": {}}
    if os.path.exists(ALERTS):
        try:
            state.update(json.load(open(ALERTS, encoding="utf-8")))
        except (ValueError, OSError):
            pass
    now = datetime.now(timezone.utc)
    sent = 0
    cfg = load_agent_cfg()
    for m in matches:
        if not m["p"]:
            continue
        fid = str(m["id"])
        mins = (datetime.fromisoformat(m["kickoff"].replace("Z", "+00:00")) - now).total_seconds() / 60
        _, cls = confidence(m["p"], m["source"], m.get("type", ""))
        big = is_big(m)
        _, _, t = fmt_time(m["kickoff"])
        h, d, a = m["p"]
        k = max(range(3), key=lambda i: m["p"][i])
        pick = PICK_TXT[k].format(h=m["home"], a=m["away"])
        # instant alert on a big price move (within 24h of kick-off)
        if cfg["send_move_alerts"] and big and fid not in state["move"] and mins <= 24 * 60:
            dh = m["move"]["diff"][0]
            who = m["home"] if dh > 0 else m["away"]
            amt = round(max(abs(m["move"]["diff"][0]), abs(m["move"]["diff"][2])) * 100)
            if tg_send(f"⚠️ حركة سعر كبيرة\n{m['home']} ضد {m['away']} ({m['league']}، {t})\n"
                       f"السعر تحرك لصالح {who} بـ {amt} نقاط منذ {m['move']['since']}.\n"
                       f"غالباً في خبر جديد (إصابة، تشكيلة، إراحة نجوم).\n"
                       f"الآن: فوز {m['home']} {pct(h)} · تعادل {pct(d)} · فوز {m['away']} {pct(a)}"):
                state["move"].append(fid); sent += 1
        # pre-match alert about an hour before, for clear matches or big movers
        if (cfg["send_prematch_alerts"] and (cls == "hi" or big) and fid not in state["pre"]
                and fid not in state["opp"] and 0 < mins <= PRE_ALERT_MIN):
            msg = (f"⏰ بعد حوالي ساعة: {m['home']} ضد {m['away']}\n{m['league']}، الساعة {t}\n\n"
                   f"فوز {m['home']}: {pct(h)}\nتعادل: {pct(d)}\nفوز {m['away']}: {pct(a)}\n"
                   f"الأرجح: {pick} ({pct(m['p'][k])})")
            if big:
                msg += "\n⚠️ السعر تحرك بشكل كبير قبل المباراة"
            if m.get("lineups"):
                msg += "\n✅ التشكيلات الرسمية نزلت، شوفها باللوحة"
            n_out = len(m["out"]["home"]) + len(m["out"]["away"])
            if n_out:
                msg += f"\nالغيابات: {n_out} لاعبين"
            if tg_send(msg):
                state["pre"].append(fid); sent += 1
    # results: one message per update listing every match that just finished (opportunities get their own)
    today = datetime.now(AMMAN).date()
    yday = (today - timedelta(days=1)).isoformat()
    todays = [r for r in log.values() if r["day"] == today.isoformat()]
    ok_today = sum(1 for r in todays if r["ok"])
    if cfg.get("result_alerts", "all") != "none":
        fresh = [(fid, r) for fid, r in log.items()
                 if fid not in state["post"] and fid not in state["opp"] and r["day"] >= yday]
        if cfg.get("result_alerts") == "opportunities":
            fresh = []
        lines = []
        for fid, r in fresh:
            pick = PICK_TXT[r["pick"]].format(h=r["home"], a=r["away"])
            lines.append(f"{'✅' if r['ok'] else '❌'} {r['home']} {r['score']} {r['away']}\n"
                         f"     توقعنا {pick} ({pct(max(r['p']))}) · {r['comp']}")
        if lines:
            shown = lines[:20]
            more = f"\n... و {len(lines) - 20} مباريات ثانية باللوحة" if len(lines) > 20 else ""
            rate = f" ({ok_today / len(todays):.0%})" if todays else ""
            msg = ("\n".join(shown) + more + f"\n\nحصيلة اليوم لحد الآن: {ok_today} صح من {len(todays)}{rate}")
            if ntfy_publish(f"🏁 انتهت {len(lines)} مباراة", msg) or tg_send(msg):
                state["post"].extend(fid for fid, _ in fresh)
                sent += 1
    pf = None
    try:
        pf = run_agent(matches, log, state)
    except Exception as e:
        print(f"   Agent skipped: {e}")
    for k_ in state:
        if isinstance(state[k_], list):
            state[k_] = state[k_][-3000:]
    json.dump(state, open(ALERTS, "w", encoding="utf-8"))
    print(f"   Phone alerts sent: {sent}")
    return pf


# ---------------- Smart agent + virtual portfolio ----------------
AGENT_CFG = "agent_settings.json"
PORTFOLIO = os.path.join("data", "portfolio.json")
DEFAULT_AGENT = {"stake": 10, "only_wins": True, "min_win_percent": 50, "value_min_percent": 2,
                 "require_value": True, "clear_min_percent": 60,
                 "alert_hours_before": 3, "max_per_day": 15,
                 "send_prematch_alerts": False, "send_move_alerts": False,
                 "require_sharp_source": True, "include_arab_leagues": True,
                 "include_european_cups": False,
                 "result_alerts": "all", "auto_approve": True}
RULE_TXT = {"value": "فوز مرجّح بسعر فوق العادل", "clear": "صورة واضحة"}


def load_agent_cfg():
    cfg = dict(DEFAULT_AGENT)
    if not os.path.exists(AGENT_CFG):
        json.dump(DEFAULT_AGENT, open(AGENT_CFG, "w", encoding="utf-8"), indent=2)
    else:
        try:
            user = json.load(open(AGENT_CFG, encoding="utf-8"))
            cfg.update(user)
            if set(DEFAULT_AGENT) - set(user):            # show new settings in the file
                json.dump(cfg, open(AGENT_CFG, "w", encoding="utf-8"), indent=2)
        except (ValueError, OSError):
            print("   agent_settings.json has a typo, using default settings.")
    return cfg


def load_portfolio():
    if os.path.exists(PORTFOLIO):
        try:
            return json.load(open(PORTFOLIO, encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"bets": {}, "ok_since": "12h", "weekly": ""}


def ntfy_topic():
    if os.environ.get("NTFY_TOPIC"):
        return os.environ["NTFY_TOPIC"].strip()
    if os.path.exists(NTFY_CFG):
        return open(NTFY_CFG, encoding="utf-8").read().strip()
    return ""


def ntfy_publish(title, message, actions=None):
    topic = ntfy_topic()
    if not topic:
        return False
    payload = {"topic": topic, "title": title, "message": message}
    if actions:
        payload["actions"] = actions
    return requests.post("https://ntfy.sh/", json=payload, timeout=30).ok


def find_opportunity(m, cfg):
    """Picks the one outcome in a match that meets the user's rules, or None."""
    if not m["p"] or m.get("type") == "friendly":
        return None
    if cfg.get("require_sharp_source", True) and m["source"] != "Pinnacle":
        return None                     # "fair" is only trusted when it comes from Pinnacle
    best = m.get("best") or []
    outcomes = (0, 2) if cfg["only_wins"] else (0, 1, 2)
    min_p = cfg["min_win_percent"] / 100
    cands = []
    for k in outcomes:
        if k >= len(best) or m["p"][k] < min_p:
            continue
        b = best[k]
        if cfg["value_min_percent"] / 100 <= b["edge"] <= MAX_EDGE:
            cands.append({"rule": "value", "k": k, "odd": b["odd"], "book": b["book"], "edge": b["edge"]})
    k = max(outcomes, key=lambda i: m["p"][i])
    if (not cfg["require_value"] and not cands and m["p"][k] >= cfg["clear_min_percent"] / 100
            and m["source"] == "Pinnacle"):
        if best and best[k]["edge"] <= MAX_EDGE:
            odd, book = best[k]["odd"], best[k]["book"]
        else:
            odd, book = 1 / m["p"][k], "السعر العادل"
        cands.append({"rule": "clear", "k": k, "odd": odd, "book": book, "edge": odd * m["p"][k] - 1})
    if not cands:
        return None
    o = max(cands, key=lambda c: c["edge"])
    o["odd"] = round(o["odd"], 2)
    o["prob"] = m["p"][o["k"]]
    return o


def run_agent(matches, log, state):
    cfg = load_agent_cfg()
    pf = load_portfolio()
    topic = ntfy_topic()
    stake = cfg["stake"]
    info = state.setdefault("opp_info", {})
    sent = 0

    # 1) approvals pressed on the phone
    if topic:
        try:
            r = requests.get(f"https://ntfy.sh/{topic}-ok/json",
                             params={"poll": "1", "since": pf.get("ok_since", "12h")}, timeout=30)
            for line in r.text.splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") != "message":
                    continue
                pf["ok_since"] = ev["id"]
                parts = ev.get("message", "").split()
                if len(parts) != 4 or parts[0] != "APPROVE":
                    continue
                fid, k, odd = parts[1], int(parts[2]), float(parts[3])
                if fid in pf["bets"] or fid not in info:
                    continue
                i = info[fid]
                ko = datetime.fromisoformat(i["kickoff"].replace("Z", "+00:00"))
                if ev.get("time", 0) > ko.timestamp():
                    ntfy_publish("⏱️ ما تسجّلت", f"{i['home']} ضد {i['away']}: الموافقة وصلت بعد بداية المباراة.")
                    continue
                pf["bets"][fid] = {**i, "k": k, "odd": odd, "stake": stake, "status": "open",
                                   "approved": datetime.fromtimestamp(ev["time"], AMMAN).strftime("%d/%m %H:%M")}
                pick = PICK_TXT[k].format(h=i["home"], a=i["away"])
                ntfy_publish("✅ تسجّلت بالمحفظة", f"{i['home']} ضد {i['away']}\n{pick} بسعر {odd}\nمبلغ وهمي: {stake}")
                sent += 1
        except requests.RequestException as e:
            print(f"   Could not read approvals: {e}")

    # 2) settle finished matches
    for fid, b in pf["bets"].items():
        if b["status"] == "open" and fid in log:
            r = log[fid]
            won = r["res"] == b["k"]
            b["status"] = "won" if won else "lost"
            b["score"] = r["score"]
            b["profit"] = round(b["stake"] * (b["odd"] - 1), 2) if won else -b["stake"]
            total = sum(x.get("profit", 0) for x in pf["bets"].values())
            pick = PICK_TXT[b["k"]].format(h=b["home"], a=b["away"])
            res = f"✅ ربحت {b['profit']:+.2f}" if won else f"❌ خسرت {b['profit']:+.2f}"
            ntfy_publish("🏁 نتيجة المحفظة", f"{b['home']} {b['score']} {b['away']}\nاختيارك: {pick} بسعر {b['odd']}\n"
                                             f"{res}\nرصيد المحفظة الكلي: {total:+.2f}")
            sent += 1

    # 2b) every opportunity we sent: what happened, approved or not
    allo = pf.setdefault("all_opps", {})
    for fid, i in list(info.items()):
        if fid in log and "k" in i and fid not in allo:
            r = log[fid]
            won = r["res"] == i["k"]
            profit = round(stake * (i["odd"] - 1), 2) if won else -stake
            allo[fid] = {**i, "score": r["score"], "won": won, "profit": profit, "stake": stake,
                         "approved": fid in pf["bets"]}
            if fid not in pf["bets"] and fid not in state["post"]:
                pick = PICK_TXT[i["k"]].format(h=i["home"], a=i["away"])
                done = list(allo.values())
                tot = sum(x["profit"] for x in done)
                hit = sum(1 for x in done if x["won"])
                ntfy_publish("🏁 نتيجة الفرصة",
                             f"{i['home']} {r['score']} {i['away']}\n{i['comp']}\n"
                             f"الفرصة كانت: {pick} بسعر {i['odd']} (احتمال {pct(i['prob'])})\n"
                             f"{'✅ صحت' if won else '❌ ما صحت'}\n"
                             f"ما سجلتها بالمحفظة. لو سجلتها: {profit:+.2f}\n\n"
                             f"كل الفرص لحد الآن: {hit} صح من {len(done)}، المجموع لو سجلتها كلها {tot:+.2f}")
                state["post"].append(fid)
                sent += 1

    # 3) new opportunities that match the rules
    now = datetime.now(timezone.utc)
    today = datetime.now(AMMAN).date().isoformat()
    sent_today = state["opp_day"].count(today)
    if topic:
        for m in sorted(matches, key=lambda m: m["kickoff"]):
            fid = str(m["id"])
            if fid in state["opp"] or sent_today >= cfg["max_per_day"]:
                continue
            mins = (datetime.fromisoformat(m["kickoff"].replace("Z", "+00:00")) - now).total_seconds() / 60
            if not 30 < mins <= cfg["alert_hours_before"] * 60:
                continue
            o = find_opportunity(m, cfg)
            if not o:
                continue
            _, _, t = fmt_time(m["kickoff"])
            h, d, a = m["p"]
            pick = PICK_TXT[o["k"]].format(h=m["home"], a=m["away"])
            msg = (f"{m['home']} ضد {m['away']}\n{m['league']}، الساعة {t}\n\n"
                   f"السبب: {RULE_TXT[o['rule']]}\n"
                   f"الاختيار: {pick}\nالاحتمال: {pct(o['prob'])}، السعر العادل {1 / o['prob']:.2f}\n"
                   f"أعلى سعر: {o['odd']} عند {o['book']}" + (f" ({o['edge']:+.0%})" if o["rule"] == "value" else "") +
                   f"\n\nكل الاحتمالات: {pct(h)} / {pct(d)} / {pct(a)}")
            if is_big(m):
                msg += "\n⚠️ السعر تحرك بشكل كبير"
            n_out = len(m["out"]["home"]) + len(m["out"]["away"])
            if n_out:
                msg += f"\nالغيابات: {n_out} لاعبين"
            auto = cfg.get("auto_approve", True)
            if auto:
                msg += f"\n\n✅ تسجّلت تلقائياً بالمحفظة التجريبية بمبلغ {stake}."
                actions = None
            else:
                msg += "\n\nإذا بدك تسجلها بالمحفظة التجريبية، اضغط الزر."
                actions = [{"action": "http", "label": f"سجّل بالمحفظة ({stake})", "url": f"https://ntfy.sh/{topic}-ok",
                            "method": "POST", "body": f"APPROVE {fid} {o['k']} {o['odd']}", "clear": True}]
            if ntfy_publish("💡 فرصة حسب شروطك", msg, actions):
                state["opp"].append(fid)
                state["opp_day"].append(today)
                info[fid] = {"home": m["home"], "away": m["away"], "comp": m["league"],
                             "kickoff": m["kickoff"], "rule": o["rule"], "k": o["k"], "odd": o["odd"],
                             "prob": o["prob"]}
                if auto:
                    pf["bets"][fid] = {**info[fid], "stake": stake, "status": "open", "auto": True,
                                       "approved": datetime.now(AMMAN).strftime("%d/%m %H:%M")}
                sent_today += 1
                sent += 1

    # 4) weekly report, Monday morning
    nowa = datetime.now(AMMAN)
    week = f"{nowa.isocalendar()[0]}-{nowa.isocalendar()[1]}"
    if nowa.weekday() == 0 and nowa.hour >= 9 and pf.get("weekly") != week:
        cutoff = (nowa - timedelta(days=7)).date().isoformat()
        wk = [b for b in pf["bets"].values() if b["status"] != "open" and b["kickoff"][:10] >= cutoff]
        allb = [b for b in pf["bets"].values() if b["status"] != "open"]
        if allb:
            def line(bs):
                st = sum(b["stake"] for b in bs)
                pr = sum(b["profit"] for b in bs)
                w = sum(1 for b in bs if b["status"] == "won")
                return f"{len(bs)} اختيار، {w} صح، الربح {pr:+.2f} من {st:.0f} ({(pr / st if st else 0):+.1%})"
            msg = f"آخر 7 أيام: {line(wk) if wk else 'ما في اختيارات'}\nمن البداية: {line(allb)}"
            if ntfy_publish("📊 تقرير المحفظة الأسبوعي", msg):
                pf["weekly"] = week
                sent += 1

    if len(info) > 800:
        for k_ in list(info)[:-800]:
            info.pop(k_, None)
    os.makedirs("data", exist_ok=True)
    json.dump(pf, open(PORTFOLIO, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"   Agent messages sent: {sent}, portfolio bets: {len(pf['bets'])}")
    return pf


def portfolio_html(pf):
    e = html.escape
    cfg = load_agent_cfg()
    bets = sorted((pf or {}).get("bets", {}).values(), key=lambda b: b["kickoff"], reverse=True)
    done = [b for b in bets if b["status"] != "open"]
    open_ = [b for b in bets if b["status"] == "open"]
    st = sum(b["stake"] for b in done)
    pr = sum(b["profit"] for b in done)
    w = sum(1 for b in done if b["status"] == "won")
    out = f"""<div class="kpis">
<div><b>{len(done)}</b><span>اختيار انتهى</span></div>
<div><b>{w}</b><span>صح</span></div>
<div><b>{pr:+.2f}</b><span>الربح أو الخسارة الوهمية</span></div>
<div><b>{(pr / st if st else 0):+.1%}</b><span>العائد من {st:.0f}</span></div></div>
<p class="hint">مبالغ وهمية بالكامل. {"كل فرصة بتنطبق عليها الشروط بتنسجل هون تلقائياً" if cfg.get("auto_approve", True) else "كل اختيار بتوافق عليه من الإشعار بينسجل هون"} بمبلغ {cfg['stake']}، وبعد المباراة بينحسب لحاله.
شروطك الحالية: {"فوز فقط (بدون تعادل)" if cfg['only_wins'] else "كل النتائج"}، احتمال {cfg['min_win_percent']}% أو أكثر،
وسعر أعلى من العادل بـ {cfg['value_min_percent']}% أو أكثر{"، والسعر العادل من Pinnacle فقط" if cfg.get('require_sharp_source', True) else ""}{"" if cfg['require_value'] else f"، أو صورة واضحة من {cfg['clear_min_percent']}%"}،
والإشعار قبل المباراة بـ {cfg['alert_hours_before']} ساعات كحد أقصى. بتقدر تغيّرها من ملف agent_settings.json.</p>"""
    allo = list((pf or {}).get("all_opps", {}).values())
    if allo:
        a_st = sum(x.get("stake", cfg["stake"]) for x in allo)
        a_pr = sum(x["profit"] for x in allo)
        a_w = sum(1 for x in allo if x["won"])
        out += (f'<h3>كل الفرص اللي وصلتك (سجلتها أو لا)</h3><p class="hint">{len(allo)} فرصة، {a_w} صحت. '
                f'لو سجلتها كلها: <b>{a_pr:+.2f}</b> ({(a_pr / a_st if a_st else 0):+.1%}). '
                f'هاد بيقيس الشروط نفسها، بغض النظر عن اختياراتك.</p>')
    for rule, name in RULE_TXT.items():
        rs = [b for b in done if b.get("rule") == rule]
        if rs:
            s_ = sum(b["stake"] for b in rs)
            p_ = sum(b["profit"] for b in rs)
            out += f'<p class="hint"><b>{name}:</b> {len(rs)} اختيار، الربح {p_:+.2f} ({p_ / s_:+.1%})</p>'
    if open_:
        out += "<h3>بانتظار المباراة</h3>"
        for b in open_:
            pick = PICK_TXT[b["k"]].format(h=b["home"], a=b["away"])
            _, day, t = fmt_time(b["kickoff"])
            out += (f'<div class="res"><span class="ic">⏳</span><span class="rt"><b>{e(b["home"])} ضد {e(b["away"])}</b>'
                    f'<small>{e(day)} {t} · {e(pick)} بسعر {b["odd"]} · مبلغ {b["stake"]}</small></span></div>')
    if done:
        out += "<h3>انتهت</h3>"
        for b in done[:40]:
            pick = PICK_TXT[b["k"]].format(h=b["home"], a=b["away"])
            icon = "✅" if b["status"] == "won" else "❌"
            out += (f'<div class="res {"ok" if b["status"] == "won" else "no"}"><span class="ic">{icon}</span>'
                    f'<span class="rt"><b>{e(b["home"])} {e(b["score"])} {e(b["away"])}</b>'
                    f'<small>{e(pick)} بسعر {b["odd"]} · {b["profit"]:+.2f}</small></span></div>')
    if not bets:
        out += ("<p class='empty'>المحفظة فاضية. لما يوصلك إشعار 💡 فرصة حسب شروطك، "
                "اضغط زر «سجّل بالمحفظة» وبتنضاف هون.</p>")
    return out


def daily_summary(log, pf):
    """Sends the end-of-day message once per day (run with --daily at 23:55)."""
    state = {}
    if os.path.exists(ALERTS):
        try:
            state = json.load(open(ALERTS, encoding="utf-8"))
        except (ValueError, OSError):
            state = {}
    now_a = datetime.now(AMMAN)
    today = now_a.date() if now_a.hour >= 12 else now_a.date() - timedelta(days=1)
    unfinished = UNFINISHED_TODAY if today == now_a.date() else []
    if state.get("daily") == today.isoformat():
        print("   Daily summary already sent today.")
        return
    recs = [r for r in log.values() if r["day"] == today.isoformat()]
    d = f"{today.day}/{today.month}"
    if not recs and not unfinished:
        msg = "ما كان في مباريات اليوم بالبطولات اللي بنتابعها."
    else:
        ok = sum(1 for r in recs if r["ok"])
        lines = [f"المباريات اللي انتهت: {len(recs)}"]
        if recs:
            exp = sum(max(r["p"]) for r in recs) / len(recs)
            lines.append(f"✅ صح: {ok}   ❌ غلط: {len(recs) - ok}   النسبة: {ok / len(recs):.0%}")
            lines.append(f"النسبة المتوقعة: {exp:.0%}")
            for cls, name in (("hi", "صورة واضحة"), ("mid", "صورة متوسطة"), ("lo", "مفتوحة")):
                rs = [r for r in recs if r["conf"] == cls]
                if rs:
                    lines.append(f"   {name}: {sum(1 for r in rs if r['ok'])} من {len(rs)}")
        allo = [x for x in (pf or {}).get("all_opps", {}).values() if x["kickoff"] and
                datetime.fromisoformat(x["kickoff"].replace("Z", "+00:00")).astimezone(AMMAN).date() == today]
        if allo:
            pr = sum(x["profit"] for x in allo)
            lines.append(f"\n💡 الفرص اليوم: {len(allo)}، صحت {sum(1 for x in allo if x['won'])}، "
                         f"لو سجلتها كلها {pr:+.2f}")
        bets = [b for b in (pf or {}).get("bets", {}).values() if b["status"] != "open"]
        today_bets = [b for b in bets if datetime.fromisoformat(b["kickoff"].replace("Z", "+00:00"))
                      .astimezone(AMMAN).date() == today]
        if today_bets:
            lines.append(f"💼 المحفظة اليوم: {sum(b['profit'] for b in today_bets):+.2f}، "
                         f"الرصيد الكلي {sum(b['profit'] for b in bets):+.2f}")
        if unfinished:
            lines.append(f"\n⏳ لسا ما خلصت: {len(unfinished)} (نتيجتها بتوصلك أول ما تخلص)")
        if recs:
            lines.append("")
            for r in sorted(recs, key=lambda r: not r["ok"])[:30]:
                pick = PICK_TXT[r["pick"]].format(h=r["home"], a=r["away"])
                lines.append(f"{'✅' if r['ok'] else '❌'} {r['home']} {r['score']} {r['away']} · {pick}")
        msg = "\n".join(lines)
    if ntfy_publish(f"📊 حصيلة يوم {d}", msg) or tg_send(msg):
        if now_a.hour >= 22 or now_a.hour < 3:      # a test during the day does not block tonight's summary
            state["daily"] = today.isoformat()
        json.dump(state, open(ALERTS, "w", encoding="utf-8"))
        print("   Daily summary sent.")
        send_dashboard_file()


def send_dashboard_file():
    """Attaches dashboard.html to a phone notification (ntfy keeps it for a few hours)."""
    topic = ntfy_topic()
    if not topic or not os.path.exists("dashboard.html"):
        return
    try:
        with open("dashboard.html", "rb") as fh:
            requests.put(f"https://ntfy.sh/{topic}", data=fh,
                         headers={"Filename": "dashboard.html", "Title": "Dashboard"}, timeout=60)
    except requests.RequestException as e:
        print(f"   Dashboard file not sent: {e}")


def main():
    key = get_key()
    COMPS.clear()
    COMPS.update(build_comps(key, load_agent_cfg()))
    print(f"Following {len(COMPS)} competitions.")
    print(f"Collecting matches for the next {DAYS_AHEAD} days...")
    matches, log = collect(key)
    track_prices(matches)
    pf = None
    try:
        pf = send_alerts(matches, log)
    except Exception as e:                      # alerts must never stop the dashboard
        print(f"   Phone alerts skipped: {e}")
    pf = pf or load_portfolio()
    json.dump(matches, open(os.path.join("data", "dashboard_data.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1) if os.path.isdir("data") else None
    open("dashboard.html", "w", encoding="utf-8").write(build_html(matches, log, pf))
    if "--daily" in sys.argv:
        try:
            daily_summary(log, pf)
        except Exception as e:
            print(f"   Daily summary skipped: {e}")
    with_odds = sum(1 for m in matches if m["p"])
    with_lu = sum(1 for m in matches if m.get("lineups"))
    print(f"\n{len(matches)} matches, {with_odds} with prices, {with_lu} with official lineups. Requests used: {calls}")
    print("Done. Open dashboard.html on your Desktop (double click).")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"\n{e}")
        if "season" in str(e).lower() or "plan" in str(e).lower():
            print("This usually means the Pro plan is not active yet on this key.")
