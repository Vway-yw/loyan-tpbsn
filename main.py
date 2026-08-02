"""逃跑吧少年热点插件 — 热点列表 + 输序号查看正文详情 + 通用兑换码"""
import asyncio
import html as html_lib
import re
import time
import urllib.parse
import urllib.request
import http.cookiejar
from typing import Dict, List, Optional

from loyan.core.decorators import on_command, plugin_handler, PluginContext
from graci import get_logger

logger = get_logger("逃跑吧少年")

SEARCH_URL = "https://weixin.sogou.com/weixin?type=2&query={query}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 15
MAX_ITEMS = 10
LIST_TTL = 1800
DETAIL_TTL = 600

_cache: Dict[str, tuple] = {}
_detail_cache: Dict[str, tuple] = {}

TOPICS = {
    "hot": ("🔥 逃跑吧少年热点", "逃跑吧少年"),
    "leak": ("⚡ 逃跑吧少年最新爆料", "逃跑吧少年 爆料 更新 活动"),
    "guide": ("📖 逃跑吧少年攻略", "逃跑吧少年 攻略"),
}


def _strip(tag_text: str) -> str:
    t = re.sub(r"<[^>]+>", "", tag_text)
    return html_lib.unescape(t).strip()


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://weixin.sogou.com/"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_list(html: str, limit: int = MAX_ITEMS) -> List[Dict]:
    items = []
    for m in re.finditer(r'<div class="txt-box">(.*?)</li>', html, re.S):
        seg = m.group(1)
        title_m = re.search(r'<a[^>]*uigs="article_title_\d+"[^>]*>(.*?)</a>', seg, re.S)
        href_m = re.search(r'<a[^>]*href="(/link\?url=[^"]+)"', seg)
        acct_m = re.search(r'class="all-time-y2">([^<]+)</span>', seg)
        ts_m = re.search(r"timeConvert\('(\d+)'\)", seg)
        summary_m = re.search(r'class="txt-info"[^>]*>(.*?)</p>', seg, re.S)
        if not title_m:
            continue
        item = {
            "title": _strip(title_m.group(1)),
            "href": href_m.group(1) if href_m else "",
            "account": acct_m.group(1).strip() if acct_m else "",
            "time": time.strftime("%m-%d %H:%M", time.localtime(int(ts_m.group(1)))) if ts_m else "",
            "summary": _strip(summary_m.group(1)) if summary_m else "",
        }
        if item["title"]:
            items.append(item)
        if len(items) >= limit:
            break
    return items


async def _get_list(query: str) -> Optional[List[Dict]]:
    now = time.time()
    cached = _cache.get(query)
    if cached and now - cached[0] < LIST_TTL:
        return cached[1]
    html = await asyncio.to_thread(_fetch, SEARCH_URL.format(query=urllib.parse.quote(query)))
    items = _parse_list(html)
    if items:
        _cache[query] = (now, items)
    return items or None


async def _get_detail(href: str) -> Optional[str]:
    if not href:
        return None
    now = time.time()
    cached = _detail_cache.get(href)
    if cached and now - cached[0] < DETAIL_TTL:
        return cached[1]
    try:
        text = await asyncio.to_thread(_fetch_detail, href)
        if text:
            _detail_cache[href] = (now, text)
        return text
    except Exception as e:
        logger.error(f"抓取正文失败: {e}")
        return None


def _fetch_detail(href: str) -> Optional[str]:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    link = "https://weixin.sogou.com" + href.replace("&amp;", "&")
    r = opener.open(urllib.request.Request(link, headers={"User-Agent": UA}), timeout=TIMEOUT)
    mid = r.read().decode("utf-8", errors="replace")
    parts = re.findall(r"url\s*\+?=\s*'([^']*)'", mid)
    final = "".join(parts)
    if not final.startswith("http"):
        return None
    r2 = opener.open(urllib.request.Request(final, headers={"User-Agent": UA}), timeout=TIMEOUT)
    body = r2.read().decode("utf-8", errors="replace")
    content = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*<script', body, re.S)
    if not content:
        return None
    text = re.sub(r"<[^>]+>", "", content.group(1))
    text = html_lib.unescape(text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text or None


def _extract_codes(text: str) -> List[str]:
    """从文本中提取疑似兑换码"""
    codes = []
    seen = set()
    for m in re.finditer(r"(?:兑换码|激活码|暗号|礼包码|福利码)\s*[:：]?\s*([A-Za-z0-9]{4,25})", text):
        c = m.group(1)
        if c not in seen:
            seen.add(c)
            codes.append(c)
    for m in re.finditer(r"\b[A-Za-z0-9]{5,20}\b", text):
        c = m.group(0)
        if c in seen:
            continue
        if not any(ch.isdigit() for ch in c):
            continue
        seen.add(c)
        codes.append(c)
    return codes[:6]


def _pick_page(ctx, maxn: int) -> Optional[tuple]:
    """解析 序号 [页码]：返回 (序号, 页码)"""
    rest = (ctx.raw_text or "").strip()
    parts = rest.split(None, 2)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return None
    idx = int(parts[1].strip())
    if not (1 <= idx <= maxn):
        return None
    page = 1
    if len(parts) > 2 and parts[2].strip().isdigit():
        page = max(1, int(parts[2].strip()))
    return (idx, page)


PAGE_SIZE = 800


async def _handle_topic(ctx: PluginContext, topic: str, extra: str = ""):
    title, query = TOPICS[topic]
    if extra:
        query += " " + extra
    items = await _get_list(query)
    if not items:
        await ctx.reply("😢 暂时没有获取到内容，请稍后再试")
        return
    pick = _pick_page(ctx, len(items))
    if pick:
        idx, page = pick
        it = items[idx - 1]
        text = await _get_detail(it["href"])
        lines = [f"📄 {it['title']}", "━━━━━━━━━━━━"]
        if text:
            total = max(1, (len(text) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, total)
            start = (page - 1) * PAGE_SIZE
            lines.append(text[start:start + PAGE_SIZE])
            lines.append("━━━━━━━━━━━━")
            lines.append(f"📄 第 {page}/{total} 页 · 📌 {' | '.join(x for x in (it.get('account'), it.get('time')) if x)}")
            if page < total:
                lines.append(f"💡 继续看：{ctx.command} {idx} {page + 1}")
        else:
            lines.append("（正文获取失败）")
        await ctx.reply("\n".join(lines))
        return
    lines = [title, "━━━━━━━━━━━━"]
    for i, it in enumerate(items, 1):
        t = it['title'] if len(it['title']) <= 30 else it['title'][:30] + '…'
        lines.append(f"{i}. {t}")
    lines.append("━━━━━━━━━━━━")
    lines.append(f"💡 回复 {ctx.command} 序号（如 1）查看正文")
    await ctx.reply("\n".join(lines))


@on_command("/逃跑热点", "/逃跑吧少年热点", "/逃跑资讯")
@plugin_handler
async def handle_tpbsn(ctx: PluginContext):
    """查看逃跑吧少年热点（输序号看正文）"""
    await ctx.reply("🔥 正在获取逃跑吧少年热点...")
    try:
        await _handle_topic(ctx, "hot")
    except Exception as e:
        logger.error(f"逃跑热点失败: {e}")
        await ctx.reply("❌ 获取失败，请稍后再试")


@on_command("/逃跑爆料", "/逃跑更新", "/逃跑吧少年爆料")
@plugin_handler
async def handle_tpbsn_leak(ctx: PluginContext):
    """查看逃跑吧少年爆料（输序号看正文）"""
    await ctx.reply("⚡ 正在获取逃跑吧少年爆料...")
    try:
        await _handle_topic(ctx, "leak")
    except Exception as e:
        logger.error(f"逃跑爆料失败: {e}")
        await ctx.reply("❌ 获取失败，请稍后再试")


@on_command("/逃跑攻略", "/逃跑吧少年攻略")
@plugin_handler
async def handle_tpbsn_guide(ctx: PluginContext):
    """查看逃跑吧少年攻略（输序号看正文；/逃跑攻略 <内容> 定向）"""
    rest = (ctx.raw_text or "").strip()
    parts = rest.split(None, 1)
    kw = parts[1].strip() if len(parts) > 1 else ""
    await ctx.reply("📖 正在获取逃跑吧少年攻略...")
    try:
        await _handle_topic(ctx, "guide", kw)
    except Exception as e:
        logger.error(f"逃跑攻略失败: {e}")
        await ctx.reply("❌ 获取失败，请稍后再试")


@on_command("/逃跑兑换码", "/逃跑码", "/逃跑吧少年兑换码", "/兑换码")
@plugin_handler
async def handle_tpbsn_code(ctx: PluginContext):
    """查看逃跑吧少年最新通用兑换码"""
    await ctx.reply("🎁 正在获取逃跑吧少年兑换码...")
    try:
        items = await _get_list("逃跑吧少年 兑换码 礼包")
        if not items:
            await ctx.reply("😢 暂时没有获取到兑换码，请稍后再试")
            return
        lines = ["🎁 逃跑吧少年 通用兑换码", "━━━━━━━━━━━━"]
        found = 0
        for i, it in enumerate(items, 1):
            t = it['title'] if len(it['title']) <= 30 else it['title'][:30] + '…'
            lines.append(f"{i}. {t}")
            codes = _extract_codes(it.get("summary", ""))
            if codes:
                found += 1
                lines.append(f"   🏷️ {' '.join(codes[:2])}")
        lines.append("━━━━━━━━━━━━")
        if found == 0:
            lines.append("⚠️ 未找到明确兑换码，可回复序号查看正文找码")
        lines.append("⚠️ 兑换码有时效，请尽快使用")
        await ctx.reply("\n".join(lines))
    except Exception as e:
        logger.error(f"获取逃跑兑换码失败: {e}")
        await ctx.reply("❌ 获取失败，请稍后再试")
