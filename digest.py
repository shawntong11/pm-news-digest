"""美国政治 + 金融市场 + 科技/AI 每小时新闻摘要（本地 Ollama / Qwen，不调用收费模型）

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
]

# 宏观数据表
MACRO = [
    ("ES=F", "标普500期货"), ("NQ=F", "纳指100期货"), ("YM=F", "道指期货"),
    ("CL=F", "WTI原油"), ("GC=F", "黄金"), ("BTC-USD", "比特币"),
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


def fetch_macro():
    import yfinance as yf
    rows = []
    for sym, name in MACRO:
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
                "reason": "",
            })
        except Exception as e:
            print(f"[warn] 行情 {sym}: {e}", file=sys.stderr)
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
                                  "link": url})
                if len(r["news"]) >= 3:
                    break
        except Exception as e:
            print(f"[warn] 个股新闻 {r['sym']}: {e}", file=sys.stderr)


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

SYSTEM = """你是一名服务于预测市场交易员的美国政治、金融与科技新闻编辑。只能依据用户提供的材料写作，不得编造材料之外的事实、数字或引语。用简体中文输出，专有名词（人名、公司、法案）可保留英文。
写人物时只用材料原文里出现的称呼，例如材料写 "Sen. Chris Van Hollen (D-Md.)" 就写“民主党参议员 Van Hollen（马里兰）”；材料没有写州名就不要写州名。凭记忆补充州名、职位是严重错误。
因果关系只能照搬材料原文；材料没有说明原因时，只描述发生了什么。"""

PROMPT = """现在是美东时间 {now}。下面是新出现的新闻（编号 [n]）、宏观行情、标普500龙头股涨跌幅榜（附个股新闻）和未来 24 小时日程。

请输出一个 JSON 对象，字段如下（所有文字用简体中文）：
- points：本期要点，3 到 5 条最重要的新进展，按影响从大到小排序，政治、市场、科技都要考虑；被多个来源同时报道的事件通常最重要
- politics：要点之外的美国政治新闻 2 到 5 条（选举、国会、白宫、法院、人事）
- tech：要点之外的科技与 AI 新闻 2 到 5 条，只限 AI 公司与模型、芯片与半导体、大型科技公司经营、科技监管与政策；游戏、影视、消费电子评测、App 更新一律不要；没有就留空数组
- markets：要点之外的宏观、公司、行业新闻 2 到 4 条（美联储、经济数据、财报、裁员、监管）
- pm：对预测市场的含义 1 到 3 条（选举、立法、美联储利率、经济数据、关税、政府人事、科技公司事件），说明方向；没有把握就写“暂无明显影响”
- watch：根据日程列出最重要的 2 到 4 项；日程为空就写“暂无已知日程”
- macro_reasons：给宏观行情表里的每个品种写一句涨跌原因（15 字以内），name 用表里的名称
- movers：给涨跌幅榜上的每只股票写一句涨跌原因（20 字以内），ticker 用表里的代码，依据该股票下面列出的个股新闻
- macro_reasons 和 movers 的原因必须是新闻原文明确写出的原因（例如原文写 "oil fell after Trump called renewed strikes unlikely"）。“避险需求”“资金轮动”“板块反弹”“市场情绪”这类泛泛的推测一律不许写；找不到明确原因就填“无明确消息”。

写作规则：
- points、politics、tech、markets、pm 每条一句话，结尾标注来源编号，如 [3][7]；只能用“新闻”部分的数字编号，个股新闻没有编号，不要标注。
- pm 只写由新闻直接推出的影响，不要猜测远期选举赔率；美联储方向要以新闻原文为准，不要自己说“加息”或“降息”。
- 同一事件只写一次：写进 points 的事件，不要在其他分类里重复。
- 不要添加材料里没有的背景信息（如议员所属州、职位、党派）。
- 忽略生活方式、名人八卦、个人理财建议、体育、地方社会新闻等与政治、市场、科技无关的内容。
- “上一版要点”里已经报道过的事件，除非有新进展，否则不要再写。

## 上一版要点
{prev}

## 宏观行情
{macro}

## 标普500龙头股涨跌幅榜（较前一交易日）
{movers}

## 未来 24 小时日程
{events}

## 新闻
{news}
"""

LIST = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "points": LIST, "politics": LIST, "tech": LIST, "markets": LIST, "pm": LIST, "watch": LIST,
        "macro_reasons": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["name", "reason"], "additionalProperties": False}},
        "movers": {"type": "array", "items": {"type": "object", "properties": {
            "ticker": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["ticker", "reason"], "additionalProperties": False}},
    },
    "required": ["points", "politics", "tech", "markets", "pm", "watch", "macro_reasons", "movers"],
    "additionalProperties": False,
}


def fmt_pct(v):
    return "—" if v is None else f"{v:+.2f}%"


def build_prompt(items, macro, gainers, losers, events, prev, now_et):
    news = "\n".join(
        f"[{i}] ({it['cat']}·{it['src']}"
        + (f"·{it['pub'].astimezone(ET):%H:%M}" if it["pub"] else "")
        + f") {it['title']}" + (f" —— {it['summary']}" if it["summary"] else "")
        for i, it in enumerate(items, 1)
    )
    macro_txt = "\n".join(f"- {r['name']}：{r['last']:,.2f}，1小时 {fmt_pct(r['h1'])}，较前收 {fmt_pct(r['d1'])}"
                          for r in macro) or "（行情获取失败）"
    mv = []
    for label, lst in (("涨幅榜", gainers), ("跌幅榜", losers)):
        mv.append(f"{label}：")
        for r in lst:
            mv.append(f"- {r['sym']} {r['name']} {fmt_pct(r['chg'])}")
            for n in r["news"]:
                mv.append(f"    · 个股新闻：{n['title']}" + (f" —— {n['summary']}" if n["summary"] else ""))
    return PROMPT.format(
        now=f"{now_et:%Y-%m-%d %H:%M}", prev=prev or "（无）", macro=macro_txt,
        movers="\n".join(mv), events="\n".join(events) or "（无）", news=news or "（本期无新增新闻）",
    )


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
    for k in ("points", "politics", "tech", "markets", "pm", "watch"):
        out[k] = [fix(s) for s in out.get(k, []) if isinstance(s, str) and s.strip()]
    return out


def apply_reasons(out, macro, movers):
    reasons = {x.get("name", "").strip(): x.get("reason", "") for x in out.get("macro_reasons", [])}
    for r in macro:
        r["reason"] = reasons.get(r["name"]) or "无明确消息"
    reasons = {x.get("ticker", "").strip().upper(): x.get("reason", "") for x in out.get("movers", [])}
    for r in movers:
        r["reason"] = reasons.get(r["sym"]) or "无明确消息"


# ---------- 排版 ----------

SECTIONS = [("points", "本期要点"), ("politics", "美国政治"), ("tech", "科技与 AI"),
            ("markets", "宏观与公司"), ("pm", "对预测市场的含义"), ("watch", "接下来关注")]


def render_md(title, out, items, macro, gainers, losers, footer):
    def text(s):
        def rep(m):
            n = int(m.group(1))
            return f"[[{n}]]({items[n - 1]['link']})" if 1 <= n <= len(items) and items[n - 1]["link"] else ""
        s = re.sub(r"\[[^\]\d][^\]]{0,20}\]", "", s.replace("**", ""))
        return re.sub(r"\[(\d{1,3})\]", rep, s)

    chg = fmt_pct

    parts = [f"# {title}"]
    for key, name in SECTIONS[:4]:
        if out.get(key):
            parts.append(f"### {name}\n" + "\n".join(f"- {text(s)}" for s in out[key]))
    rows = "\n".join(f"| {r['name']} | {r['last']:,.2f} | {chg(r['h1'])} | {chg(r['d1'])} | {r['reason'] or '—'} |"
                     for r in macro)
    parts.append("### 宏观数据\n| 品种 | 最新 | 1小时 | 较前收 | 原因 |\n|---|---:|---:|---:|---|\n" + rows)
    for label, lst in (("涨幅榜", gainers), ("跌幅榜", losers)):
        if lst:
            rows = "\n".join(f"| {r['name']}{'' if r['name'] == r['sym'] else ' ' + r['sym']} | {r['last']:,.2f} | {chg(r['chg'])} | {r['reason'] or '—'} |"
                             for r in lst)
            parts.append(f"### 标普500龙头 · {label}\n| 股票 | 价格 | 涨跌 | 原因 |\n|---|---:|---:|---|\n" + rows)
    for key, name in SECTIONS[4:]:
        if out.get(key):
            parts.append(f"### {name}\n" + "\n".join(f"- {text(s)}" for s in out[key]))
    parts.append(f"_{footer}_")
    return "\n\n".join(parts) + "\n"


# ---------- 输出 ----------

def notify(title, body):
    if sys.platform != "darwin":  # 云端没有 macOS 通知
        return
    body = re.sub(r"\[\d+\]|\*\*", "", body).replace('"', "'")[:200]
    subprocess.run(["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                   check=False, capture_output=True)


def send_telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20, json={
            "chat_id": chat, "text": text[:4000], "parse_mode": "Markdown",
            "disable_web_page_preview": True})
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
    gainers, losers = fetch_movers()
    fetch_stock_news(gainers + losers)
    events = upcoming_events(now_et)
    prev_row = con.execute("SELECT v FROM meta WHERE k='prev_points'").fetchone()
    prev = prev_row[0] if prev_row else ""
    print(f"新增新闻 {len(items)} 条，宏观 {len(macro)} 项，涨/跌榜 {len(gainers)}/{len(losers)}，日程 {len(events)} 项")

    prompt = build_prompt(items, macro, gainers, losers, events, prev, now_et)
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
        apply_reasons(out, macro, gainers + losers)
    else:  # 没有新新闻就不调用模型
        out = {"points": ["本期没有新的重要新闻。"], "watch": events[:4]}

    title = f"美国要闻 {now_et:%m-%d %H:00} ET"
    footer = (f"生成：{datetime.now(ET):%Y-%m-%d %H:%M} ET · 模型 {MODEL if items else '无（无新增新闻）'}"
              f" · 用时 {time.time() - t0:.0f}s · 行情来自 Yahoo Finance")
    md = render_md(title, out, items, macro, gainers, losers, footer)

    out_dir = DIGESTS / f"{now_et:%Y-%m-%d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{now_et:%H}00.md").write_text(md)
    (DATA / "latest.md").write_text(md)

    # 成功后再标记已读，失败的话下一小时会重试
    now_ts = time.time()
    con.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?)",
                    [(it["key"], it["title"], now_ts) for it in items])
    if items and out.get("points"):
        plain = "\n".join("- " + re.sub(r"\[\d+\]|\*\*", "", s) for s in out["points"])
        con.execute("INSERT OR REPLACE INTO meta VALUES ('prev_points', ?)", (plain,))
    con.execute("DELETE FROM seen WHERE ts < ?", (now_ts - 7 * 86400,))
    if slot:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_slot', ?)", (slot,))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_run_ts', ?)", (str(t0),))
    con.commit()

    print(f"已写入 {DATA / 'latest.md'}（{time.time() - t0:.0f}s）")
    if not args.no_notify:
        notify(title, (out.get("points") or ["已更新"])[0])
        send_telegram(re.sub(r"\[\[(\d+)\]\]\([^)]*\)", r"[\1]", md)[:4000])


if __name__ == "__main__":
    main()
