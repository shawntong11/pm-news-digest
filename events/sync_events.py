# 从 ../market_calendar/sync_events.py 复制而来，供云端（GitHub Actions）同步事件日历。
"""Pull upcoming US market events into the events table.

Sources (no key needed unless noted):
  - Federal Reserve: FOMC meeting schedule
  - BEA: GDP and PCE (Personal Income and Outlays) release dates
  - Forex Factory weekly feed: this week's US data with forecast / previous
  - Nasdaq: earnings dates for large caps and the watchlist
  - FRED (optional, FRED_API_KEY): CPI, jobs report, PPI, retail sales, JOLTS dates
  - Computed: monthly options expiration / quad witching

Run directly (`python sync_events.py`) or let app.py call sync_all() on a timer.
"""
import calendar
import os
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays
import requests

BASE = Path(__file__).parent
DB_PATH = BASE / "calendar.db"
ET = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "Mozilla/5.0 (MarketLog personal use)"}

EARNINGS_DAYS_AHEAD = 45
EARNINGS_MIN_CAP = 100e9           # show every company at least this big
WATCHLIST = {                       # always show these, whatever their size
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD",
    "NFLX", "JPM", "GS", "COIN", "PLTR", "MSTR",
}


def load_env():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def ev(d: date, title, category, source, importance=2, time_=None, note="", topic=None):
    return {"date": d.isoformat(), "title": title, "category": category, "source": source,
            "importance": importance, "time": time_, "note": note, "topic": topic}


# ---------- Federal Reserve ----------

MONTHS = {m: i for i, m in enumerate(calendar.month_name) if m}


def fetch_fomc():
    html = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                        headers=HEADERS, timeout=20).text
    out = []
    # each year has its own panel: "<a id=...>2026 FOMC Meetings</a>"
    parts = re.split(r">(\d{4}) FOMC Meetings<", html)
    for year_s, body in zip(parts[1::2], parts[2::2]):
        year = int(year_s)
        if year < date.today().year - 1:
            continue
        months = re.findall(r'fomc-meeting__month[^"]*"><strong>([^<]+)</strong>', body)
        dates = re.findall(r'fomc-meeting__date[^"]*">([^<]+)<', body)
        for mon, days in zip(months, dates):
            if "notation" in days.lower() or "unscheduled" in mon.lower():
                continue
            sep = "*" in days
            m = re.match(r"(\d+)(?:-(\d+))?", days.strip())
            if not m:
                continue
            # "April/May" + "30-1" -> decision day falls in the second month
            mon_names = mon.split("/")
            last_mon = MONTHS.get(mon_names[-1].strip())
            if not last_mon:
                continue
            last_day = int(m.group(2) or m.group(1))
            try:
                d = date(year, last_mon, last_day)
            except ValueError:
                continue
            note = "会后发布经济预测（SEP）和点阵图；主席记者会 14:30" if sep else "主席记者会 14:30"
            out.append(ev(d, "FOMC 利率决议" + ("（含点阵图）" if sep else ""), "fed", "fed",
                          3, "14:00", note, topic="fomc"))
    return out


# ---------- BEA (GDP, PCE) ----------

def parse_ics(text):
    text = text.replace("\r\n", "\n").replace("\n ", "")  # unfold long lines
    for block in text.split("BEGIN:VEVENT")[1:]:
        fields = {}
        for line in block.split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.split(";")[0]] = v
        yield fields


def fetch_bea():
    text = requests.get("https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics",
                        headers=HEADERS, timeout=20).text
    out = []
    for f in parse_ics(text):
        summary = f.get("SUMMARY", "").replace("\\,", ",")
        raw = f.get("DTSTART", "")
        try:
            dt = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
        except ValueError:
            continue
        if raw.endswith("Z"):
            dt = dt.replace(tzinfo=timezone.utc).astimezone(ET)
        d = dt.date()
        if d < date.today() - timedelta(days=60):
            continue
        hhmm = dt.strftime("%H:%M")
        is_gdp = (summary.startswith("Gross Domestic Product,")
                  or re.match(r"GDP \((Advance|Second|Third) Estimate\)", summary))
        if is_gdp:
            stage = ("初值" if "Advance" in summary else "修正值" if "Second" in summary
                     else "终值" if "Third" in summary else "")
            q = re.search(r"(\d)(?:st|nd|rd|th) Quarter.*?(\d{4})", summary)
            label = f"{q.group(2)}Q{q.group(1)} " if q else ""
            out.append(ev(d, f"GDP {label}{stage}".strip(), "macro", "bea", 3, hhmm, summary, "gdp"))
        elif summary.startswith("Personal Income and Outlays"):
            out.append(ev(d, "PCE 物价 / 个人收支", "macro", "bea", 3, hhmm,
                          summary + "（美联储最看重的通胀指标）", "pce"))
    return out


# ---------- Forex Factory weekly feed ----------

FF_TOPICS = [  # (pattern, topic, Chinese group title)
    (r"\bCPI\b", "cpi", "CPI 通胀数据"),
    (r"Non-Farm Employment Change|Unemployment Rate|Average Hourly Earnings", "nfp", "非农就业报告"),
    (r"^ADP", None, None),
    (r"\bPPI\b", "ppi", "PPI 生产者物价"),
    (r"Retail Sales", "retail", "零售销售"),
    (r"JOLTS", "jolts", "JOLTS 职位空缺"),
    (r"GDP", "gdp", "GDP"),
    (r"PCE|Personal (Income|Spending)", "pce", "PCE 物价 / 个人收支"),
    (r"Federal Funds Rate|FOMC Statement|FOMC Press Conference", "fomc", "FOMC 利率决议"),
]
FF_ZH = {
    "ISM Manufacturing PMI": "ISM 制造业 PMI", "ISM Services PMI": "ISM 服务业 PMI",
    "Unemployment Claims": "初请失业金", "ADP Non-Farm Employment Change": "ADP 就业",
    "Prelim UoM Consumer Sentiment": "密歇根消费者信心（初值）",
    "Revised UoM Consumer Sentiment": "密歇根消费者信心（终值）",
    "Prelim UoM Inflation Expectations": "密歇根通胀预期（初值）",
    "Revised UoM Inflation Expectations": "密歇根通胀预期（终值）",
    "CB Consumer Confidence": "世企研究会消费者信心", "FOMC Meeting Minutes": "FOMC 会议纪要",
    "Congressional Elections": "美国国会选举", "Presidential Election": "美国总统选举",
    "Crude Oil Inventories": "EIA 原油库存", "Empire State Manufacturing Index": "纽约联储制造业指数",
    "Philly Fed Manufacturing Index": "费城联储制造业指数", "Durable Goods Orders m/m": "耐用品订单",
    "Core Durable Goods Orders m/m": "核心耐用品订单", "Building Permits": "营建许可",
    "Housing Starts": "新屋开工", "New Home Sales": "新屋销售", "Existing Home Sales": "成屋销售",
    "Pending Home Sales m/m": "成屋签约销售", "Flash Manufacturing PMI": "标普制造业 PMI（初值）",
    "Flash Services PMI": "标普服务业 PMI（初值）", "Trade Balance": "贸易差额",
    "Prelim Nonfarm Productivity q/q": "非农生产率（初值）", "Industrial Production m/m": "工业产出",
    "Jackson Hole Symposium": "杰克逊霍尔年会", "Treasury Currency Report": "财政部汇率报告",
}


def ff_title(t):
    if m := re.match(r"(?:Fed Chair|FOMC Member) (\w+) (?:Speaks|Testifies)", t):
        return ("美联储主席 " if t.startswith("Fed Chair") else "美联储官员 ") + m.group(1) + " 讲话"
    return FF_ZH.get(t, t)


def fetch_forexfactory():
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                     headers=HEADERS, timeout=20)
    if r.status_code == 429:
        raise RuntimeError("请求太频繁被限流（429），稍后自动重试")
    r.raise_for_status()
    rows = r.json()
    out, groups = [], defaultdict(list)
    for r in rows:
        if r.get("country") != "USD" or r.get("impact") not in ("High", "Medium"):
            continue
        dt = datetime.fromisoformat(r["date"]).astimezone(ET)
        imp = 3 if r["impact"] == "High" else 2
        detail = r["title"]
        if r.get("forecast") or r.get("previous"):
            detail += f"：预期 {r.get('forecast') or '—'}，前值 {r.get('previous') or '—'}"
        topic, group_title = None, None
        for pat, tp, gt in FF_TOPICS:
            if re.search(pat, r["title"]):
                topic, group_title = tp, gt
                break
        if topic:
            groups[(dt.date(), topic)].append((dt, imp, group_title, detail))
        else:
            out.append(ev(dt.date(), ff_title(r["title"]), "macro", "ff", imp,
                          dt.strftime("%H:%M"), detail))
    # several lines of one release (CPI m/m, y/y, core...) become one event
    for (d, topic), items in groups.items():
        items.sort()
        cat = "fed" if topic == "fomc" else "macro"
        out.append(ev(d, items[0][2], cat, "ff", max(i[1] for i in items),
                      items[0][0].strftime("%H:%M"), "\n".join(i[3] for i in items), topic))
    return out


# ---------- FRED (optional key) ----------

FRED_RELEASES = {  # release_id: (title, topic, importance, time)
    10: ("CPI 通胀数据", "cpi", 3, "08:30"),
    50: ("非农就业报告", "nfp", 3, "08:30"),
    46: ("PPI 生产者物价", "ppi", 2, "08:30"),
    9: ("零售销售", "retail", 2, "08:30"),
    192: ("JOLTS 职位空缺", "jolts", 2, "10:00"),
}


def fetch_fred():
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return None  # not configured
    out = []
    start = (date.today() - timedelta(days=60)).isoformat()
    end = (date.today() + timedelta(days=400)).isoformat()
    for rid, (title, topic, imp, t) in FRED_RELEASES.items():
        r = requests.get("https://api.stlouisfed.org/fred/release/dates", params={
            "release_id": rid, "api_key": key, "file_type": "json",
            "realtime_start": start, "realtime_end": end,
            "include_release_dates_with_no_data": "true", "limit": 1000,
        }, timeout=20)
        r.raise_for_status()
        for row in r.json().get("release_dates", []):
            out.append(ev(date.fromisoformat(row["date"]), title, "macro", "fred", imp, t, "", topic))
    return out


# ---------- Nasdaq earnings ----------

def _cap(s):
    try:
        return float(re.sub(r"[^\d.]", "", s or "") or 0)
    except ValueError:
        return 0.0


def fetch_earnings():
    out = []
    nyse = holidays.financial_holidays("NYSE", years=[date.today().year, date.today().year + 1])
    d = date.today() - timedelta(days=3)
    end = date.today() + timedelta(days=EARNINGS_DAYS_AHEAD)
    while d <= end:
        if d.weekday() < 5 and d not in nyse:
            r = requests.get("https://api.nasdaq.com/api/calendar/earnings",
                             params={"date": d.isoformat()}, headers=HEADERS, timeout=20)
            r.raise_for_status()
            rows = ((r.json().get("data") or {}).get("rows")) or []
            for row in rows:
                sym, cap = row.get("symbol", ""), _cap(row.get("marketCap"))
                if cap < EARNINGS_MIN_CAP and sym not in WATCHLIST:
                    continue
                when = {"time-pre-market": "盘前", "time-after-hours": "盘后"}.get(row.get("time"), "")
                imp = 3 if cap >= 1e12 else 2
                note = (f"{row.get('name', '')} · 市值 ${cap/1e9:,.0f}B · EPS 预期 {row.get('epsForecast') or '—'}"
                        f" · 财季 {row.get('fiscalQuarterEnding', '')}")
                out.append(ev(d, f"{sym} 财报" + (f"（{when}）" if when else ""), "earnings",
                              "nasdaq", imp, None, note))
            time.sleep(0.3)
        d += timedelta(days=1)
    return out


# ---------- computed: options expiration ----------

def options_expirations():
    out = []
    nyse = holidays.financial_holidays("NYSE", years=[date.today().year, date.today().year + 1])
    first = date.today().replace(day=1)
    for i in range(14):
        y, m = first.year + (first.month - 1 + i) // 12, (first.month - 1 + i) % 12 + 1
        d = date(y, m, 1)
        d += timedelta(days=(4 - d.weekday()) % 7 + 14)   # third Friday
        if d in nyse:
            d -= timedelta(days=1)
        quad = m in (3, 6, 9, 12)
        out.append(ev(d, "四巫日（季度期权期货交割）" if quad else "月度期权到期", "market", "calc",
                      3 if quad else 1, "16:00", "当天成交量和波动可能放大"))
    return out


# ---------- write to DB ----------

SOURCES = [  # (name, fetcher, coverage: "future" or "range")
    ("fed", fetch_fomc, "future"),
    ("bea", fetch_bea, "future"),
    ("fred", fetch_fred, "future"),
    ("ff", fetch_forexfactory, "range"),
    ("nasdaq", fetch_earnings, "range"),
    ("calc", options_expirations, "future"),
]
MIN_INTERVAL = {"ff": 3600, "nasdaq": 3600}  # seconds between fetches of slow / rate-limited sources
PRIORITY = {"ff": 0, "fred": 1, "bea": 2, "fed": 3}  # lower wins on same date + topic


def rank(topic, src):
    # the Fed's own schedule knows about the dot plot, so it wins for FOMC
    if topic == "fomc" and src == "fed":
        return -1
    return PRIORITY.get(src, 9)


def ensure_schema(conn):
    # 云端从空数据库开始，先建表（结构与 market_calendar/app.py 一致）
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, title TEXT NOT NULL,
        category TEXT DEFAULT 'other', source TEXT DEFAULT 'manual', note TEXT)""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    for col, decl in [("ext_key", "TEXT"), ("importance", "INTEGER DEFAULT 2"),
                      ("time", "TEXT"), ("topic", "TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ev_ext ON events(ext_key)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")


def sync_all(force=False):
    load_env()
    report = {}
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        ensure_schema(conn)
        today = date.today().isoformat()
        for name, fetch, coverage in SOURCES:
            last = conn.execute("SELECT value FROM meta WHERE key=?", (f"fetched_at:{name}",)).fetchone()
            if not force and last and time.time() - float(last[0]) < MIN_INTERVAL.get(name, 0):
                report[name] = "1 小时内已更新"
                continue
            try:
                items = fetch()
            except Exception as e:  # one broken source shouldn't stop the rest
                report[name] = f"失败：{type(e).__name__}: {e}"[:200]
                continue
            if items is None:
                report[name] = "未配置"
                continue
            keys = set()
            for it in items:
                key = f"{name}:{it['date']}:{it['title']}"
                keys.add(key)
                conn.execute(
                    """INSERT INTO events (date, title, category, source, note, ext_key, importance, time, topic)
                       VALUES (:date, :title, :category, :source, :note, :key, :importance, :time, :topic)
                       ON CONFLICT(ext_key) DO UPDATE SET category=excluded.category, note=excluded.note,
                         importance=excluded.importance, time=excluded.time, topic=excluded.topic""",
                    {**it, "key": key},
                )
            # drop this source's events that disappeared (rescheduled / cancelled)
            if coverage == "range" and items:
                lo, hi = min(i["date"] for i in items), max(i["date"] for i in items)
            else:
                lo, hi = today, "9999-12-31"
            stale = [r[0] for r in conn.execute(
                "SELECT ext_key FROM events WHERE source=? AND date BETWEEN ? AND ?", (name, lo, hi))
                if r[0] not in keys]
            conn.executemany("DELETE FROM events WHERE ext_key=?", [(k,) for k in stale])
            report[name] = f"{len(items)} 条"
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"fetched_at:{name}", str(time.time())))

        # same release from several sources: keep only the best one per day
        rows = conn.execute(
            "SELECT id, date, topic, source FROM events WHERE topic IS NOT NULL AND source != 'manual'"
        ).fetchall()
        best = {}
        for id_, d, topic, src in rows:
            k = (d, topic)
            if k not in best or rank(topic, src) < rank(topic, best[k][1]):
                best[k] = (id_, src)
        keep = {v[0] for v in best.values()}
        conn.executemany("DELETE FROM events WHERE id=?",
                         [(r[0],) for r in rows if r[0] not in keep])
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('events_synced_at', ?)", (str(time.time()),))
        failed = any(v.startswith("失败") for v in report.values())
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('events_sync_failed', ?)", ("1" if failed else "0",))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('events_sync_report', ?)",
                     (" · ".join(f"{k} {v}" for k, v in report.items()),))
    return report


if __name__ == "__main__":
    import sys
    for k, v in sync_all(force="--force" in sys.argv).items():
        print(f"{k:8s} {v}")
