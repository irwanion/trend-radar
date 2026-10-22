#!/usr/bin/env python3
"""Trend Radar harvester.
Asks Google Trends (Indonesia) how interest in each keyword in config.json is moving,
scores every keyword, and writes the best 8 to docs/data/. Run: python harvester/harvest.py
Add --demo to create fake data (no internet needed) to preview the dashboard.
"""
import datetime as dt, json, os, random, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "harvester" / "config.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "data"
DEMO = "--demo" in sys.argv
COMP = {"low": 1.0, "medium": 0.55, "high": 0.2}
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-flash-preview"]  # tried in this order, first one that works wins
CLAUDE_MODEL = "claude-haiku-5-5"  # only used if you set ANTHROPIC_API_KEY instead
# Describes your videos to the AI. To change it, add a "video_brief" line to config.json.
BRIEF = CFG.get("video_brief") or (
    "Videos are 15-30 second vertical clips for Shopee Video and YouTube Shorts. The creator never appears "
    "on camera: every shot is made with an AI video generator (Kling), so describe only shots such a tool "
    "does well (products, hands, kitchens, rooms, food, close-ups). The voiceover is casual Bahasa Indonesia.")


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


def call_gemini(prompt):
    from google import genai
    from google.genai import types
    client = genai.Client()  # reads your key from the GEMINI_API_KEY secret
    names = [n for n in [os.environ.get("GEMINI_MODEL")] + GEMINI_MODELS if n]
    last = None
    for name in names:
        try:
            r = client.models.generate_content(model=name, contents=prompt,
                                               config=types.GenerateContentConfig(response_mime_type="application/json"))
            return r.text, name
        except Exception as e:
            print(f"Gemini model {name} did not work: {e}")
            last = e
    raise last


def call_claude(prompt):
    import anthropic  # reads your key from the ANTHROPIC_API_KEY secret
    msg = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text"), CLAUDE_MODEL


def call_model(prompt):
    """Returns (answer text, model name). Gemini is used when GEMINI_API_KEY is set, otherwise Claude."""
    return call_gemini(prompt) if os.environ.get("GEMINI_API_KEY") else call_claude(prompt)


AN_KEYS = ["name", "category", "function", "selling_points", "visual_details", "target_customer"]


def make_prompt(items):
    lines = "\n".join(json.dumps(dict(keyword=i["keyword"], category=i["category"], why_it_ranks=i["why"],
                                      people_also_search=i["related"], upcoming_event=i["upcoming"]),
                                 ensure_ascii=False) for i in items)
    return ("You write short-video ideas for an Indonesian Shopee affiliate.\n\n"
            f"{BRIEF}\n\n"
            "HOW TO ANSWER (this part decides the format):\n"
            "For EACH product below write:\n"
            "- analysis: an object with the keys name, category, function, selling_points (2-3 items), visual_details, "
            "target_customer. Short, Bahasa Indonesia\n"
            "- hook: one punchy opening line, max 12 words, Bahasa Indonesia\n"
            "- script: exactly 4 short beats (strings), each starting with its time range, Bahasa Indonesia, following the "
            "script outline above; the last beat is the call to check the yellow cart (keranjang kuning)\n"
            "- prompt: one English paragraph for the AI video generator (Kling), vertical 9:16, no text and no faces in the shot\n\n"
            "Rules: you only get search keywords. You cannot see product images, so never claim you did. "
            "Do not invent brand names, discounts, specifications, health claims or reviews. "
            "You do not know the real price or size, so where the outline asks for price or size, write the placeholder "
            "[harga] or [ukuran] for the creator to fill in. If upcoming_event is set, use it naturally.\n"
            'Return ONLY a JSON list, nothing else: [{"keyword": "...", "analysis": {...}, "hook": "...", '
            '"script": ["...", "...", "...", "..."], "prompt": "..."}]\n\n'
            f"Products, one per line:\n{lines}")


def parse_ideas(text, keywords, model):
    data = json.loads(text[text.find("["):text.rfind("]") + 1])
    ok = {}
    for d in data:
        s = d.get("script")
        if (d.get("keyword") in keywords and isinstance(d.get("hook"), str) and isinstance(d.get("prompt"), str)
                and isinstance(s, list) and s and all(isinstance(x, str) for x in s)):
            idea = dict(hook=d["hook"].strip()[:200], script=[x.strip()[:300] for x in s[:6]],
                        prompt=d["prompt"].strip()[:800], model=model)
            a = d.get("analysis")
            if isinstance(a, dict):
                an = {}
                for k in AN_KEYS:
                    v = a.get(k)
                    v = ", ".join(map(str, v)) if isinstance(v, list) else str(v or "")
                    if v.strip():
                        an[k] = v.strip()[:300]
                if an:
                    idea["analysis"] = an
            ok[d["keyword"]] = idea
    return ok


def add_ideas(items):
    """Asks an AI model for a hook, script and video prompt per product. Never stops the daily harvest."""
    if DEMO or not (os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        print("Video ideas skipped (no GEMINI_API_KEY set). The dashboard uses its starter templates.")
        return
    try:
        text, model = call_model(make_prompt(items))
        ideas = parse_ideas(text, {i["keyword"] for i in items}, model)
        for it in items:
            if it["keyword"] in ideas:
                it["ideas"] = ideas[it["keyword"]]
        print(f"Video ideas written for {len(ideas)} of {len(items)} products ({model}).")
    except Exception as e:
        print("Video ideas failed, the harvest continues without them:", e)


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
    add_ideas(items)
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
