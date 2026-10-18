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

def scoreboard():
    rz = json.loads(get(f"https://pub-api.fantasysports.yahoo.com/fantasy/v3/redzone/nfl?league_id={LEAGUE}&format=json"))
    L = rz["service"]["leagues"][str(LEAGUE)]; week = int(L["weekInfo"]["week"])
    pairs = [tuple(p) for p in L["matchupGroups"][0]["matchups"]]
    t = text(get(f"{BASE}?matchup_week={week}&module=matchups&lhst=matchups"))
    i = t.find(f"Week {week} Matchups"); j = t.find("Standings", i)
    rows = []
    rx = r"View Matchup (.*?) (\d+)-(\d+)-\d+ \| \w+ ([\d.]+) ([\d.]+) vs ([\d.]+) ([\d.]+) (.*?) (\d+)-(\d+)-\d+ \|"
    for (a, b), m in zip(pairs, re.finditer(rx, t[i:j])):
        n1, w1, l1, c1, p1, c2, p2, n2, w2, l2 = m.groups()
        rows.append(dict(id=f"{a}v{b}", left=n1, leftRec=f"{w1}-{l1}", right=n2, rightRec=f"{w2}-{l2}",
                         leftScore=float(c1), leftProj=float(p1), rightScore=float(c2), rightProj=float(p2)))
    return week, rows


ROSTER_JS = """() => {
  const t = document.querySelector('table'); if (!t) return [];
  const out = [];
  for (const r of t.querySelectorAll('tr')) {
    const c = [...r.querySelectorAll('th,td')]; if (c.length !== 9) continue;
    const slot = c[4].innerText.trim(); if (slot === 'Pos' || slot === 'TOTAL') continue;
    const cell = td => { const a = td.querySelector('a'); return [a ? a.innerText.trim() : '', td.innerText.replace(/\\s+/g, ' ').trim()]; };
    out.push({slot, L: cell(c[1]), lp: c[3].innerText.trim(), R: cell(c[7]), rp: c[5].innerText.trim()});
  }
  return out;
}"""

def parse_player(name, text, pts):
    rest = text[len(name):].strip() if name and text.startswith(name) else text
    m = re.match(r"([A-Za-z]{2,3}) - ([A-Z/,]+)\s*(.*)", rest)
    team, pos, game = m.groups() if m else ("", "", rest)
    tag = None
    t = re.match(r"(IR-R|IR|PUP|SUSP|NFI|DTD|NA|O|Q|D|P)\s+(.*)", game)
    if t: tag, game = t.groups()
    if re.match(r"Final", game): state = "final"
    elif re.match(r"(1st|2nd|3rd|4th|OT|Half|Delay)", game): state = "live"
    elif "No Game" in game: state = "bye"
    elif not name: state = "empty"
    else: state = "pending"
    p = float(pts) if re.match(r"-?\d", pts) else 0.0
    return dict(player=name or "(Empty)", pos=pos, team=team, pts=p, game=game.strip(), tag=tag, state=state)

def shotgun_entries(rows, left_team, right_team):
    """Starters at zero or below whose fate is in play: live or final games, inactive tags, byes, empty slots."""
    out = []
    for r in rows:
        for side, who in (("L", left_team), ("R", right_team)):
            name, text = r[side]; pts = r["lp"] if side == "L" else r["rp"]
            d = parse_player(name, text, pts)
            if d["pts"] > 0: continue
            inactive = d["tag"] in ("O", "IR", "IR-R", "PUP", "SUSP", "NFI", "NA")
            if d["state"] == "pending" and not inactive: continue
            d.update(slot=r["slot"], manager=who, locked=d["state"] == "final")
            out.append(d)
    return out

async def odds_for(page, week, mid):
    a, b = mid.split("v")
    await page.goto(f"{BASE}/matchup?week={week}&mid1={a}&mid2={b}", wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_function("() => /(Favorite|Underdog) \\d+%|Projected Winner|Loser/.test(document.body.innerText)", timeout=20000)
    except Exception:
        pass
    body = await page.inner_text("body")
    try:
        rows = await page.evaluate(ROSTER_JS)
    except Exception as e:
        print("roster read failed for", mid, e, file=sys.stderr); rows = []
    m = re.search(r"(Favorite|Underdog) (\d+)%", body)
    r = re.search(r"(\d+) Players [Rr]emaining[\s\S]*?Players [Rr]emaining (\d+)", body)
    rem = [int(r.group(1)), int(r.group(2))] if r else None
    pct = int(m.group(2)) if m else None
    return pct, rem, body, rows

async def main(out_dir):
    week, rows = scoreboard()
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
            row["shotgun"] = shotgun_entries(rows_, row["left"], row["right"])
            if pct is None:
                # decided matchup: no percentage shown. Use scores to assign 100/0; fall back to projections.
                if "Winner" in body or "Loser" in body or (rem == [0, 0]):
                    pct = 100 if row["leftScore"] >= row["rightScore"] else 0
                else:
                    print("no odds for", row["id"], file=sys.stderr); continue
            if rem is None:
                rem = [0, 0] if pct in (0, 100) else [None, None]
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
    hist["samples"].append(dict(ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), week=week, matchups=good))
    json.dump(hist, open(path, "w"), ensure_ascii=False, indent=1)
    json.dump(hist, open(os.path.join(out_dir, "current.json"), "w"), ensure_ascii=False, indent=1)
    print(f"week {week}: sample #{len(hist['samples'])} with {len(good)} matchups; all decided: {all_done}")
    for r in good: print(f"  {r['leftPct']:>3}% {r['left']} vs {r['right']} rem {r['leftRem']}/{r['rightRem']}")
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "data/odds")))
