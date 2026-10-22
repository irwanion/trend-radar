#!/usr/bin/env python3
"""Trend Radar harvester.
Asks Google Trends (Indonesia) how interest in each keyword in config.json is moving,
scores every keyword, and writes the best 8 to docs/data/. Run: python harvester/harvest.py
Add --demo to create fake data (no internet needed) to preview the dashboard.
"""
import datetime as dt, json, random, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "harvester" / "config.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "data"
DEMO = "--demo" in sys.argv
COMP = {"low": 1.0, "medium": 0.55, "high": 0.2}


def mean(x):
    return sum(x) / len(x) if x else 0.0


def fetch_live(keywords, anchor):
    """Google Trends only compares 5 terms at once, so every batch carries the same
    'anchor' term; all results are expressed relative to it so they can be compared."""
    from pytrends.request import TrendReq
    py = TrendReq(hl="id-ID", tz=420, timeout=(10, 30))
    out = {}
    for i in range(0, len(keywords), 4):
        batch = [k for k in keywords[i:i + 4] if k != anchor]
        for attempt in range(4):
            try:
                py.build_payload(batch + [anchor], timeframe="today 3-m", geo="ID")
                df = py.interest_over_time()
                if df.empty:
                    raise ValueError("empty answer")
                if "isPartial" in df:
                    df = df[df["isPartial"].astype(str) != "True"]
                am = mean(df[anchor].astype(float).tolist())
                try:
                    rq = py.related_queries()
                except Exception:
                    rq = {}
                for k in batch:
                    rising = []
                    r = (rq.get(k) or {}).get("rising")
                    if r is not None and len(r):
                        rising = [q for q in r["query"].tolist() if q != k][:3]
                    if am > 0:
                        out[k] = {"series": [v / am * 100 for v in df[k].astype(float)], "related": rising}
                print("ok:", batch)
                break
            except Exception as e:
                wait = 25 * (attempt + 1)
                print(f"retry {attempt + 1} for {batch}: {e} (waiting {wait}s)")
                time.sleep(wait)
        time.sleep(random.uniform(6, 12))
    return out


def fetch_demo(keywords):
    random.seed(dt.date.today().toordinal())
    out = {}
    for k in keywords:
        base, drift = random.uniform(5, 60), random.uniform(-0.4, 1.2)
        s = [max(0, base * (1 + drift * i / 90) + random.gauss(0, base * 0.12)) for i in range(90)]
        out[k] = {"series": s, "related": [k + " murah", k + " terbaik"]}
    return out


def pct_rank(vals):
    order = sorted(vals)
    n = max(len(vals) - 1, 1)
    return [order.index(v) / n for v in vals]


def upcoming_event(tags, today):
    best = None
    for e in CFG["events"]:
        days = (dt.date.fromisoformat(e["date"]) - today).days
        if 0 <= days <= CFG["event_window_days"] and ("all" in e["tags"] or set(tags) & set(e["tags"])):
            if best is None or days < best[1]:
                best = (e["name"], days)
    return best


def build_items(raw, today):
    rows = []
    for cat in CFG["categories"]:
        for kw, comm, comp, content, tags in cat["keywords"]:
            d = raw.get(kw)
            if not d or len(d["series"]) < 35:
                continue
            s = d["series"]
            recent, base, level = mean(s[-7:]), mean(s[-28:-7]), mean(s[-14:])
            growth = max(-1, min(3, (recent - base) / base)) if base >= 1 else 0.0
            spark = [round(mean(s[len(s) - 7 * (i + 1):len(s) - 7 * i]), 1) for i in range(12)][::-1]
            rows.append(dict(kw=kw, cat=cat["name"], comm=comm, comp=comp, content=content, tags=tags,
                             level=level, growth=growth, spark=spark, related=d["related"],
                             event=upcoming_event(tags, today)))
    if not rows:
        return []
    pl, pg = pct_rank([r["level"] for r in rows]), pct_rank([r["growth"] for r in rows])
    items = []
    for r, a, b in zip(rows, pl, pg):
        ev = r["event"]
        season = 1 - ev[1] / CFG["event_window_days"] if ev else 0
        parts = dict(demand=a * 20, growth=b * 30 * (1 if r["growth"] > 0 else 0.3),
                     commission=min(r["comm"], 10) / 10 * 15, competition=COMP[r["comp"]] * 15,
                     content=r["content"] / 5 * 10, season=season * 10)
        rising = r["growth"] >= 0.25 and r["level"] >= 1
        why = []
        if rising:
            why.append(f"Searches up {round(r['growth'] * 100)}% vs the previous 3 weeks")
        if r["comp"] == "low":
            why.append("Low competition (your estimate)")
        if r["comm"] >= 8:
            why.append(f"Good commission (~{r['comm']}%, your estimate)")
        if r["content"] >= 5:
            why.append("Very easy to show in a short video")
        if ev:
            why.append(f"{ev[0]} in {ev[1]} days")
        items.append(dict(keyword=r["kw"], category=r["cat"], score=round(sum(parts.values())),
                          parts={k: round(v, 1) for k, v in parts.items()},
                          growth_pct=round(r["growth"] * 100), rising=rising,
                          upcoming=ev[0] if ev else None, spark=r["spark"], related=r["related"],
                          commission=r["comm"], competition=r["comp"], content=r["content"], why=why))
    items.sort(key=lambda x: -x["score"])
    picked, per = [], {}
    for it in items:
        if per.get(it["category"], 0) < CFG["max_per_category"] and len(picked) < CFG["max_items_per_day"]:
            picked.append(it)
            per[it["category"]] = per.get(it["category"], 0) + 1
    for n, it in enumerate(picked, 1):
        it["rank"] = n
    return picked


def main():
    today = dt.date.today()
    kws = list(dict.fromkeys(k[0] for c in CFG["categories"] for k in c["keywords"]))
    raw = fetch_demo(kws) if DEMO else fetch_live(kws, CFG["anchor"])
    items = build_items(raw, today)
    if not items:
        sys.exit("No data came back from Google Trends. Nothing was changed. Try again later.")
    OUT.mkdir(parents=True, exist_ok=True)
    payload = dict(date=today.isoformat(), generated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   demo=DEMO, items=items)
    (OUT / f"{today.isoformat()}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    dates = sorted((p.stem for p in OUT.glob("20??-??-??.json")), reverse=True)
    for old in dates[CFG["history_days"]:]:
        (OUT / f"{old}.json").unlink()
    (OUT / "index.json").write_text(json.dumps(dict(dates=dates[:CFG["history_days"]])), encoding="utf-8")
    print(f"Saved {len(items)} products for {today}.")


if __name__ == "__main__":
    main()
