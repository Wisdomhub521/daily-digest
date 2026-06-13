#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 每日摘要 —— 完整版（抓取 / 容错 / 昨日筛选 / 补抓 / DeepSeek 摘要 / 生成 Atom feed / 去重）

当前 FEEDS 仅含 LessWrong + 阮一峰（两源验证整条链路）。加源直接往 FEEDS 里加即可，
fetch=True 表示「正文过短时允许补抓全文」。

依赖：
    pip install feedparser requests trafilatura feedgen openai

跑法：
    export DEEPSEEK_API_KEY=sk-xxxx
    python main.py                          # 默认 demo 模式（不卡日期，保证有内容）

    DIGEST_MODE=daily python main.py        # 正式：只取「昨天(Asia/Shanghai)」发布的文章
    DRY_RUN=1 python main.py                # 跳过 DeepSeek，用占位摘要看拼版/feed

产物：
    output/digest-YYYYMMDD.md   控制台同款 markdown 存档
    docs/entries/YYYYMMDD.json  当期摘要（feed 历史素材）
    docs/feed.xml               Atom feed（GitHub Pages 发布目录，给 Inoreader 订阅）
    seen.json                   已处理文章 ID（daily 模式默认启用去重）

环境变量一览：
    DIGEST_MODE     demo|daily（默认 demo）
    DEMO_MAX        demo 每源最多取几篇（默认 3）
    MAX_PER_FEED    daily 每源上限，防全量 feed 刷屏（默认 10）
    CONTENT_LIMIT   喂 DeepSeek 的正文上限字符（默认 6000）
    FETCH_THRESHOLD 正文 strip_html 后短于此字符数才触发补抓（默认 300）
    DEEPSEEK_MODEL  默认 deepseek-v4-flash（沿用你 demo；如用 instructions 的就设 deepseek-chat）
    SITE_URL        GitHub Pages 站点根，用于 feed 里的链接（默认占位，部署后改成你的）
    FEED_KEEP       feed 保留最近 N 期（默认 30）
    USE_SEEN        1|0，是否启用去重（默认 daily=1 / demo=0）
"""

import os, re, sys, time, json, html, hashlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import feedparser

TZ = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (compatible; RSSDigestBot/1.0; +https://github.com/yourname/rss-digest)"

# ---- 源清单（加源往这里加；fetch=True 表示正文过短时允许补抓）----
FEEDS = [
    {"name": "LessWrong", "url": "https://www.lesswrong.com/feed.xml",        "fetch": False},
    {"name": "阮一峰",     "url": "https://www.ruanyifeng.com/blog/atom.xml",  "fetch": False},
    # 补抓源示例（MVP 验证通过后加回）：
    # {"name": "少数派",           "url": "https://sspai.com/feed",               "fetch": True},
    # {"name": "Hugging Face",     "url": "https://huggingface.co/blog/feed.xml", "fetch": True},
    # {"name": "Our World in Data","url": "https://ourworldindata.org/atom.xml",  "fetch": True},
]

# ---- 配置 ----
MODE            = os.getenv("DIGEST_MODE", "demo").lower()       # demo | daily
DEMO_MAX        = int(os.getenv("DEMO_MAX", "3"))
MAX_PER_FEED    = int(os.getenv("MAX_PER_FEED", "10"))
CONTENT_LIMIT   = int(os.getenv("CONTENT_LIMIT", "6000"))
FETCH_THRESHOLD = int(os.getenv("FETCH_THRESHOLD", "300"))
DEEPSEEK_MODEL  = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
SITE_URL        = os.getenv("SITE_URL", "https://yourname.github.io/rss-digest").rstrip("/")
FEED_KEEP       = int(os.getenv("FEED_KEEP", "30"))
DRY_RUN         = os.getenv("DRY_RUN") == "1" or not os.getenv("DEEPSEEK_API_KEY")
USE_SEEN        = os.getenv("USE_SEEN", "1" if MODE == "daily" else "0") == "1"

DOCS      = Path("docs")
ENTRIES   = DOCS / "entries"
OUTPUT    = Path("output")
SEEN_FILE = Path("seen.json")

PROMPT_TMPL = """你是中文资讯解读助手，面向一位希望「不读原文也能拿到干货」的读者。下面是一篇文章（可能为英文）。请用中文输出两部分：

精要：3-5 句，讲清文章的核心论点，并务必带出文中的具体例子、数据或案例（原文有什么就讲什么，把抽象观点落到实处）。让读者读完这几句就抓住了文章最有价值的信息，而不是泛泛而谈、只有结论没有血肉。
延伸：1-3 句，发散地谈这篇文章引申出的思考——它能迁移到哪些领域、能解释什么现象、对实践有何可借鉴之处，或它最反直觉/最值得玩味的点。直接说洞见本身，禁止使用"对关注X的读者有什么启发""值得XX的人一看"这类套话句式，也不要硬贴金融/AI/科技标签。

只输出"精要："和"延伸："两段，不要其它内容。
若给定内容过短或仅为导语，就已有信息客观概括，绝不编造原文未出现的细节、数字或例子。

文章标题：{title}
文章内容：{content}"""


# ---------------- 工具 ----------------
def log(*a): print(*a, file=sys.stderr)

def strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()

def http_get(url: str, retries: int = 3) -> bytes:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last = e
            time.sleep(2 ** i)
    raise last

def entry_time(e) -> datetime | None:
    for k in ("published_parsed", "updated_parsed"):
        t = e.get(k)
        if t:
            return datetime(*t[:6], tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    return None

def entry_body(e) -> str:
    if e.get("content"):
        return e.content[0].value
    return e.get("summary", "") or e.get("description", "")

def entry_uid(e) -> str:
    raw = e.get("id") or e.get("link") or (e.get("title", "") + str(e.get("published", "")))
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


# ---------------- 补抓 ----------------
def fetch_fulltext(url: str) -> str:
    """正文过短时抓 HTML 全文。requests(正经UA+退避×3) → trafilatura.extract。"""
    import trafilatura
    raw = http_get(url)  # 复用带 UA + 指数退避的 http_get，不用 trafilatura.fetch_url
    text = trafilatura.extract(raw, include_comments=False, favor_recall=True)
    return (text or "").strip()


# ---------------- DeepSeek ----------------
def _parse_two(txt: str) -> tuple[str, str]:
    jing, yan, cur = [], [], None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("精要："):
            cur = jing; s = s[3:].strip()
            if s: jing.append(s)
        elif s.startswith("延伸："):
            cur = yan; s = s[3:].strip()
            if s: yan.append(s)
        elif s and cur is not None:
            cur.append(s)
    return (" ".join(jing) or txt.strip(), " ".join(yan))

def summarize(title: str, content: str) -> tuple[str, str]:
    if DRY_RUN:
        return (f"[占位精要] 《{title[:30]}》正文约 {len(content)} 字，填 key 后此处为带实例的真实精要。",
                "[占位延伸] DRY_RUN 未调用 DeepSeek。")
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                    base_url="https://api.deepseek.com/v1")
    prompt = PROMPT_TMPL.format(title=title, content=content[:CONTENT_LIMIT])
    for i in range(3):
        try:
            r = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5, max_tokens=900, timeout=120,
            )
            return _parse_two(r.choices[0].message.content.strip())
        except Exception as e:
            log(f"  ! DeepSeek 重试 {i+1}/3: {e}")
            time.sleep(2 ** i)
    return ("[精要失败]", "[延伸失败]")


# ---------------- 去重 ----------------
def load_seen() -> set:
    if USE_SEEN and SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text("utf-8")))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    if USE_SEEN:
        SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=0), "utf-8")


# ---------------- 采集 ----------------
def collect(seen: set):
    yesterday = (datetime.now(TZ) - timedelta(days=1)).date()
    sections, failed = [], []
    fetched_n = fetch_fail_n = 0

    for f in FEEDS:
        name, url, can_fetch = f["name"], f["url"], f.get("fetch", False)
        try:
            d = feedparser.parse(http_get(url))
            items = []
            for e in d.entries:
                uid = entry_uid(e)
                if USE_SEEN and uid in seen:
                    continue
                t = entry_time(e)
                if MODE == "daily":
                    if t is None or t.date() != yesterday:
                        continue
                items.append({
                    "uid": uid,
                    "title": e.get("title", "(无标题)"),
                    "link": e.get("link", ""),
                    "time": t,
                    "body": strip_html(entry_body(e)),
                    "_can_fetch": can_fetch,
                })
            items.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=TZ))
            limit = DEMO_MAX if MODE == "demo" else MAX_PER_FEED
            items = items[-limit:]  # 升序后取末尾 = 最新 limit 篇，仍保持时间从早到晚

            # 补抓：正文过短且该源允许 + 有 link
            for it in items:
                if it["_can_fetch"] and it["link"] and len(it["body"]) < FETCH_THRESHOLD:
                    try:
                        full = fetch_fulltext(it["link"])
                        if len(full) >= len(it["body"]):
                            it["body"] = full
                            it["fetched"] = True
                            fetched_n += 1
                    except Exception as fe:
                        it["fetch_failed"] = True
                        fetch_fail_n += 1
                        log(f"  ! 补抓失败 {it['link']}: {fe}")

            sections.append({"name": name, "items": items})
            log(f"[{name}] 取 {len(items)} 篇")
        except Exception as e:
            failed.append(name)
            sections.append({"name": name, "items": []})
            log(f"[{name}] 失败: {e}")

    return sections, failed, yesterday, fetched_n, fetch_fail_n


def summarize_all(sections):
    for sec in sections:
        for it in sec["items"]:
            it["zhai"], it["qifa"] = summarize(it["title"], it["body"])


# ---------------- 渲染 ----------------
def _stat_line(sections, failed, fetched_n, fetch_fail_n):
    total = sum(len(s["items"]) for s in sections)
    return (f"共 {len(sections)} 个源，{total} 篇文章，"
            f"补抓全文 {fetched_n} 篇/失败 {fetch_fail_n} 篇，"
            f"失败源：{failed or '无'}")

def render_md(sections, failed, yesterday, fetched_n, fetch_fail_n):
    title_date = yesterday if MODE == "daily" else datetime.now(TZ).date()
    out = [f"# 每日 RSS 摘要 · {title_date.strftime('%Y年%m月%d日')}",
           f"_模式：{MODE}{'（DRY_RUN 占位）' if DRY_RUN else ''}_\n"]
    for i, sec in enumerate(sections, 1):
        out.append(f"\n## {i}. 〔{sec['name']}〕\n")
        if not sec["items"]:
            out.append("　昨日无更新\n")
            continue
        for j, it in enumerate(sec["items"], 1):
            ts = it["time"].strftime("%m-%d %H:%M") if it["time"] else "时间缺失"
            tag = "（补抓全文）" if it.get("fetched") else ("（补抓失败）" if it.get("fetch_failed") else "")
            out.append(f"**第{j}篇：《{it['title']}》**　`{ts}`{tag}")
            out.append(f"精要：{it['zhai']}")
            out.append(f"延伸：{it['qifa']}\n")
    out.append(f"\n---\n{_stat_line(sections, failed, fetched_n, fetch_fail_n)}")
    return "\n".join(out)

def render_html(sections, failed, yesterday, fetched_n, fetch_fail_n):
    esc = lambda s: html.escape(s or "")
    title_date = yesterday if MODE == "daily" else datetime.now(TZ).date()
    parts = [f"<p><em>模式：{MODE}{'（DRY_RUN 占位）' if DRY_RUN else ''}</em></p>"]
    for i, sec in enumerate(sections, 1):
        parts.append(f"<h2>{i}. 〔{esc(sec['name'])}〕</h2>")
        if not sec["items"]:
            parts.append("<p>昨日无更新</p>")
            continue
        for j, it in enumerate(sec["items"], 1):
            ts = it["time"].strftime("%m-%d %H:%M") if it["time"] else "时间缺失"
            tag = "（补抓全文）" if it.get("fetched") else ("（补抓失败）" if it.get("fetch_failed") else "")
            link = f' <a href="{esc(it["link"])}">原文</a>' if it.get("link") else ""
            parts.append(
                f"<p><strong>第{j}篇：《{esc(it['title'])}》</strong> "
                f"<code>{ts}</code>{tag}{link}<br>"
                f"<strong>精要：</strong>{esc(it['zhai'])}<br>"
                f"<strong>延伸：</strong>{esc(it['qifa'])}</p>"
            )
    parts.append(f"<hr><p>{esc(_stat_line(sections, failed, fetched_n, fetch_fail_n))}</p>")
    return "\n".join(parts), title_date


# ---------------- 生成 Atom feed ----------------
def write_entry_and_feed(title_date, html_content):
    """把当期摘要落盘为 entries/{date}.json，再扫描最近 FEED_KEEP 期重建 docs/feed.xml。"""
    from feedgen.feed import FeedGenerator

    ENTRIES.mkdir(parents=True, exist_ok=True)
    date_str = title_date.strftime("%Y%m%d")
    title = f"每日 RSS 摘要 · {title_date.strftime('%Y年%m月%d日')}"
    now = datetime.now(TZ)
    (ENTRIES / f"{date_str}.json").write_text(json.dumps({
        "date": date_str, "title": title,
        "updated": now.isoformat(), "html": html_content,
    }, ensure_ascii=False), "utf-8")

    # 读全部历史，按日期倒序取最近 FEED_KEEP 期
    rows = []
    for p in ENTRIES.glob("*.json"):
        try:
            rows.append(json.loads(p.read_text("utf-8")))
        except Exception:
            pass
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows = rows[:FEED_KEEP]

    fg = FeedGenerator()
    fg.id(f"{SITE_URL}/feed.xml")
    fg.title("每日 RSS 摘要")
    fg.author({"name": "RSS Digest Bot"})
    fg.link(href=f"{SITE_URL}/feed.xml", rel="self")
    fg.link(href=SITE_URL, rel="alternate")
    fg.language("zh-CN")
    fg.updated(now)

    # 按日期升序 add_entry：feedgen 默认把后加的放前面，故最终最新在最上
    for r in sorted(rows, key=lambda x: x["date"]):
        fe = fg.add_entry()
        fe.id(f"{SITE_URL}/entries/{r['date']}")
        fe.title(r["title"])
        fe.link(href=f"{SITE_URL}/entries/{r['date']}.html")
        fe.updated(r["updated"])
        fe.content(r["html"], type="html")

    DOCS.mkdir(parents=True, exist_ok=True)
    fg.atom_file(str(DOCS / "feed.xml"))
    # 顺手把当期 HTML 落一份独立页，便于 Pages 直链（Inoreader 不依赖它，可读 feed 内嵌全文）
    (ENTRIES / f"{date_str}.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<h1>{html.escape(title)}</h1>\n{html_content}", "utf-8")


# ---------------- 主流程 ----------------
def main():
    log(f"=== MODE={MODE} DRY_RUN={DRY_RUN} USE_SEEN={USE_SEEN} MODEL={DEEPSEEK_MODEL} ===")
    seen = load_seen()

    sections, failed, yesterday, fetched_n, fetch_fail_n = collect(seen)
    summarize_all(sections)

    md = render_md(sections, failed, yesterday, fetched_n, fetch_fail_n)
    html_content, title_date = render_html(sections, failed, yesterday, fetched_n, fetch_fail_n)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    fn = OUTPUT / f"digest-{datetime.now(TZ).strftime('%Y%m%d')}.md"
    fn.write_text(md, "utf-8")

    write_entry_and_feed(title_date, html_content)

    # 处理成功后再写 seen（失败的不计入，留待下轮重试）
    for sec in sections:
        for it in sec["items"]:
            seen.add(it["uid"])
    save_seen(seen)

    print(md)
    log(f"\n已写入 {fn}  |  docs/feed.xml 已更新")


if __name__ == "__main__":
    main()
