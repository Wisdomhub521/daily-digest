#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 每日摘要 —— 完整版 v3（抓取 / 容错 / 时间窗筛选 / 补抓 / DeepSeek 摘要 / 生成 Atom feed / 去重）

本版相对 v2 的改动（综合一轮调试）：
  1. UA 按域名分流：substack 类源走浏览器 UA + 完整 Accept 头，规避默认 bot UA 的 403。
  2. docs/entries 启动期自检 sanitize_entries()：
        - 解析失败 / 非 dict 的坏文件 → 隔离进 _broken/（根治 KeyError: 'key'）
        - 缺字段 → 用文件名 stem 等兜底补齐并回写
        - 顶层 entry 超过 FEED_KEEP 期 → 旧的归档进 _archive/（解决目录长期膨胀）
  3. write_entry_and_feed 读历史 entry 仍保留容错兜底，双保险。

源：6 个「直接用」+ 4 个「补抓」，内联在 FEEDS（fetch=True 表示正文过短时补抓全文）。

依赖：
    pip install feedparser requests trafilatura feedgen openai

三种模式（DIGEST_MODE）：
    demo      （默认）不卡日期，每源取最新 N 篇 —— 验证拼版用
    daily                只取「昨天(Asia/Shanghai)」发布的，适合每天清晨一次汇总
    rolling              取「最近 LOOKBACK_HOURS 小时」发布的 + 去重增量，适合一天多次更新

跑法：
    export DEEPSEEK_API_KEY=sk-xxxx
    DIGEST_MODE=rolling python main.py        # 生产：一天两跑用这个
    DRY_RUN=1 DIGEST_MODE=demo python main.py # 跳过 DeepSeek 看拼版/feed

产物：
    output/digest-<run_key>.md   控制台同款 markdown 存档
    docs/entries/<run_key>.json  当次摘要（feed 历史素材；rolling 下 run_key 含时分，一天多跑互不覆盖）
    docs/entries/_broken/        被隔离的坏 entry（确认无用后可手动删）
    docs/entries/_archive/       超出 FEED_KEEP 期的旧 entry（归档，不进 feed）
    docs/feed.xml                Atom feed（GitHub Pages 发布目录，给 Inoreader 订阅）
    seen.json                    已处理文章 ID（daily/rolling 默认启用去重）

环境变量：
    DIGEST_MODE     demo|daily|rolling（默认 demo）
    LOOKBACK_HOURS  rolling 窗口小时数（默认 30，两跑间隔 10h，留足重叠兜底防漏）
    DEMO_MAX        demo 每源最多取几篇（默认 3）
    MAX_PER_FEED    daily/rolling 每源上限，防全量 feed 刷屏（默认 10）
    CONTENT_LIMIT   喂 DeepSeek 的正文上限字符（默认 6000）
    FETCH_THRESHOLD 正文 strip_html 后短于此才补抓（默认 300）
    DEEPSEEK_MODEL  默认 deepseek-v4-flash
    SITE_URL        GitHub Pages 站点根（部署后改成你的）
    FEED_KEEP       feed 保留最近 N 期（默认 40），同时作为 entries 归档阈值
    USE_SEEN        1|0（默认 demo=0 / 其余=1）
"""

import os, re, sys, time, json, html, hashlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
import feedparser

TZ = ZoneInfo("Asia/Shanghai")

# ---- UA 按域名分流 ----
UA_BOT = "Mozilla/5.0 (compatible; RSSDigestBot/1.0; +https://github.com/yourname/rss-digest)"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# 对默认 bot UA 敏感、易 403 的域名走浏览器 UA（custom-domain 的 substack 不在此列，按需补）
BROWSER_UA_HOSTS = ("substack.com",)

def pick_ua(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return UA_BROWSER if host.endswith(BROWSER_UA_HOSTS) else UA_BOT

# ---- 源清单（fetch=True：正文过短时补抓全文）----
FEEDS = [
    # —— 直接用（feed 自带全文或摘要够长）——
    {"name": "LessWrong",          "url": "https://www.lesswrong.com/feed.xml",         "fetch": False},
    # —— 补抓（feed 正文过短，触发全文抓取）——
    {"name": "Hugging Face",       "url": "https://huggingface.co/blog/feed.xml",        "fetch": True},
    {"name": "少数派",             "url": "https://sspai.com/feed",                      "fetch": True},
    # === BEGIN AUTO-ADDED（check_feed.py --write 自动写入区，勿删这对锚点）===
    # === END AUTO-ADDED ===
]

# ---- 配置 ----
MODE            = os.getenv("DIGEST_MODE", "demo").lower()       # demo | daily | rolling
LOOKBACK_HOURS  = int(os.getenv("LOOKBACK_HOURS", "30"))
DEMO_MAX        = int(os.getenv("DEMO_MAX", "3"))
MAX_PER_FEED    = int(os.getenv("MAX_PER_FEED", "10"))
CONTENT_LIMIT   = int(os.getenv("CONTENT_LIMIT", "6000"))
FETCH_THRESHOLD = int(os.getenv("FETCH_THRESHOLD", "300"))
DEEPSEEK_MODEL  = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
SITE_URL        = os.getenv("SITE_URL", "https://yourname.github.io/rss-digest").rstrip("/")
FEED_KEEP       = int(os.getenv("FEED_KEEP", "40"))
DRY_RUN         = os.getenv("DRY_RUN") == "1" or not os.getenv("DEEPSEEK_API_KEY")
USE_SEEN        = os.getenv("USE_SEEN", "0" if MODE == "demo" else "1") == "1"

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

def http_get(url: str, retries: int = 3, ua: str | None = None) -> bytes:
    headers = {
        "User-Agent": ua or pick_ua(url),
        "Accept": ("application/rss+xml, application/atom+xml, application/xml, "
                   "text/xml, text/html;q=0.9, */*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    }
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
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
    """正文过短时抓 HTML 全文。requests(正经UA+退避×3) → trafilatura.extract，不用 fetch_url。"""
    import trafilatura
    raw = http_get(url)
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


# ---------------- entries 目录自检 + 归档 ----------------
def sanitize_entries():
    """启动期自检 + 归档，根治 docs/entries 目录：
    1) 解析失败 / 非 dict → 移入 _broken/（根治 KeyError: 'key'）
    2) 缺字段 → 用文件名 stem 等兜底补齐并回写
    3) 顶层 entry 超过 FEED_KEEP 期 → 旧的移入 _archive/（feed 只取最近 FEED_KEEP，归档不影响展示）
    glob('*.json') 只扫顶层，_broken/ 与 _archive/ 下文件不再被读取。
    返回 (repaired, quarantined, archived)。
    """
    if not ENTRIES.exists():
        return 0, 0, 0
    broken  = ENTRIES / "_broken"
    archive = ENTRIES / "_archive"
    repaired = quarantined = archived = 0
    now_iso = datetime.now(TZ).isoformat()

    valid = []  # [(key, path), ...]
    for p in ENTRIES.glob("*.json"):
        try:
            r = json.loads(p.read_text("utf-8"))
            if not isinstance(r, dict):
                raise ValueError("not a dict")
        except Exception as e:
            broken.mkdir(parents=True, exist_ok=True)
            p.rename(broken / p.name)
            quarantined += 1
            log(f"  ! 隔离坏 entry → _broken/{p.name}（{e}）")
            continue
        changed = False
        if not r.get("key"):     r["key"] = p.stem;      changed = True
        if not r.get("title"):   r["title"] = r["key"];  changed = True
        if not r.get("updated"): r["updated"] = now_iso; changed = True
        if "html" not in r:      r["html"] = "";          changed = True
        if changed:
            p.write_text(json.dumps(r, ensure_ascii=False), "utf-8")
            repaired += 1
            log(f"  ~ 修复 entry {p.name}")
        valid.append((r["key"], p))

    # 归档：按 key 倒序保留最近 FEED_KEEP 期，其余移入 _archive/
    valid.sort(key=lambda x: x[0], reverse=True)
    for key, p in valid[FEED_KEEP:]:
        archive.mkdir(parents=True, exist_ok=True)
        p.rename(archive / p.name)
        archived += 1

    if repaired or quarantined or archived:
        log(f"  entries 自检：修复 {repaired}，隔离 {quarantined}，归档 {archived}")
    return repaired, quarantined, archived


# ---------------- 运行标识 ----------------
def run_meta():
    """返回 (run_key, run_title, disp_date)。
    daily：一天一篇，key/标题按昨天日期；其余：key 含时分，一天多跑互不覆盖。"""
    now = datetime.now(TZ)
    if MODE == "daily":
        d = (now - timedelta(days=1)).date()
        return d.strftime("%Y%m%d"), f"每日 RSS 摘要 · {d.strftime('%Y年%m月%d日')}", d
    return (now.strftime("%Y%m%d-%H%M"),
            f"RSS 摘要 · {now.strftime('%Y年%m月%d日 %H:%M')}", now.date())


# ---------------- 采集 ----------------
def collect(seen: set):
    now       = datetime.now(TZ)
    yesterday = (now - timedelta(days=1)).date()
    cutoff    = now - timedelta(hours=LOOKBACK_HOURS)
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
                elif MODE == "rolling":
                    if t is None or t < cutoff:
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

            for it in items:  # 补抓：正文过短且该源允许且有 link
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

    return sections, failed, fetched_n, fetch_fail_n


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

def render_md(sections, failed, fetched_n, fetch_fail_n, run_title):
    out = [f"# {run_title}",
           f"_模式：{MODE}{'（DRY_RUN 占位）' if DRY_RUN else ''}_\n"]
    for i, sec in enumerate(sections, 1):
        out.append(f"\n## {i}. 〔{sec['name']}〕\n")
        if not sec["items"]:
            out.append("　无更新\n")
            continue
        for j, it in enumerate(sec["items"], 1):
            ts = it["time"].strftime("%m-%d %H:%M") if it["time"] else "时间缺失"
            tag = "（补抓全文）" if it.get("fetched") else ("（补抓失败）" if it.get("fetch_failed") else "")
            out.append(f"**第{j}篇：《{it['title']}》**　`{ts}`{tag}")
            out.append(f"精要：{it['zhai']}")
            out.append(f"延伸：{it['qifa']}\n")
    out.append(f"\n---\n{_stat_line(sections, failed, fetched_n, fetch_fail_n)}")
    return "\n".join(out)

def render_html(sections, failed, fetched_n, fetch_fail_n):
    esc = lambda s: html.escape(s or "")
    parts = [f"<p><em>模式：{MODE}{'（DRY_RUN 占位）' if DRY_RUN else ''}</em></p>"]
    for i, sec in enumerate(sections, 1):
        parts.append(f"<h2>{i}. 〔{esc(sec['name'])}〕</h2>")
        if not sec["items"]:
            parts.append("<p>无更新</p>")
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
    return "\n".join(parts)


# ---------------- 生成 Atom feed ----------------
def write_entry_and_feed(run_key, run_title, html_content):
    """先 sanitize_entries() 自检+归档，再落盘 entries/<run_key>.json，
    最后扫描最近 FEED_KEEP 期重建 docs/feed.xml。"""
    from feedgen.feed import FeedGenerator

    ENTRIES.mkdir(parents=True, exist_ok=True)
    sanitize_entries()          # ★ 每轮先清理历史残文件 + 归档过期，再写新 entry / 重建 feed

    now = datetime.now(TZ)
    (ENTRIES / f"{run_key}.json").write_text(json.dumps({
        "key": run_key, "title": run_title,
        "updated": now.isoformat(), "html": html_content,
    }, ensure_ascii=False), "utf-8")

    rows = []
    for p in ENTRIES.glob("*.json"):     # 只扫顶层；_broken/_archive 不参与
        try:
            r = json.loads(p.read_text("utf-8"))
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        r.setdefault("key", p.stem)       # 容错兜底，双保险
        r.setdefault("title", r["key"])
        r.setdefault("updated", now.isoformat())
        r.setdefault("html", "")
        rows.append(r)
    rows.sort(key=lambda r: r["key"], reverse=True)
    rows = rows[:FEED_KEEP]

    fg = FeedGenerator()
    fg.id(f"{SITE_URL}/feed.xml")
    fg.title("每日 RSS 摘要")
    fg.author({"name": "RSS Digest Bot"})
    fg.link(href=f"{SITE_URL}/feed.xml", rel="self")
    fg.link(href=SITE_URL, rel="alternate")
    fg.language("zh-CN")
    fg.updated(now)

    for r in sorted(rows, key=lambda x: x["key"]):  # 升序加，feedgen 把最新放最上
        fe = fg.add_entry()
        fe.id(f"{SITE_URL}/entries/{r['key']}")
        fe.title(r["title"])
        fe.link(href=f"{SITE_URL}/entries/{r['key']}.html")
        fe.updated(r["updated"])
        fe.content(r["html"], type="html")

    DOCS.mkdir(parents=True, exist_ok=True)
    fg.atom_file(str(DOCS / "feed.xml"))
    (ENTRIES / f"{run_key}.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(run_title)}</title>"
        f"<h1>{html.escape(run_title)}</h1>\n{html_content}", "utf-8")


# ---------------- 主流程 ----------------
def main():
    log(f"=== MODE={MODE} DRY_RUN={DRY_RUN} USE_SEEN={USE_SEEN} MODEL={DEEPSEEK_MODEL} ===")
    seen = load_seen()
    run_key, run_title, _ = run_meta()

    sections, failed, fetched_n, fetch_fail_n = collect(seen)

    # ★ 护栏：本轮 0 篇新文章 → 不调 DeepSeek、不写 docs/output、不推送。
    #   备用触发在主跑已成功时空跑即退（文章已进 seen），不刷空摘要；
    #   主跑被跳过时文章仍在 LOOKBACK 窗口内，备用触发正常补出摘要。
    total_new = sum(len(s["items"]) for s in sections)
    if total_new == 0:
        log("本轮 0 篇新文章 → 跳过摘要/feed 生成与推送（备用触发空跑无害）。")
        return

    summarize_all(sections)

    md   = render_md(sections, failed, fetched_n, fetch_fail_n, run_title)
    html_content = render_html(sections, failed, fetched_n, fetch_fail_n)

    OUTPUT.mkdir(parents=True, exist_ok=True)            # ← output 段，保持不变
    (OUTPUT / f"digest-{run_key}.md").write_text(md, "utf-8")  # ← 仅本地存档，不进仓库

    write_entry_and_feed(run_key, run_title, html_content)

    for sec in sections:  # 成功处理的才计入 seen
        for it in sec["items"]:
            seen.add(it["uid"])
    save_seen(seen)

    print(md)
    log(f"\n已写入 output/digest-{run_key}.md  |  docs/feed.xml 已更新")
if __name__ == "__main__":
    main()
