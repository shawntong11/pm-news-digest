"""每日金融简报：美国政治、国际政治、科技与 AI、加密货币、金融市场新闻摘要（默认 Claude Haiku 5.5）

用法：
    python digest.py            # 生成一版摘要
    python digest.py --dry-run  # 只打印发给模型的内容，不调用模型、不标记已读
    python digest.py --no-notify
"""
import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DIGESTS = DATA / "digests"
DB_PATH = DATA / "seen.sqlite"
# 本地用 market_calendar 的数据库；云端（GitHub Actions）用 events/calendar.db
CALENDAR_DB = Path(os.environ.get("CALENDAR_DB") or ROOT.parent / "market_calendar" / "calendar.db")
ET = ZoneInfo("America/New_York")

# claude（默认，Claude Haiku 5.5，按量付费）或 ollama（本地 Qwen，免费但很占内存）
# 这几个值要在 load_env() 读完 .env 之后再取，见 main()
PROVIDER = MODEL = None
OLLAMA_URL = "http://localhost:11434"
THINK = True

LOOKBACK_HOURS = 3      # 只看最近几小时发布的新闻
# 美东时间早上 8 点到午夜，每 2 小时一版（8、10 … 22、0 点），共 9 版。
# launchd 每小时唤醒一次；某一版因为电脑休眠错过了，下一次唤醒时补上。


def current_slot(now_et):
    """返回当前应该生成的那一版的标识，例如 '2026-11-11 10'；不在生成时段返回 None。"""
    h = now_et.hour
    if 8 <= h <= 23:
        return f"{now_et:%Y-%m-%d} {h - h % 2:02d}"
    if h <= 1:  # 午夜那一版，凌晨 1 点前还可以补
        return f"{now_et - timedelta(hours=h + 1):%Y-%m-%d} 24"
    return None
PER_CAT = 22            # 每个分类最多交给模型的新闻条数
UA = "Mozilla/5.0 (Macintosh) news-digest/1.0"

FEEDS = [
    # (分类, 来源名, URL)
    ("政治", "Politico", "https://rss.politico.com/politics-news.xml"),
    ("政治", "The Hill", "https://thehill.com/homenews/feed/"),
    ("政治", "NPR Politics", "https://feeds.npr.org/1014/rss.xml"),
    ("政治", "Google News 美国", "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-US&gl=US&ceid=US:en"),
    ("金融", "CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("金融", "CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("金融", "CNBC Markets", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("金融", "WSJ Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("金融", "MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("金融", "美联储", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("金融", "Google News 商业", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"),
    ("科技", "CNBC Tech", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("科技", "TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("科技", "TechCrunch", "https://techcrunch.com/feed/"),
    ("科技", "The Verge", "https://www.theverge.com/rss/index.xml"),
    ("科技", "Google News 科技", "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en"),
    ("国际", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("国际", "NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ("国际", "Google News 国际", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en"),
    ("中国", "BBC 中国", "https://feeds.bbci.co.uk/news/world/asia/china/rss.xml"),
    ("中国", "Google News 中国", "https://news.google.com/rss/search?q=China+OR+Beijing+OR+%22Xi+Jinping%22+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("加密", "CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("加密", "Cointelegraph", "https://cointelegraph.com/rss"),
    ("加密", "Decrypt", "https://decrypt.co/feed"),
]

# 宏观数据表：(代码, 名称, 用来找涨跌原因的 Google News 搜索词)
MACRO = [
    ("ES=F", "标普500期货", "stock market today S&P 500"), ("NQ=F", "纳指100期货", "Nasdaq stocks today"),
    ("YM=F", "道指期货", "Dow Jones today"), ("CL=F", "WTI原油", "crude oil prices"), ("GC=F", "黄金", "gold prices today"),
    ("EURUSD=X", "欧元/美元", "euro dollar"), ("GBPUSD=X", "英镑/美元", "pound sterling GBP/USD"),
    ("JPY=X", "美元/日元", "yen dollar USD/JPY"), ("CAD=X", "美元/加币", "Canadian dollar loonie USD/CAD"),
    ("AUDUSD=X", "澳元/美元", "Australian dollar AUD/USD"),
]

# 加密货币实时价格
CRYPTO = [
    ("BTC-USD", "BTC", "bitcoin price crypto market today"), ("ETH-USD", "ETH", "ether price ethereum"),
    ("XRP-USD", "XRP", "XRP price"), ("SOL-USD", "SOL", "solana price"),
    ("BNB-USD", "BNB", "BNB price"), ("HYPE32196-USD", "HYPE", "Hyperliquid HYPE price"),
]

# 标普500 龙头股（按市值挑选）
LEADERS = {
    "AAPL": "苹果", "MSFT": "微软", "NVDA": "英伟达", "GOOGL": "谷歌", "AMZN": "亚马逊",
    "META": "Meta", "AVGO": "博通", "TSLA": "特斯拉", "BRK-B": "伯克希尔", "JPM": "摩根大通",
    "LLY": "礼来", "V": "Visa", "WMT": "沃尔玛", "ORCL": "甲骨文", "MA": "万事达",
    "XOM": "埃克森美孚", "NFLX": "奈飞", "COST": "好市多", "JNJ": "强生", "PG": "宝洁",
    "HD": "家得宝", "ABBV": "艾伯维", "BAC": "美国银行", "UNH": "联合健康", "KO": "可口可乐",
    "PLTR": "Palantir", "AMD": "AMD", "CVX": "雪佛龙", "GE": "GE航空", "CSCO": "思科",
    "WFC": "富国银行", "IBM": "IBM", "MS": "摩根士丹利", "PM": "菲利普莫里斯", "CRM": "Salesforce",
    "GS": "高盛", "ABT": "雅培", "MU": "美光", "LIN": "林德", "MCD": "麦当劳",
    "AXP": "美国运通", "T": "AT&T", "MRK": "默沙东", "DIS": "迪士尼", "PEP": "百事",
    "INTU": "Intuit", "NOW": "ServiceNow", "TMO": "赛默飞", "ISRG": "直觉外科", "CAT": "卡特彼勒",
}
TOP_N = 5  # 涨幅榜、跌幅榜各几只


# ---------- 去重存储 ----------

def db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, title TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return con


SOURCE_CN = {
    "the new york times": "纽约时报", "new york times": "纽约时报", "nytimes.com": "纽约时报",
    "the wall street journal": "华尔街日报", "wall street journal": "华尔街日报", "wsj": "华尔街日报",
    "wsj markets": "华尔街日报", "reuters": "路透社", "bloomberg": "彭博社", "bloomberg.com": "彭博社",
    "associated press": "美联社", "ap news": "美联社", "financial times": "金融时报",
    "the washington post": "华盛顿邮报", "washington post": "华盛顿邮报", "the economist": "经济学人",
    "south china morning post": "南华早报", "scmp": "南华早报", "yahoo": "雅虎财经",
    "bbc": "BBC", "bbc world": "BBC", "bbc 中国": "BBC", "npr politics": "NPR", "npr world": "NPR", "cnbc economy": "CNBC",
    "cnbc markets": "CNBC", "cnbc tech": "CNBC", "techcrunch ai": "TechCrunch", "yahoo finance": "雅虎财经",
}


def source_name(name):
    """统一来源名：常见大报用中文名，其余保留英文。"""
    name = re.split(r"\s+[|–—:]\s+", (name or "").strip())[0][:30]  # 去掉“The Strategist | ASPI's ...”这种长尾巴
    return SOURCE_CN.get(name.lower(), name)


def norm_title(t):
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t)  # 去掉 Google News 标题末尾的 " - 来源"
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def key_of(title):
    return hashlib.sha1(norm_title(title).encode()).hexdigest()


# ---------- 新闻 ----------

def clean(s, n=220):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\{beacon\}", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n] + ("…" if len(s) > n else "")


def fetch_news(con, hours=LOOKBACK_HOURS):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    by_cat, keys = {}, set()
    for cat, src, url in FEEDS:
        try:
            f = feedparser.parse(url, agent=UA)
        except Exception as e:  # 单个源出错不影响整体
            print(f"[warn] {src}: {e}", file=sys.stderr)
            continue
        for e in f.entries:
            title = clean(e.get("title"), 200)
            if not title:
                continue
            tp = e.get("published_parsed") or e.get("updated_parsed")
            pub = datetime(*tp[:6], tzinfo=timezone.utc) if tp else None
            if pub and pub < cutoff:
                continue
            k = key_of(title)
            if k in keys or con.execute("SELECT 1 FROM seen WHERE key=?", (k,)).fetchone():
                continue
            keys.add(k)
            summary = clean(e.get("summary"))
            if summary and norm_title(summary).startswith(norm_title(title)[:40]):
                summary = ""  # Google News 的简介只是重复标题
            by_cat.setdefault(cat, []).append({
                "cat": cat, "src": src, "title": title, "summary": summary,
                "source": source_name((e.get("source") or {}).get("title") if src.startswith("Google") else src),
                "link": e.get("link", ""), "pub": pub, "key": k,
            })
    newest = lambda x: x["pub"] or datetime.min.replace(tzinfo=timezone.utc)
    items = []
    for lst in by_cat.values():  # 每个分类各取最新的若干条，避免某一类挤掉其他类
        items += sorted(lst, key=newest, reverse=True)[:PER_CAT]
    return sorted(items, key=newest, reverse=True)


# ---------- 行情 ----------

def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


def search_news(query, n=5, days=1):
    """用 Google News 搜索最近的相关报道，用来解释行情涨跌（这类标题通常直接写原因）。"""
    url = ("https://news.google.com/rss/search?q=" + requests.utils.quote(f"{query} when:{days}d")
           + "&hl=en-US&gl=US&ceid=US:en")
    f = None
    for attempt in range(3):  # Google News 偶尔返回空结果，重试两次
        try:
            f = feedparser.parse(url, agent=UA)
            if f.entries:
                break
        except Exception as e:
            print(f"[warn] 搜索 {query}: {e}", file=sys.stderr)
        time.sleep(2)
    if not f or not f.entries:
        return []
    out = []
    for e in f.entries[:n]:
        tp = e.get("published_parsed")
        out.append({"title": clean(e.get("title"), 160), "summary": "", "link": e.get("link", ""),
                    "source": source_name((e.get("source") or {}).get("title")),
                    "when": f"{datetime(*tp[:6], tzinfo=timezone.utc).astimezone(ET):%m-%d %H:%M}" if tp else ""})
    return out


def fetch_macro():
    import yfinance as yf
    rows = []
    for sym, name, query in MACRO:
        try:
            t = yf.Ticker(sym)
            h = t.history(period="5d", interval="60m")["Close"].dropna()
            d = t.history(period="10d", interval="1d")["Close"].dropna()
            if h.empty:
                continue
            last, last_ts = float(h.iloc[-1]), h.index[-1].tz_convert(ET)
            prior = d[[ix.date() < last_ts.date() for ix in d.index]]
            rows.append({
                "sym": sym, "name": name, "last": last,
                "h1": pct(last, float(h.iloc[-2])) if len(h) > 1 else None,
                "d1": pct(last, float(prior.iloc[-1])) if len(prior) else None,
                "reason": "", "news": search_news(query),
            })
        except Exception as e:
            print(f"[warn] 行情 {sym}: {e}", file=sys.stderr)
    return rows


def fetch_crypto():
    """加密货币 24 小时交易，所以涨跌用“1 小时”和“24 小时”。"""
    import yfinance as yf
    rows = []
    for sym, name, query in CRYPTO:
        try:
            h = yf.Ticker(sym).history(period="3d", interval="60m")["Close"].dropna()
            if h.empty:
                continue
            last = float(h.iloc[-1])
            rows.append({
                "sym": sym, "name": name, "last": last,
                "h1": pct(last, float(h.iloc[-2])) if len(h) > 1 else None,
                "d1": pct(last, float(h.iloc[-25])) if len(h) > 24 else None,
                "reason": "", "news": search_news(query),
            })
        except Exception as e:
            print(f"[warn] 加密 {sym}: {e}", file=sys.stderr)
    return rows


def fetch_movers():
    """标普500 龙头股今日（或最近一个交易日）涨跌幅榜。"""
    import yfinance as yf
    try:
        d = yf.download(list(LEADERS), period="5d", interval="1d", progress=False,
                        threads=False, auto_adjust=False)["Close"]
    except Exception as e:
        print(f"[warn] 龙头股: {e}", file=sys.stderr)
        return [], []
    rows = []
    for sym in LEADERS:
        s = d[sym].dropna() if sym in d else []
        if len(s) >= 2:
            rows.append({"sym": sym, "name": LEADERS[sym], "last": float(s.iloc[-1]),
                         "chg": pct(float(s.iloc[-1]), float(s.iloc[-2])),
                         "date": s.index[-1].date(), "reason": "", "news": []})
    rows.sort(key=lambda r: r["chg"], reverse=True)
    gainers = [r for r in rows if r["chg"] > 0][:TOP_N]
    losers = [r for r in reversed(rows) if r["chg"] < 0][:TOP_N]
    return gainers, losers


def fetch_stock_news(movers):
    """给涨跌幅榜上的股票拉 Yahoo 个股新闻，用来解释涨跌原因。"""
    import yfinance as yf
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    for r in movers:
        try:
            for n in (yf.Ticker(r["sym"]).news or [])[:6]:
                c = n.get("content", n)
                pub = c.get("pubDate") or ""
                try:
                    if datetime.fromisoformat(pub.replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    pass
                url = (c.get("canonicalUrl") or c.get("clickThroughUrl") or {}).get("url", "")
                r["news"].append({"title": clean(c.get("title"), 160),
                                  "summary": clean(c.get("summary") or c.get("description"), 160),
                                  "link": url, "source": source_name((c.get("provider") or {}).get("displayName"))})
                if len(r["news"]) >= 3:
                    break
        except Exception as e:
            print(f"[warn] 个股新闻 {r['sym']}: {e}", file=sys.stderr)
        # Yahoo 个股新闻常常不是讲当天涨跌的，再补几条“为什么涨/跌”的搜索结果
        r["news"] += search_news(f"{r['sym'].replace('-', '.')} stock", n=3, days=2)


# ---------- 日历 ----------

def upcoming_events(now_et, hours=24):
    if not CALENDAR_DB.exists():
        return []
    end = now_et + timedelta(hours=hours)
    try:
        con = sqlite3.connect(f"file:{CALENDAR_DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT date, COALESCE(time,''), title, COALESCE(note,''), importance FROM events "
            "WHERE date BETWEEN ? AND ? AND importance >= 2 ORDER BY date, time",
            (now_et.date().isoformat(), end.date().isoformat()),
        ).fetchall()
        con.close()
    except sqlite3.Error as e:
        print(f"[warn] 日历: {e}", file=sys.stderr)
        return []
    out = []
    for date, tm, title, note, imp in rows:
        if tm:
            when = datetime.fromisoformat(f"{date}T{tm}").replace(tzinfo=ET)
            if not (now_et <= when <= end):
                continue
        elif date == now_et.date().isoformat() and now_et.hour >= 20:
            continue
        note = note.replace("\n", "；")[:120]
        star = "★" if imp >= 3 else ""
        out.append(f"{date} {tm or '全天'} {star}{title}" + (f"（{note}）" if note else ""))
    return out[:15]


# ---------- 模型 ----------

SYSTEM = """你是一名服务于预测市场交易员的新闻编辑，负责美国政治、国际政治、金融、科技与加密货币。只能依据用户提供的材料写作，不得编造材料之外的事实、数字或引语。用简体中文输出，专有名词（人名、公司、法案）可保留英文。
写人物时只用材料原文里出现的称呼，例如材料写 "Sen. Chris Van Hollen (D-Md.)" 就写“民主党参议员 Van Hollen（马里兰）”；材料没有写州名就不要写州名。凭记忆补充州名、职位是严重错误。
因果关系只能照搬材料原文；材料没有说明原因时，只描述发生了什么。"""

PROMPT = """现在是美东时间 {now}。下面是新出现的新闻（编号 [n]）、宏观行情和加密货币行情（各附相关报道）、标普500龙头股涨跌幅榜（附个股报道）和未来 24 小时日程。

请输出一个 JSON 对象，字段如下（所有文字用简体中文）。每个分类内部按重要性从高到低排序，被多个来源同时报道的事件通常最重要：
- politics：美国政治 2 到 6 条（选举、国会、白宫、法院、人事、联邦机构）
- world：国际新闻 2 到 5 条（战争与冲突、外交、贸易谈判与关税、各国大选与政局、国际组织），与美国相关或影响市场的优先；中国的新闻放进 china，不要放这里；没有就留空数组
- china：中国新闻 2 到 5 条（中美关系、关税与出口管制、经济数据、政策与监管、台海与南海、香港、中国科技和大公司），影响市场或美国政策的优先；没有就留空数组
- tech：科技与 AI 2 到 5 条，只限 AI 公司与模型、芯片与半导体、大型科技公司经营、科技监管与政策；游戏、影视、消费电子评测、App 更新一律不要；没有就留空数组
- crypto：加密货币 2 到 5 条（价格大幅波动、ETF 资金流、监管与立法、交易所和项目、机构动向）；没有就留空数组
- markets：宏观与公司 2 到 4 条（美联储、经济数据、财报、裁员、并购、监管）
- pm：对预测市场的含义 1 到 3 条（选举、立法、美联储利率、经济数据、关税、政府人事、科技公司事件、加密货币价格区间），说明方向；没有把握就写“暂无明显影响”
- watch：根据日程列出最重要的 2 到 4 项；日程为空就写“暂无已知日程”
- macro_reasons：给宏观行情表里的每个品种写涨跌原因，name 用表里的名称
- crypto_reasons：给加密货币行情表里的每个币写涨跌原因，name 用表里的名称（如 BTC）
- movers：给涨跌幅榜上的每只股票写涨跌原因，ticker 用表里的代码

涨跌原因的写法（macro_reasons、crypto_reasons、movers）：
- 一定要写出原因：依据该品种下面列出的“相关报道”，以及“新闻”部分里直接相关的报道，概括报道中说的驱动因素，30 字以内，例如“特朗普称不太可能重启空袭，油价回落”“AI 芯片股遭抛售，英伟达 CEO 辞职拖累”“BTC 跌破 6.5 万美元引发多头爆仓”。
- 宏观品种（股指期货、原油、黄金）的原因通常就是当天大盘或商品的主线新闻，可以用“新闻”部分里的宏观新闻；汇率要写清是哪一边的因素（美元整体强弱、美联储预期、对方央行如欧央行/英央行/日本央行/加拿大央行/澳洲联储、该国经济数据、油价对加币、中国需求对澳元、日本政府干预等），以报道为准；BTC 之外的币如果报道说是跟随 BTC 或整个加密市场下跌，就写“随 BTC/加密市场整体下跌”加上整体原因。
- 原因必须来自报道原文，不许编造；“避险需求”“资金轮动”“市场情绪”这类空话只有在报道原文这么说时才可以写。
- 原因要能解释涨跌的方向：只是同一家公司的其他新闻（例如被罚款，股价却上涨），不能当作原因。个股涨跌幅是上一个交易日的，周末发布的报道一般不是原因。
- 所有报道都没有提到驱动因素时，只写“无明确消息”这五个字，不要再附加“相关报道称……”之类的说明。
- 原因结尾也标注依据的编号，如“……油价回落 [31]”。
- 涨跌幅很小（1 小时和较前收都在 ±0.3% 以内）可以写“窄幅波动”。

写作规则：
- politics、world、china、tech、crypto、markets、pm 每条一句话，结尾标注来源编号，如 [3][7]；“新闻”“相关报道”“个股报道”的编号都可以用，不要编造编号，也不要写“相关报道称”这种字眼。
- pm 只写由新闻直接推出的影响，不要猜测远期选举赔率；美联储方向要以新闻原文为准，不要自己说“加息”或“降息”。
- 同一事件只写一次，放进最合适的一个分类，不要在其他分类里重复。
- 不要添加材料里没有的背景信息（如议员所属州、职位、党派）。
- 忽略生活方式、名人八卦、个人理财建议、体育、地方社会新闻等与政治、市场、科技无关的内容。
- “上一版要点”里已经报道过的事件，除非有新进展，否则不要再写。

## 上一版要点
{prev}

## 宏观行情（附相关报道）
{macro}

## 加密货币行情（1 小时、24 小时涨跌，附相关报道）
{crypto}

## 标普500龙头股涨跌幅榜（较前一交易日）
{movers}

## 未来 24 小时日程
{events}

## 新闻
{news}
"""

LIST = {"type": "array", "items": {"type": "string"}}
SECTION_KEYS = ["politics", "world", "china", "tech", "crypto", "markets", "pm", "watch"]


def reasons_schema(key):
    return {"type": "array", "items": {"type": "object", "properties": {
        key: {"type": "string"}, "reason": {"type": "string"}},
        "required": [key, "reason"], "additionalProperties": False}}


SCHEMA = {
    "type": "object",
    "properties": {**{k: LIST for k in SECTION_KEYS},
                   "macro_reasons": reasons_schema("name"),
                   "crypto_reasons": reasons_schema("name"),
                   "movers": reasons_schema("ticker")},
    "required": SECTION_KEYS + ["macro_reasons", "crypto_reasons", "movers"],
    "additionalProperties": False,
}


def fmt_pct(v):
    return "—" if v is None else f"{v:+.2f}%"


def fmt_price(v):
    return f"{v:,.4f}" if v < 10 else f"{v:,.2f}"


def build_prompt(items, macro, crypto, gainers, losers, events, prev, now_et):
    """返回 (提示词, refs)。refs 是所有带编号的报道：先是新闻，再是行情和个股的相关报道。"""
    refs = list(items)
    seen = {norm_title(it["title"]): i for i, it in enumerate(refs, 1)}

    def news_lines(r, label="相关报道"):
        lines = []
        for n in r["news"]:
            k = norm_title(n["title"])
            if k not in seen:
                refs.append(n)
                seen[k] = len(refs)
            lines.append(f"    · [{seen[k]}] {label}{'（' + n['when'] + '）' if n.get('when') else ''}：{n['title']}"
                         + (f" —— {n['summary']}" if n.get("summary") else ""))
        return lines

    news = "\n".join(
        f"[{i}] ({it['cat']}·{it['src']}"
        + (f"·{it['pub'].astimezone(ET):%H:%M}" if it["pub"] else "")
        + f") {it['title']}" + (f" —— {it['summary']}" if it["summary"] else "")
        for i, it in enumerate(items, 1)
    )
    macro_txt = []
    for r in macro:
        macro_txt.append(f"- {r['name']}：{fmt_price(r['last'])}，1小时 {fmt_pct(r['h1'])}，较前收 {fmt_pct(r['d1'])}")
        macro_txt += news_lines(r)
    crypto_txt = []
    for r in crypto:
        crypto_txt.append(f"- {r['name']}：${fmt_price(r['last'])}，1小时 {fmt_pct(r['h1'])}，24小时 {fmt_pct(r['d1'])}")
        crypto_txt += news_lines(r)
    mv = []
    for label, lst in (("涨幅榜", gainers), ("跌幅榜", losers)):
        mv.append(f"{label}：")
        for r in lst:
            mv.append(f"- {r['sym']} {r['name']} {fmt_pct(r['chg'])}")
            mv += news_lines(r, "个股报道")
    return PROMPT.format(
        now=f"{now_et:%Y-%m-%d %H:%M}", prev=prev or "（无）",
        macro="\n".join(macro_txt) or "（行情获取失败）", crypto="\n".join(crypto_txt) or "（行情获取失败）",
        movers="\n".join(mv), events="\n".join(events) or "（无）", news=news or "（本期无新增新闻）",
    ), refs


STATE_RE = re.compile(r"[一-龥]{1,6}州(?=(?:民主党|共和党)?(?:联邦)?(?:参议员|众议员))")


def unload_model():
    """让 Ollama 立刻释放模型占用的内存（约 24GB）。"""
    try:
        requests.post(f"{OLLAMA_URL}/api/generate", timeout=60,
                      json={"model": MODEL, "keep_alive": 0})
    except requests.RequestException as e:
        print(f"[warn] 卸载模型失败: {e}", file=sys.stderr)


def call_ollama(prompt):
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=1200, json={
        "model": MODEL,
        "stream": False,
        "think": THINK,
        "format": SCHEMA,
        "keep_alive": 0,  # 生成完立刻释放内存，避免电脑变卡
        "options": {"temperature": 0.3, "num_ctx": 20480},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
    })
    r.raise_for_status()
    text = re.sub(r"<think>.*?</think>", "", r.json()["message"]["content"], flags=re.S).strip()
    return clean_output(json.loads(text))


def call_claude(prompt):
    import anthropic
    client = anthropic.Anthropic()  # 从环境变量 ANTHROPIC_API_KEY 读取密钥
    msg = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if msg.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude 没有完整生成摘要（stop_reason={msg.stop_reason}）")
    u = msg.usage
    cost = u.input_tokens * 0.10 / 1e6 + u.output_tokens * 0.50 / 1e6
    print(f"Claude 用量：输入 {u.input_tokens}，输出 {u.output_tokens} token，约 ${cost:.4f}")
    text = next(b.text for b in msg.content if b.type == "text")
    return clean_output(json.loads(text))


def clean_output(out):
    def fix(s):  # 小模型常凭记忆给议员配错州名（如“佛罗里达州参议员 Van Hollen”），直接删掉
        return STATE_RE.sub("", s.strip()) if isinstance(s, str) else s
    for k in SECTION_KEYS:
        out[k] = [fix(s) for s in out.get(k, []) if isinstance(s, str) and s.strip()]
    return out


def apply_reasons(out, macro, crypto, movers):
    for rows, key, field in ((macro, "macro_reasons", "name"), (crypto, "crypto_reasons", "name")):
        reasons = {x.get(field, "").strip().upper(): x.get("reason", "") for x in out.get(key, [])}
        for r in rows:
            r["reason"] = reasons.get(r["name"].upper()) or "无明确消息"
    reasons = {x.get("ticker", "").strip().upper(): x.get("reason", "") for x in out.get("movers", [])}
    for r in movers:
        r["reason"] = reasons.get(r["sym"]) or "无明确消息"


# ---------- 排版 ----------

SECTIONS = [("politics", "美国政治"), ("world", "国际新闻"), ("china", "中国新闻"), ("tech", "科技与 AI"), ("crypto", "加密货币"),
            ("markets", "宏观与公司"), ("pm", "对预测市场的含义"), ("watch", "接下来关注")]


def render_md(title, out, items, macro, crypto, gainers, losers, footer):
    def text(s):
        def rep(m):
            links, names = [], set()
            for n in map(int, re.findall(r"\d+", m.group(0))):
                if not (1 <= n <= len(items) and items[n - 1].get("link")):
                    continue
                name = items[n - 1].get("source") or items[n - 1].get("src") or "原文"
                if name not in names:
                    names.add(name)
                    links.append(f"[{name}]({items[n - 1]['link']})")
            return f"（{'、'.join(links)}）" if links else ""
        s = re.sub(r"\[[^\]\d][^\]]{0,20}\]", "", s.replace("**", ""))
        s = re.sub(r"\s*(?:\[\d{1,3}\]\s*)+", rep, s)
        return s.strip()

    chg = fmt_pct

    def section(key, name):
        if out.get(key):
            parts.append(f"### {name}\n" + "\n".join(f"- {text(s)}" for s in out[key]))

    parts = [f"# {title}"]
    for key, name in SECTIONS[:5]:
        section(key, name)
    if crypto:
        rows = "\n".join(f"| {r['name']} | ${fmt_price(r['last'])} | {chg(r['h1'])} | {chg(r['d1'])} | {text(r['reason']) or '—'} |"
                         for r in crypto)
        parts.append("### 加密货币实时价格\n| 币种 | 价格 | 1小时 | 24小时 | 原因 |\n|---|---:|---:|---:|---|\n" + rows)
    section(*SECTIONS[5])
    rows = "\n".join(f"| {r['name']} | {fmt_price(r['last'])} | {chg(r['h1'])} | {chg(r['d1'])} | {text(r['reason']) or '—'} |"
                     for r in macro)
    parts.append("### 宏观数据\n| 品种 | 最新 | 1小时 | 较前收 | 原因 |\n|---|---:|---:|---:|---|\n" + rows)
    for label, lst in (("涨幅榜", gainers), ("跌幅榜", losers)):
        if lst:
            rows = "\n".join(f"| {r['name']}{'' if r['name'] == r['sym'] else ' ' + r['sym']} | {r['last']:,.2f} | {chg(r['chg'])} | {text(r['reason']) or '—'} |"
                             for r in lst)
            parts.append(f"### 标普500龙头 · {label}\n| 股票 | 价格 | 涨跌 | 原因 |\n|---|---:|---:|---|\n" + rows)
    for key, name in SECTIONS[6:]:
        section(key, name)
    parts.append(f"_{footer}_")
    return "\n\n".join(parts) + "\n"


# ---------- 输出 ----------

def notify(title, body):
    if sys.platform != "darwin":  # 云端没有 macOS 通知
        return
    body = re.sub(r"\[\d+\]|\*\*", "", body).replace('"', "'")[:200]
    subprocess.run(["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                   check=False, capture_output=True)


def to_telegram_html(md):
    """把 Markdown 摘要转成 Telegram 能显示的 HTML（Telegram 不支持标题和表格）。"""
    esc = lambda t: html.escape(t, quote=False)
    link = lambda t: re.sub(r"\[([^\]]+)\]\(([^)]*)\)",
                            lambda m: f'<a href="{html.escape(m[2])}">{m[1]}</a>', t)
    out, header = [], True
    for line in md.splitlines():
        line = line.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(cells[0]) <= set("-: "):
                continue
            if header:  # 表头那一行不发
                header = False
                continue
            reason = cells[-1]
            row = "  ".join(cells[:-1])
            out.append("• " + esc(row) + ("" if reason in ("", "无明确消息") else f"：{esc(reason)}"))
            continue
        header = True
        m = re.match(r"#+\s*(.*)", line)
        if m:
            out.append(f"\n<b>{esc(m[1])}</b>")
        elif line.startswith("_") and line.endswith("_"):
            out.append(f"<i>{esc(line.strip('_'))}</i>")
        else:
            out.append(link(esc(re.sub(r"^- ", "• ", line))))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def send_telegram(md, page_url=None):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    # 粘贴时终端可能夹带看不见的控制字符，只取 “数字:字母数字” 这一段
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", tok)
    if not m:
        print("[warn] Telegram: TELEGRAM_BOT_TOKEN 格式不对，跳过推送", file=sys.stderr)
        return
    tok = m[0]
    text = to_telegram_html(md)
    if page_url:
        text += f'\n\n<a href="{page_url}">在 GitHub 查看全文</a>'
    # Telegram 每条消息最多 4096 字，按段落拆成几条
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > 3800:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block
    chunks.append(cur)
    for chunk in chunks:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20, json={
                "chat_id": chat, "text": chunk[:4096], "parse_mode": "HTML",
                "disable_web_page_preview": True})
            if not r.ok:
                print(f"[warn] Telegram: {r.status_code} {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"[warn] Telegram: {e}", file=sys.stderr)


def load_env():
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--force", action="store_true", help="不在生成时段也照样生成")
    ap.add_argument("--check", action="store_true", help="只判断这次要不要生成，输出 run=true/false")
    args = ap.parse_args()
    load_env()
    # 网页上粘贴密钥时容易带上空格、换行或引号，统一去掉
    for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if k in os.environ:
            os.environ[k] = os.environ[k].strip().strip('"\'').strip()
    global PROVIDER, MODEL, OLLAMA_URL, THINK
    PROVIDER = os.environ.get("DIGEST_PROVIDER", "claude")
    MODEL = os.environ.get("DIGEST_MODEL") or ("qwen3:32b" if PROVIDER == "ollama" else "claude-haiku-5-5")
    OLLAMA_URL = os.environ.get("OLLAMA_URL", OLLAMA_URL)
    THINK = os.environ.get("DIGEST_THINK", "1") == "1"
    if PROVIDER == "claude" and not os.environ.get("ANTHROPIC_API_KEY") and not (args.dry_run or args.check):
        sys.exit("缺少 ANTHROPIC_API_KEY：请在 news_digest/.env 里加一行 ANTHROPIC_API_KEY=sk-ant-...")

    t0 = time.time()
    now_et = datetime.now(ET)
    con = db()
    meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
    slot = current_slot(now_et)
    skip = None
    if not (args.force or args.dry_run):
        if slot is None:
            skip = f"[{now_et:%H:%M}] 不在生成时段（8:00–24:00），跳过"
        elif meta.get("last_slot") == slot:
            skip = f"[{now_et:%H:%M}] 这一版（{slot}:00）已经生成过，跳过"
    if args.check:
        print(f"run={'false' if skip else 'true'}")
        print(skip or f"[{now_et:%H:%M}] 需要生成", file=sys.stderr)
        return
    if skip:
        print(skip)
        return
    # 回看到上一次成功生成的时间（至少 3 小时，最多 12 小时），夜间和错过的新闻都不会漏
    since = (time.time() - float(meta.get("last_run_ts", 0))) / 3600 + 0.5
    lookback = min(max(since, LOOKBACK_HOURS), 12)
    items = fetch_news(con, lookback)
    macro = fetch_macro()
    crypto = fetch_crypto()
    gainers, losers = fetch_movers()
    fetch_stock_news(gainers + losers)
    events = upcoming_events(now_et)
    prev_row = con.execute("SELECT v FROM meta WHERE k='prev_points'").fetchone()
    prev = prev_row[0] if prev_row else ""
    print(f"新增新闻 {len(items)} 条，宏观 {len(macro)} 项，加密 {len(crypto)} 项，"
          f"涨/跌榜 {len(gainers)}/{len(losers)}，日程 {len(events)} 项")

    prompt, refs = build_prompt(items, macro, crypto, gainers, losers, events, prev, now_et)
    if args.dry_run:
        print(prompt)
        return

    if items:
        if PROVIDER == "ollama":
            try:
                out = call_ollama(prompt)
            finally:
                unload_model()  # 不管成功还是出错，都把本地模型从内存里卸载
        else:
            out = call_claude(prompt)
        apply_reasons(out, macro, crypto, gainers + losers)
    else:  # 没有新新闻就不调用模型
        out = {"politics": ["本期没有新的重要新闻。"], "watch": events[:4]}

    title = f"每日金融简报 {now_et:%m-%d %H:00} ET"
    footer = (f"生成：{datetime.now(ET):%Y-%m-%d %H:%M} ET · 模型 {MODEL if items else '无（无新增新闻）'}"
              f" · 用时 {time.time() - t0:.0f}s · 行情来自 Yahoo Finance")
    md = render_md(title, out, refs, macro, crypto, gainers, losers, footer)

    out_dir = DIGESTS / f"{now_et:%Y-%m-%d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{now_et:%H}00.md").write_text(md)
    (DATA / "latest.md").write_text(md)

    # 成功后再标记已读，失败的话下一小时会重试
    now_ts = time.time()
    con.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?)",
                    [(it["key"], it["title"], now_ts) for it in items])
    # 记下这一版写过的事件（每个分类前 3 条），下一版避免重复
    reported = [s for k in SECTION_KEYS[:6] for s in out.get(k, [])[:3]]
    if items and reported:
        plain = "\n".join("- " + re.sub(r"\[\d+\]|\*\*", "", s) for s in reported)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('prev_points', ?)", (plain,))
    con.execute("DELETE FROM seen WHERE ts < ?", (now_ts - 7 * 86400,))
    if slot:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_slot', ?)", (slot,))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_run_ts', ?)", (str(t0),))
    con.commit()

    print(f"已写入 {DATA / 'latest.md'}（{time.time() - t0:.0f}s）")
    if not args.no_notify:
        notify(title, next((out[k][0] for k in SECTION_KEYS if out.get(k)), "已更新"))
        repo = os.environ.get("GITHUB_REPOSITORY")  # 在 GitHub Actions 里运行时才有
        rel = f"data/digests/{now_et:%Y-%m-%d}/{now_et:%H}00.md"
        send_telegram(md, f"https://github.com/{repo}/blob/main/{rel}" if repo else None)


if __name__ == "__main__":
    main()
