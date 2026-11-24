"""Sample Yahoo win odds for every matchup in the Harry Koch Memorial Cup and append to a JSON history.
Runs in GitHub Actions with Playwright (the live win percentage only renders in a browser).
Usage: python tools/odds_sampler.py <out_dir>
       python tools/odds_sampler.py --slim <out_dir>   (rewrite the slim files from the week files already there; no browser needed)
       python tools/odds_sampler.py --idle-gap <out_dir>   (seconds the workflow may wait before its next pass; see idle_gap())

Files in <out_dir>:
  weekN.json       the week's full history: every sample with every matchup's scores, odds, starters and shotgun entries. The sampler
                   reads it back each pass and appends. About 10 MB by the end of a week.
  current.json     a copy of the current week's weekN.json, kept for pages and tabs that predate the slim files.
  weekN.lite.json  the same week as the page needs it (see slim()): the latest sample in full, and for the chart only the time and
                   the left team's odds of every sample. Well under a hundredth of the size.
  live.json        a copy of the current week's weekN.lite.json. This is what the page polls.
  next.json        next week's matchups, staged once the current week is decided.
"""
import asyncio, datetime, glob, html, json, os, re, sys, urllib.request
from datetime import timedelta
from zoneinfo import ZoneInfo
try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None   # only the sampling passes need a browser; --slim runs without one

LEAGUE = 932579
BASE = f"https://football.fantasysports.yahoo.com/f1/{LEAGUE}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
ET = ZoneInfo("America/New_York")
WEEK1_THU = datetime.datetime(2026, 9, 10, 20, 15, tzinfo=ET)

def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read().decode("utf-8", "replace")

def text(raw):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)))

def week_meta(week):
    thu = WEEK1_THU + timedelta(days=7 * (week - 1)); sun = thu + timedelta(days=3); mon = sun + timedelta(days=1)
    segs = [("TNF", thu, thu.replace(hour=23, minute=59), 0.12), ("SUN", sun.replace(hour=13, minute=0), sun.replace(hour=23, minute=59), 0.76), ("MNF", mon.replace(hour=20, minute=15), mon.replace(hour=23, minute=59), 0.12)]
    return ({"segments": [dict(label=l, start=a.isoformat(), end=b.isoformat(), w=w) for l, a, b, w in segs],
             "ticks": [dict(at=sun.replace(hour=16, minute=5).isoformat()), dict(at=sun.replace(hour=20, minute=20).isoformat())]})

def slot_label(t):
    """Short band label for a kickoff slot: TNF/SNF/MNF for night games, otherwise the time, prefixed by the day unless it is Sunday."""
    wd, h = t.weekday(), t.hour + t.minute / 60
    if h >= 19: return {3: "TNF", 6: "SNF", 0: "MNF"}.get(wd, t.strftime("%a").upper() + " NT")
    h12 = t.hour % 12 or 12
    tm = f"{h12}{'PM' if t.hour >= 12 else 'AM'}" if t.minute < 30 else f"{h12}:{t.minute:02d}"   # 1:00/4:05/4:25 -> hour, 9:30/12:30 -> as is
    return tm if wd == 6 else f"{t.strftime('%a').upper()} {tm}"

def nfl_windows(week):
    """This week's real game windows from ESPN's schedule: kickoffs within 30 minutes of each other share a slot (1:00/1:05, 4:05/4:25),
    a slot runs 195 minutes past its last kickoff or hands off at the next kickoff, and each carries its game count for the chart's widths.
    So a London 9:30, a Thanksgiving triple-header, Saturday games or a week with no morning game all lay out from the schedule."""
    try:
        d = json.loads(get(f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&seasontype=2&dates={WEEK1_THU.year}"))
        kicks = sorted(datetime.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET) for e in d["events"]
                       if e.get("timeValid", True) and e["competitions"][0].get("timeValid", True))   # late-season TBD kickoffs sit at midnight
    except Exception as e:
        print("schedule fetch failed:", e, file=sys.stderr); return None
    if not kicks: return None
    slots = []
    for k in kicks:
        if slots and (k - slots[-1]["start"]).total_seconds() <= 30 * 60: slots[-1]["games"] += 1; slots[-1]["last"] = k
        else: slots.append({"start": k, "last": k, "games": 1})
    out = []
    for i, sl in enumerate(slots):
        end = sl["last"] + timedelta(minutes=195)
        if i + 1 < len(slots) and slots[i + 1]["start"] < end: end = slots[i + 1]["start"]
        out.append(dict(label=slot_label(sl["start"]), start=sl["start"].isoformat(), end=end.isoformat(), games=sl["games"]))
    return out

_RZ = None
def league_feed():
    """Yahoo's public league feed (current week, each team's record and points). Fetched once per pass."""
    global _RZ
    if _RZ is None:
        _RZ = json.loads(get(f"https://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id={LEAGUE}&format=json"))["service"]["leagues"][str(LEAGUE)]
    return _RZ

def current_week():
    return int(league_feed()["weekInfo"]["week"])

def standings_base(week):
    """Every team's record and points entering this week, for the page's projected standings. The feed says which week its points run
    through (pfWeek); anything other than exactly last week returns None, so a base is never built from the wrong week."""
    teams = league_feed()["teams"]
    if {int(t.get("pfWeek") or 0) for t in teams.values()} != {week - 1}: return None
    return {"through": week - 1, "teams": {str(k): {"w": int(t["wins"]), "l": int(t["losses"]), "t": int(t.get("ties") or 0),
                                                  "pf": float(t["pf"]), "pa": float(t["pa"])} for k, t in teams.items()}}

def advance_base(base, matchups):
    """A base moved forward one finished week by adding that week's final scores. Covers the stretch after Yahoo turns the week over
    but before its feed has folded last week's results into the records."""
    teams = {k: dict(v) for k, v in base["teams"].items()}
    for m in matchups:
        a, b = m["id"].split("v"); ls, rs = m["leftScore"], m["rightScore"]
        if a not in teams or b not in teams: continue
        teams[a]["pf"] = round(teams[a]["pf"] + ls, 2); teams[a]["pa"] = round(teams[a]["pa"] + rs, 2)
        teams[b]["pf"] = round(teams[b]["pf"] + rs, 2); teams[b]["pa"] = round(teams[b]["pa"] + ls, 2)
        if ls == rs: teams[a]["t"] += 1; teams[b]["t"] += 1
        else:
            teams[a if ls > rs else b]["w"] += 1; teams[b if ls > rs else a]["l"] += 1
    return {"through": base["through"] + 1, "teams": teams}

def scoreboard(week=None):
    week = week or current_week()
    raw = get(f"{BASE}?matchup_week={week}&module=matchups&lhst=matchups")
    pairs = []
    for a, b in re.findall(rf"/f1/{LEAGUE}/matchup\?week={week}&(?:amp;)?mid1=(\d+)&(?:amp;)?mid2=(\d+)", raw):
        if (int(a), int(b)) not in pairs: pairs.append((int(a), int(b)))
    t = text(raw)
    i = t.find(f"Week {week} Matchups"); j = t.find("Standings", i)
    rows = []
    rx = r"View Matchup (.*?) (\d+)-(\d+)-\d+ \| \w+ ([\d.]+) ([\d.]+) vs ([\d.]+) ([\d.]+) (.*?) (\d+)-(\d+)-\d+ \|"
    for (a, b), m in zip(pairs, re.finditer(rx, t[i:j])):
        n1, w1, l1, c1, p1, c2, p2, n2, w2, l2 = m.groups()
        rows.append(dict(id=f"{a}v{b}", left=n1, leftRec=f"{w1}-{l1}", right=n2, rightRec=f"{w2}-{l2}",
                         leftScore=float(c1), leftProj=float(p1), rightScore=float(c2), rightProj=float(p2)))
    return week, rows


ROSTER_JS = """() => {
  // Find the starters table by its header (works for both Yahoo layouts: live has 9 columns, non-live has 11 with repeated Pos cells).
  const tables = [...document.querySelectorAll('table')];
  for (const t of tables) {
    const rows = [...t.querySelectorAll('tr')];
    const hdrRow = rows.find(r => [...r.querySelectorAll('th,td')].some(c => c.innerText.trim() === 'Pos'));
    if (!hdrRow) continue;
    const H = [...hdrRow.querySelectorAll('th,td')].map(c => c.innerText.trim());
    const idx = (label) => H.map((h, i) => h === label ? i : -1).filter(i => i >= 0);
    const player = idx('Player'), proj = idx('Proj'), pts = idx('Fan Pts'), pos = idx('Pos');
    if (player.length < 2 || proj.length < 2 || pts.length < 2 || !pos.length) continue;
    const out = [];
    const cell = td => { const a = td.querySelector('a'); return [a ? a.innerText.trim() : '', td.innerText.replace(/\\s+/g, ' ').trim()]; };
    for (const r of rows) {
      const c = [...r.querySelectorAll('th,td')]; if (c.length !== H.length) continue;
      const slot = c[pos[0]].innerText.trim(); if (!slot || slot === 'Pos' || /^total$/i.test(slot)) continue;
      out.push({slot, L: cell(c[player[0]]), lp: c[pts[0]].innerText.trim(), lpr: c[proj[0]].innerText.trim(),
                R: cell(c[player[player.length-1]]), rp: c[pts[pts.length-1]].innerText.trim(), rpr: c[proj[proj.length-1]].innerText.trim()});
    }
    if (out.length) return out;
  }
  return [];
}"""

JUNK = re.compile(r"(Video Forecast|New Player Note|No new player Notes|Player Note|Player Notes)", re.I)

def short_name(name):
    """'Matthew Stafford' -> 'M. Stafford'; already-short names and team defenses are left alone."""
    parts = name.split()
    if len(parts) >= 2 and len(parts[0]) > 2 and not parts[0].endswith("."):
        return parts[0][0] + ". " + " ".join(parts[1:])
    return name

def parse_player(name, text, pts):
    rest = text[len(name):].strip() if name and text.startswith(name) else text
    rest = JUNK.sub(" ", rest).strip()
    m = re.search(r"\b([A-Za-z]{2,3}) - ([A-Z/,]+)\s*(.*)$", rest)
    team, pos, game = m.groups() if m else ("", "", rest)
    tag = None
    t = re.match(r"(IR-R|IR|PUP|SUSP|NFI|DTD|NA|O|Q|D|P)\s+(.*)", game)
    if t: tag, game = t.groups()
    if re.search(r"\bFinal\b", game): state = "final"
    elif re.search(r"\b(1st|2nd|3rd|4th|OT|Half|Halftime|Delay)\b", game): state = "live"
    elif re.search(r"No Game|\bBye\b", game, re.I): state = "bye"
    elif not name: state = "empty"
    else: state = "pending"
    p = float(pts) if re.match(r"-?\d", pts) else 0.0
    return dict(player=short_name(name) if name else "(Empty)", pos=pos, team=team, pts=p, game="" if state == "empty" else game.strip(), tag=tag, state=state)

def active_starters(rows, side, top=9):
    """Starters whose game is still to come or in progress, ranked by expected remaining points (Yahoo's live projection minus points so far)."""
    out = []
    for r in rows:
        name, text = r[side]; pts = r["lp"] if side == "L" else r["rp"]; proj = r.get("lpr" if side == "L" else "rpr", "")
        d = parse_player(name, text, pts)
        if d["state"] not in ("pending", "live"): continue
        nums = re.findall(r"-?\d+\.\d+", proj or "")
        orig_proj = float(nums[0]) if nums else 0.0
        live_proj = float(nums[-1]) if nums else 0.0
        remaining = live_proj if d["state"] == "pending" else max(live_proj - d["pts"], 0.0)
        if remaining <= 0 and d["state"] != "live": continue   # a live player always shows, even when Yahoo projects nothing more from him
        out.append(dict(player=d["player"], pos=d["pos"], slot=r["slot"], rem=round(remaining, 2), state=d["state"],
                        pts=d["pts"], proj=orig_proj, live=live_proj, game=d["game"]))
    out.sort(key=lambda x: -x["rem"])
    return out[:top]

def starters_left(rows, side):
    """How many of a side's starters have a game still to come or in progress, counted from the roster table."""
    pk = "lp" if side == "L" else "rp"
    return sum(1 for r in rows if r["slot"] not in ("BN", "IR") and parse_player(r[side][0], r[side][1], r[pk])["state"] in ("pending", "live"))

def shotgun_entries(rows, left_team, right_team):
    """Starters at zero or below whose fate is in play: live or final games, inactive tags, byes, empty slots."""
    out = []
    for r in rows:
        for side, who in (("L", left_team), ("R", right_team)):
            name, text = r[side]; pts = r["lp"] if side == "L" else r["rp"]; proj = r.get("lpr" if side == "L" else "rpr", "")
            d = parse_player(name, text, pts)
            nums = re.findall(r"-?\d+\.\d+", proj or "")
            live_proj = float(nums[-1]) if nums else None
            # Two ways in: sitting at zero or below, or a live player Yahoo now projects to finish at zero or below (a defense getting shelled).
            proj_neg = d["state"] == "live" and live_proj is not None and live_proj <= 0
            if d["pts"] > 0 and not proj_neg: continue
            inactive = d["tag"] in ("O", "IR", "IR-R", "PUP", "SUSP", "NFI", "NA")
            if d["state"] == "pending" and not inactive: continue
            d.update(slot=r["slot"], manager=who, locked=d["state"] == "final", live=live_proj, reason="proj" if (d["pts"] > 0 and proj_neg) else "pts")
            out.append(d)
    return out

async def odds_for(page, week, mid):
    a, b = mid.split("v")
    await page.goto(f"{BASE}/matchup?week={week}&mid1={a}&mid2={b}", wait_until="domcontentloaded", timeout=45000)
    # Wait for the odds themselves, not just the layout: the live layout prints "Winner"/"Loser" before the percentages render.
    body = ""
    for attempt in range(3):
        try:
            await page.wait_for_function("() => /\\d+%[\\s\\S]{0,40}?Projected Winner|(Favorite|Underdog) \\d+%|100%|0 Players [Rr]emaining[\\s\\S]*?Players [Rr]emaining 0/.test(document.body.innerText)", timeout=15000)
        except Exception:
            pass
        body = await page.inner_text("body")
        if re.search(r"\d+%[\s\S]{0,40}?Projected Winner|(Favorite|Underdog) \d+%", body) or "100%" in body: break
        await page.wait_for_timeout(2000)
    try:
        rows = await page.evaluate(ROSTER_JS)
    except Exception as e:
        print("roster read failed for", mid, e, file=sys.stderr); rows = []
    # Left team's percentage is the first one printed before "Projected Winner", labeled (Favorite/Underdog) or not (a dead 50/50 has no labels).
    m = re.search(r"(\d+)%[\s\S]{0,40}?Projected Winner", body) or re.search(r"(Favorite|Underdog) (\d+)%", body)
    r = re.search(r"(\d+) Players [Rr]emaining[\s\S]*?Players [Rr]emaining (\d+)", body)
    rem = [int(r.group(1)), int(r.group(2))] if r else None
    # Once both teams are finished, Yahoo swaps to a result layout ("Winner / Result / Loser") that prints neither the odds nor the
    # players-remaining line. Count the remaining starters from the roster instead, so a finished matchup reads 0 and 0, never null.
    if rem is None and rows: rem = [starters_left(rows, "L"), starters_left(rows, "R")]
    pct = int(m.group(m.lastindex)) if m else None
    return pct, rem, body, rows

# Yahoo prints kickoff times in the browser's own clock. A GitHub runner is on UTC, which turned Monday night's 8:15 PM kickoff into
# "Tue 1:15 AM". Pin the browser to Eastern, the clock the NFL schedule is written in, and say so in the files ("tz") so the page
# knows these times need no correcting.
YAHOO_TZ, TZ_LABEL = "America/New_York", "ET"

PREVIEW_V = 3  # bump to force a refresh of next.json after a format change

async def preview_next(out_dir, nweek, max_age_min=20):
    """Snapshot next week's matchups (odds, projections, starters) so the page can stage the upcoming slate. Refreshed at most every max_age_min."""
    path = os.path.join(out_dir, "next.json")
    try:
        old = json.load(open(path))
        age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(old["ts"])).total_seconds() / 60
        if old.get("week") == nweek and old.get("v") == PREVIEW_V and age < max_age_min: return
    except Exception:
        pass
    try:
        _, rows = scoreboard(nweek)
    except Exception as e:
        print("next-week scoreboard failed:", e, file=sys.stderr); return
    if not rows: print("no matchups listed yet for week", nweek, file=sys.stderr); return
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}, timezone_id=YAHOO_TZ)
        page = await ctx.new_page()
        for row in rows:
            try:
                pct, rem, body, rows_ = await odds_for(page, nweek, row["id"])
            except Exception as e:
                print("next-week matchup failed:", row["id"], e, file=sys.stderr); continue
            row.update(leftPct=pct, leftRem=rem[0] if rem else None, rightRem=rem[1] if rem else None)
            if rows_:
                row["shotgun"] = shotgun_entries(rows_, row["left"], row["right"])
                row["leftActive"] = active_starters(rows_, "L"); row["rightActive"] = active_starters(rows_, "R")
        await browser.close()
    json.dump(dict(v=PREVIEW_V, week=nweek, tz=TZ_LABEL, ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), matchups=rows, windows=nfl_windows(nweek)), open(path, "w"), ensure_ascii=False, indent=1)
    print(f"next week {nweek}: {len(rows)} matchups previewed")

def healed_pct(m):
    """A sample's odds for the chart, with the page's heal() already applied: a matchup recorded with both players-remaining counts
    missing, nobody left to play on either side and a score on the board was finished, so it reads 100 or 0 for whoever is ahead.
    Keep in step with heal() in odds/index.html, which still does this for the full-format files."""
    if m.get("leftRem") is None and m.get("rightRem") is None:
        left = lambda a: sum(1 for x in a or [] if x.get("state") in ("live", "pending"))
        ls, rs = m.get("leftScore") or 0, m.get("rightScore") or 0
        if left(m.get("leftActive")) == 0 and left(m.get("rightActive")) == 0 and ls != rs and ls + rs > 0:
            return 100 if ls > rs else 0
    return m.get("leftPct")

def slim(hist):
    """The week as the page needs it. The full history repeats every matchup's starters and shotgun entries in each of about a
    thousand samples, but the page only reads those from the latest one; from the rest it draws the chart, which takes a time and
    the left team's odds. So: everything at the top level as is (week, windows, base, preview), the latest sample in full as "last",
    and "series" with one list of sample times (epoch seconds) and one list of odds per matchup id, index for index (null where a
    sample has no reading for that matchup). "v" is 2; the full format has no "v"."""
    samples = hist.get("samples") or []
    ids = list(dict.fromkeys(m["id"] for s in samples for m in s["matchups"]))
    pct = {i: [] for i in ids}
    for s in samples:
        by = {m["id"]: m for m in s["matchups"]}
        for i in ids: pct[i].append(healed_pct(by[i]) if i in by else None)
    out = {k: v for k, v in hist.items() if k != "samples"}
    out.update(v=2, last=samples[-1] if samples else None,
               series={"ts": [int(datetime.datetime.fromisoformat(s["ts"]).timestamp()) for s in samples], "pct": pct})
    return out

def dump(obj, path):
    """Compact JSON. The indent the files used to carry made them two thirds larger and nothing reads them by eye."""
    with open(path, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))

def write_slim(hist, out_dir, current=True):
    """weekN.lite.json, and live.json when this is the current week. If that fails the files are removed rather than left stale:
    the page falls back to the full files when the slim ones are missing, but it cannot tell an old slim file from a fresh one."""
    paths = [os.path.join(out_dir, f"week{hist['week']}.lite.json")] + ([os.path.join(out_dir, "live.json")] if current else [])
    try:
        lite = slim(hist)
        for p in paths: dump(lite, p)
    except Exception as e:
        print("slim files failed:", e, file=sys.stderr)
        for p in paths:
            if os.path.exists(p): os.remove(p)

def reslim(out_dir):
    """Rebuild the slim files from the week files on disk: a lite file for every weekN.json, and live.json from current.json."""
    for path in sorted(glob.glob(os.path.join(out_dir, "week*.json"))):
        if not re.fullmatch(r"week\d+\.json", os.path.basename(path)): continue
        hist = json.load(open(path, encoding="utf-8")); write_slim(hist, out_dir, current=False)
        print(f"week {hist['week']}: {len(hist.get('samples') or [])} samples, {os.path.getsize(path)} -> {os.path.getsize(os.path.join(out_dir, 'week%d.lite.json' % hist['week']))} bytes")
    cur = os.path.join(out_dir, "current.json")
    if os.path.exists(cur): dump(slim(json.load(open(cur, encoding="utf-8"))), os.path.join(out_dir, "live.json"))
    return 0

async def main(out_dir):
    week, rows = scoreboard()
    await preview_next(out_dir, week + 1)
    meta = week_meta(week)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"week{week}.json")
    hist = json.load(open(path)) if os.path.exists(path) else {"week": week, "samples": []}
    hist.update(meta); hist["tz"] = TZ_LABEL
    # Standings entering the week. Refreshed every pass while the feed is exactly one week behind (stat corrections land midweek);
    # left alone once the feed has moved on, so a finished week keeps the base it was played from.
    try:
        base = standings_base(week)
        if base is None and "base" not in hist:
            prev_path = os.path.join(out_dir, f"week{week-1}.json")
            prev = json.load(open(prev_path)) if os.path.exists(prev_path) else {}
            if prev.get("base", {}).get("through") == week - 2 and prev.get("samples"):
                base = advance_base(prev["base"], prev["samples"][-1]["matchups"])
        if base: hist["base"] = base
    except Exception as e:
        print("standings base failed:", e, file=sys.stderr)
    windows = nfl_windows(week)
    if windows: hist["windows"] = windows
    windows = hist.get("windows")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}, timezone_id=YAHOO_TZ)
        page = await ctx.new_page()
        last_ms = {x["id"]: x for x in (hist["samples"][-1]["matchups"] if hist["samples"] else [])}
        for row in rows:
            # A matchup that was already final in the last sample, with the same scores now, has nothing new to read. Carry it instead
            # of loading its page: Yahoo's result layout prints no odds, so every finished matchup cost three 15 second waits and a
            # week with five of them decided dropped to one sample every 4.5 minutes during Monday night's game.
            done = last_ms.get(row["id"])
            if done and done.get("leftRem") == 0 and done.get("rightRem") == 0 and done.get("leftPct") in (0, 100) \
               and all(round(done.get(k) or 0, 2) == round(row[k], 2) for k in ("leftScore", "rightScore")):
                for k in ("shotgun", "leftActive", "rightActive", "leftPct", "leftRem", "rightRem"): row[k] = done.get(k, [] if k in ("shotgun", "leftActive", "rightActive") else 0)
                continue
            pct, rem, body, rows_ = await odds_for(page, week, row["id"])
            if rows_:
                row["shotgun"] = shotgun_entries(rows_, row["left"], row["right"])
                row["leftActive"] = active_starters(rows_, "L"); row["rightActive"] = active_starters(rows_, "R")
            else:
                prev_m = next((x for x in (hist["samples"][-1]["matchups"] if hist["samples"] else []) if x["id"] == row["id"]), None)
                print("roster unreadable for", row["id"], "- carrying previous", file=sys.stderr)
                for k in ("shotgun", "leftActive", "rightActive"): row[k] = (prev_m or {}).get(k, [])
            if pct is None:
                if rem == [0, 0] or "100%" in body:
                    # Truly decided: nobody left to play, or Yahoo prints 100%. Assign by score.
                    pct = 100 if row["leftScore"] >= row["rightScore"] else 0
                else:
                    # Odds not readable this pass: carry the previous sample's value rather than guessing.
                    prev_m = next((x for x in (hist["samples"][-1]["matchups"] if hist["samples"] else []) if x["id"] == row["id"]), None)
                    if prev_m is None or prev_m.get("leftPct") is None:
                        print("no odds for", row["id"], file=sys.stderr); continue
                    pct = prev_m["leftPct"]; row["stale"] = True
                    print("carried previous odds for", row["id"], file=sys.stderr)
            if rem is None:
                rem = [0, 0] if pct in (0, 100) else [None, None]
            # Guard against a misread percentage: a swing of 40+ points while scores and projections are unchanged is Yahoo's page
            # rendering mid-update, not a real move. Keep the previous reading instead.
            prev_m = next((x for x in (hist["samples"][-1]["matchups"] if hist["samples"] else []) if x["id"] == row["id"]), None)
            if prev_m and prev_m.get("leftPct") is not None and list(rem) != [0, 0] and abs(pct - prev_m["leftPct"]) >= 40 \
               and all(abs(row[k] - prev_m.get(k, row[k])) < 0.5 for k in ("leftScore", "rightScore", "leftProj", "rightProj")):
                print(f"suspicious odds jump for {row['id']}: {prev_m['leftPct']} -> {pct} with no score change; keeping {prev_m['leftPct']}", file=sys.stderr)
                pct = prev_m["leftPct"]; row["stale"] = True
            row.update(leftPct=pct, leftRem=rem[0], rightRem=rem[1])
        await browser.close()
    good = [r for r in rows if "leftPct" in r]
    if len(good) < len(rows):
        print(f"WARNING: {len(rows)-len(good)} matchups missing odds", file=sys.stderr)
    all_done = bool(good) and all(r["leftRem"] == 0 and r["rightRem"] == 0 for r in good)
    def state_of(ms): return {m["id"]: (round(m.get("leftScore") or 0, 2), round(m.get("rightScore") or 0, 2)) for m in ms}
    if all_done and hist["samples"]:
        last = hist["samples"][-1]["matchups"]
        # A finished week is only left alone while its scores hold. Yahoo can show a game as over a beat before its last play is
        # scored (a walk-off field goal moved a defense down a points-allowed tier and flipped a 0.22 point win into a 1.78 point
        # loss after the closing sample), and stat corrections land for days. A moved score falls through and is recorded below.
        if all(r.get("leftRem") == 0 and r.get("rightRem") == 0 for r in last) and state_of(last) == state_of(good):
            # Nothing to write, except the slim files the first time a sampler that knows about them finds a finished week.
            if not all(os.path.exists(os.path.join(out_dir, f)) for f in (f"week{week}.lite.json", "live.json")): write_slim(hist, out_dir)
            print("week already fully decided; no new sample"); return 0
    # History only starts once the week is underway (a starter is live or final). Before kickoff, odds swing with lineup changes,
    # so pre-action runs just refresh a preview snapshot that the page shows in place of samples.
    snapshot = dict(ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), week=week, matchups=good)
    underway = any(a.get("state") in ("live", "final") for r in good for k in ("leftActive", "rightActive", "shotgun") for a in r.get(k, []))
    any_live = any(a.get("state") == "live" for r in good for k in ("leftActive", "rightActive") for a in r.get(k, []))
    # Record while a game is on, or while a window is open once the week is underway; idle runs between windows only refresh the preview.
    # Also record one closing sample when the previous sample still had players live, so finals replace "4th 0:00" readings.
    last_had_live = bool(hist["samples"]) and any(a.get("state") == "live" for r in hist["samples"][-1]["matchups"] for k in ("leftActive", "rightActive") for a in r.get(k, []))
    # Catch-up: if the sampler was down through a game (GitHub outage, broken chain), the first pass back finds scores, odds or
    # players-remaining that moved since the last sample. Record that so the week's finals land even when no window is open.
    # Scores only: odds and players-remaining also move with lineup changes between games, and those are not chart material.
    moved = underway and bool(hist["samples"]) and state_of(hist["samples"][-1]["matchups"]) != state_of(good)
    recorded = any_live or last_had_live or moved or (in_window(windows=windows) and underway)
    if recorded:
        hist["samples"].append(snapshot); hist.pop("preview", None)
    else:
        hist["preview"] = snapshot
    dump(hist, path); dump(hist, os.path.join(out_dir, "current.json"))
    write_slim(hist, out_dir)
    # The workflow keys its cadence off this line: "sample #" means keep sampling every minute, anything else means idle.
    status = f"sample #{len(hist['samples'])}" + (" (catch-up)" if moved and not (any_live or last_had_live) else "") if recorded else ("preview refreshed" if underway else "preview only (week not underway)")
    print(f"week {week}: {status} with {len(good)} matchups; all decided: {all_done}")
    for r in good: print(f"  {r['leftPct']:>3}% {r['left']} vs {r['right']} rem {r['leftRem']}/{r['rightRem']}")
    return 0

def in_window(now=None, windows=None):
    """True while a game window is open. With the week's schedule (nfl_windows) that is 5 minutes before any kickoff slot to its end;
    without it, the fixed fallback: Thu 8 PM to midnight, Sun 1 PM to 1 AM Mon, Mon 8 PM to 1 AM Tue (ET)."""
    now = now or datetime.datetime.now(ET)
    if windows:
        return any(datetime.datetime.fromisoformat(g["start"]) - timedelta(minutes=5) <= now <= datetime.datetime.fromisoformat(g["end"]) for g in windows)
    wd, h = now.weekday(), now.hour + now.minute / 60
    return (wd == 3 and h >= 20) or (wd == 6 and h >= 13) or (wd == 0 and h < 1) or (wd == 0 and h >= 20) or (wd == 1 and h < 1)

def idle_gap(out_dir, now=None, cap=1200):
    """Seconds the workflow's loop may wait after a pass that recorded nothing. A minute while a game window is open: the week's first
    kickoff is not 'underway' until a starter goes live, and a pass that failed should be retried. Otherwise until the next window
    opens (5 minutes before its kickoff), at most 20 minutes, so sampling starts with the game and not up to 20 minutes into it.
    Thanksgiving week has nine windows over six days; every one of them is a cold start."""
    now = now or datetime.datetime.now(ET)
    windows = None
    for f in ("live.json", "current.json"):
        try:
            windows = json.load(open(os.path.join(out_dir, f), encoding="utf-8")).get("windows"); break
        except Exception:
            continue
    if in_window(now, windows): return 60
    opens = [datetime.datetime.fromisoformat(g["start"]) - timedelta(minutes=5) for g in windows or []]
    ahead = [(o - now).total_seconds() for o in opens if o > now]
    return max(60, min(cap, int(min(ahead)))) if ahead else cap

if __name__ == "__main__":
    if "--in-window" in sys.argv:
        ok = in_window(); print("in window" if ok else "outside window"); sys.exit(0 if ok else 1)
    if "--idle-gap" in sys.argv:
        print(idle_gap(next((a for a in sys.argv[1:] if not a.startswith("--")), "data/odds"))); sys.exit(0)
    if "--slim" in sys.argv:
        sys.exit(reslim(next((a for a in sys.argv[1:] if not a.startswith("--")), "data/odds")))
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "data/odds")))
