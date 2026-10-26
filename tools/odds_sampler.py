"""Sample Yahoo win odds for every matchup in the Harry Koch Memorial Cup and append to a JSON history.
Runs in GitHub Actions with Playwright (the live win percentage only renders in a browser).
Usage: python tools/odds_sampler.py <out_dir>
"""
import asyncio, datetime, html, json, os, re, sys, urllib.request
from datetime import timedelta
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

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

def current_week():
    rz = json.loads(get(f"https://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id={LEAGUE}&format=json"))
    return int(rz["service"]["leagues"][str(LEAGUE)]["weekInfo"]["week"])

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
    pct = int(m.group(m.lastindex)) if m else None
    return pct, rem, body, rows

PREVIEW_V = 2  # bump to force a refresh of next.json after a format change

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
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
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
    json.dump(dict(v=PREVIEW_V, week=nweek, ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), matchups=rows), open(path, "w"), ensure_ascii=False, indent=1)
    print(f"next week {nweek}: {len(rows)} matchups previewed")

async def main(out_dir):
    week, rows = scoreboard()
    await preview_next(out_dir, week + 1)
    meta = week_meta(week)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"week{week}.json")
    hist = json.load(open(path)) if os.path.exists(path) else {"week": week, "samples": []}
    hist.update(meta)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        for row in rows:
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
            if prev_m and prev_m.get("leftPct") is not None and abs(pct - prev_m["leftPct"]) >= 40 \
               and all(abs(row[k] - prev_m.get(k, row[k])) < 0.5 for k in ("leftScore", "rightScore", "leftProj", "rightProj")):
                print(f"suspicious odds jump for {row['id']}: {prev_m['leftPct']} -> {pct} with no score change; keeping {prev_m['leftPct']}", file=sys.stderr)
                pct = prev_m["leftPct"]; row["stale"] = True
            row.update(leftPct=pct, leftRem=rem[0], rightRem=rem[1])
        await browser.close()
    good = [r for r in rows if "leftPct" in r]
    if len(good) < len(rows):
        print(f"WARNING: {len(rows)-len(good)} matchups missing odds", file=sys.stderr)
    all_done = bool(good) and all(r["leftRem"] == 0 and r["rightRem"] == 0 for r in good)
    if all_done and hist["samples"]:
        last = hist["samples"][-1]["matchups"]
        if all(r.get("leftRem") == 0 and r.get("rightRem") == 0 for r in last):
            print("week already fully decided; no new sample"); return 0
    # History only starts once the week is underway (a starter is live or final). Before kickoff, odds swing with lineup changes,
    # so pre-action runs just refresh a preview snapshot that the page shows in place of samples.
    snapshot = dict(ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), week=week, matchups=good)
    underway = any(a.get("state") in ("live", "final") for r in good for k in ("leftActive", "rightActive", "shotgun") for a in r.get(k, []))
    any_live = any(a.get("state") == "live" for r in good for k in ("leftActive", "rightActive") for a in r.get(k, []))
    # Record while a game is on, or while a window is open once the week is underway; idle runs between windows only refresh the preview.
    # Also record one closing sample when the previous sample still had players live, so finals replace "4th 0:00" readings.
    last_had_live = bool(hist["samples"]) and any(a.get("state") == "live" for r in hist["samples"][-1]["matchups"] for k in ("leftActive", "rightActive") for a in r.get(k, []))
    if any_live or last_had_live or (in_window() and underway):
        hist["samples"].append(snapshot); hist.pop("preview", None)
    else:
        hist["preview"] = snapshot
    json.dump(hist, open(path, "w"), ensure_ascii=False, indent=1)
    json.dump(hist, open(os.path.join(out_dir, "current.json"), "w"), ensure_ascii=False, indent=1)
    print(f"week {week}: {'sample #' + str(len(hist['samples'])) if underway else 'preview only (week not underway)'} with {len(good)} matchups; all decided: {all_done}")
    for r in good: print(f"  {r['leftPct']:>3}% {r['left']} vs {r['right']} rem {r['leftRem']}/{r['rightRem']}")
    return 0

def in_window(now=None):
    """True during NFL game windows (ET): Thu 8 PM to midnight, Sun 1 PM to 1 AM Mon, Mon 8 PM to midnight. Used to self-chain runs."""
    now = now or datetime.datetime.now(ET)
    wd, h = now.weekday(), now.hour + now.minute / 60
    return (wd == 3 and h >= 20) or (wd == 6 and h >= 13) or (wd == 0 and h < 1) or (wd == 0 and h >= 20)

if __name__ == "__main__":
    if "--in-window" in sys.argv:
        ok = in_window(); print("in window" if ok else "outside window"); sys.exit(0 if ok else 1)
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "data/odds")))
