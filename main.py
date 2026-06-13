#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 每日摘要 —— MVP DEMO（LessWrong + 阮一峰）
填入 DEEPSEEK_API_KEY 即可即刻运行：

    pip install feedparser openai
    export DEEPSEEK_API_KEY=sk-xxxx        # Windows: set DEEPSEEK_API_KEY=sk-xxxx
    python main.py

不想花钱先看拼版效果：
    DRY_RUN=1 python main.py              # 跳过 DeepSeek，用占位摘要

模式：
    DIGEST_MODE=demo （默认）每源取最新 N 篇，不卡日期 —— 保证有内容可看
    DIGEST_MODE=daily          只取「昨天（Asia/Shanghai）」发布的文章
"""

import os, re, sys, time, html, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser

TZ = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (compatible; RSSDigestBot/1.0; +https://github.com/yourname/rss-digest)"

# ---- DEMO 两源（正式版改为读 feeds.opml）----
FEEDS = [
    {"name": "LessWrong", "url": "https://www.lesswrong.com/feed.xml"},
    {"name": "阮一峰",     "url": "https://www.ruanyifeng.com/blog/atom.xml"},
]

MODE          = os.getenv("DIGEST_MODE", "demo").lower()   # demo | daily
DEMO_MAX      = int(os.getenv("DEMO_MAX", "3"))            # demo 每源最多取几篇
MAX_PER_FEED  = int(os.getenv("MAX_PER_FEED", "10"))       # daily 每源上限（防刷屏）
CONTENT_LIMIT = 6000                                       # 喂 DeepSeek 的正文上限
DRY_RUN       = os.getenv("DRY_RUN") == "1" or not os.getenv("DEEPSEEK_API_KEY")

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
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=30).read()
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


# ---------------- DeepSeek ----------------
def _parse_two(txt: str) -> tuple[str, str]:
    """解析"精要：…（可多行）"和"延伸：…（可多行）"。"""
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
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5, max_tokens=900, timeout=120,
            )
            return _parse_two(r.choices[0].message.content.strip())
        except Exception as e:
            log(f"  ! DeepSeek 重试 {i+1}/3: {e}")
            time.sleep(2 ** i)
    return ("[精要失败]", "[延伸失败]")


# ---------------- 主流程 ----------------
def collect():
    yesterday = (datetime.now(TZ) - timedelta(days=1)).date()
    sections, failed = [], []
    for f in FEEDS:
        name, url = f["name"], f["url"]
        try:
            d = feedparser.parse(http_get(url))
            items = []
            for e in d.entries:
                t = entry_time(e)
                if MODE == "daily":
                    if t is None or t.date() != yesterday:
                        continue
                items.append({"title": e.get("title", "(无标题)"),
                              "time": t, "body": strip_html(entry_body(e))})
            items.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=TZ))
            limit = DEMO_MAX if MODE == "demo" else MAX_PER_FEED
            items = items[-limit:]   # 升序后取末尾=最新 limit 篇，仍保持时间从早到晚
            sections.append({"name": name, "items": items})
            log(f"[{name}] 取 {len(items)} 篇")
        except Exception as e:
            failed.append(name)
            sections.append({"name": name, "items": []})
            log(f"[{name}] 失败: {e}")
    return sections, failed, yesterday


def render(sections, failed, yesterday):
    title_date = yesterday if MODE == "daily" else datetime.now(TZ).date()
    out = [f"# 每日 RSS 摘要 · {title_date.strftime('%Y年%m月%d日')}",
           f"_模式：{MODE}{'（DRY_RUN 占位）' if DRY_RUN else ''}_\n"]
    total = 0
    for i, sec in enumerate(sections, 1):
        out.append(f"\n## {i}. 〔{sec['name']}〕\n")
        if not sec["items"]:
            out.append("　昨日无更新\n")
            continue
        for j, it in enumerate(sec["items"], 1):
            total += 1
            zhai, qifa = summarize(it["title"], it["body"])
            ts = it["time"].strftime("%m-%d %H:%M") if it["time"] else "时间缺失"
            out.append(f"**第{j}篇：《{it['title']}》**　`{ts}`")
            out.append(f"精要：{zhai}")
            out.append(f"延伸：{qifa}\n")
    out.append(f"\n---\n共 {len(sections)} 个源，{total} 篇文章，"
               f"失败 {len(failed)} 源：{failed or '无'}")
    return "\n".join(out)


def main():
    log(f"=== MODE={MODE} DRY_RUN={DRY_RUN} ===")
    sections, failed, yesterday = collect()
    md = render(sections, failed, yesterday)
    os.makedirs("output", exist_ok=True)
    fn = f"output/digest-{datetime.now(TZ).strftime('%Y%m%d')}.md"
    with open(fn, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    log(f"\n已写入 {fn}")


if __name__ == "__main__":
    main()
