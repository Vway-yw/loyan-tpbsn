"""逃跑吧少年热点插件 — 热点列表 + 序号/翻页命令看正文（按时间排序过滤旧文）"""
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
from loyan.plugins.core.reading import get_reading, set_reading

logger = get_logger("逃跑吧少年")

SEARCH_URL = "https://weixin.sogou.com/weixin?type=2&query={query}"
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
]
TIMEOUT = 15
MAX_ITEMS = 10
MAX_AGE = 45 * 86400  # 只显示 45 天内的文章（时效）
LIST_TTL = 900        # 列表缓存 15 分钟（时效优先）
DETAIL_TTL = 600
PAGE_SIZE = 500

_cache: Dict[str, tuple] = {}
_detail_cache: Dict[str, tuple] = {}


TOPICS = {
    "hot": ("🔥 逃跑吧少年热点", "逃跑吧少年"),
    "leak": ("⚡ 逃跑吧少年最新爆料", "逃跑吧少年 爆料 更新 活动"),
    "guide": ("📖 逃跑吧少年攻略", "逃跑吧少年 攻略"),
    "code": ("🎁 逃跑吧少年 通用兑换码", "逃跑吧少年 兑换码 礼包"),
}


def _strip(tag_text: str) -> str:
    t = re.sub(r"<[^>]+>", "", tag_text)
    return html_lib.unescape(t).strip()


def _fetch(url: str, ua: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Referer": "https://weixin.sogou.com/"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_list(html: str, limit: int = MAX_ITEMS) -> List[Dict]:
    """解析搜索：标题+链接+时间；优先过滤旧文，若过滤后为空则全部返回（保证有内容）"""
    now = time.time()
    all_items = []
    for m in re.finditer(r'<div class="txt-box">(.*?)</li>', html, re.S):
        seg = m.group(1)
        title_m = re.search(r'<a[^>]*uigs="article_title_\d+"[^>]*>(.*?)</a>', seg, re.S)
        href_m = re.search(r'<a[^>]*href="(/link\?url=[^"]+)"', seg)
        acct_m = re.search(r'class="all-time-y2">([^<]+)</span>', seg)
        ts_m = re.search(r"timeConvert\('(\d+)'\)", seg)
        if not title_m:
            continue
        ts = int(ts_m.group(1)) if ts_m else 0
        summary_m = re.search(r'class="txt-info"[^>]*>(.*?)</p>', seg, re.S)
        item = {
            "title": _strip(title_m.group(1)),
            "href": href_m.group(1) if href_m else "",
            "summary": _strip(summary_m.group(1)) if summary_m else "",
            "account": acct_m.group(1).strip() if acct_m else "",
            "time": time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "",
            "ts": ts,
        }
        if item["title"] and item["href"]:
            all_items.append(item)
        if len(all_items) >= limit:
            break
    if not all_items:
        return []
    fresh = [i for i in all_items if i["ts"] and (now - i["ts"]) <= MAX_AGE]
    if not fresh:
        # 无新内容：全部显示（保证有内容，带时间标注）
        fresh = all_items
    fresh.sort(key=lambda x: x["ts"], reverse=True)
    return fresh[:limit]


async def _get_list(query: str) -> Optional[List[Dict]]:
    """带缓存获取列表，多 UA 轮换"""
    now = time.time()
    cached = _cache.get(query)
    if cached and now - cached[0] < LIST_TTL:
        return cached[1]
    items = None
    for ua in UA_LIST:
        try:
            html = await asyncio.to_thread(_fetch, SEARCH_URL.format(query=urllib.parse.quote(query)), ua)
            items = _parse_list(html)
            if items:
                break
        except Exception:
            continue
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
    text = None
    for ua in UA_LIST:
        try:
            text = await asyncio.to_thread(_fetch_detail, href, ua)
            if text:
                break
        except Exception:
            continue
    if text:
        _detail_cache[href] = (now, text)
    return text  # 失败时返回 None，调用方用摘要降级


def _fetch_detail(href: str, ua: str) -> Optional[str]:
    """同步抓正文：先建立搜狗会话（cookie），再跟随中间页 JS 拼接 mp.weixin.qq.com 地址"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # 1. 先访问搜索页建立会话 cookie（否则 link 跳转被反爬拦截）
    try:
        opener.open(urllib.request.Request(SEARCH_URL.format(query=urllib.parse.quote("热门")), headers=headers), timeout=TIMEOUT).read()
    except Exception:
        pass
    # 2. 访问中间页
    link = "https://weixin.sogou.com" + href.replace("&amp;", "&")
    h2 = dict(headers)
    h2["Referer"] = SEARCH_URL.format(query=urllib.parse.quote("热门"))
    r = opener.open(urllib.request.Request(link, headers=h2), timeout=TIMEOUT)
    mid = r.read().decode("utf-8", errors="replace")
    parts = re.findall(r"url\s*\+?=\s*'([^']*)'", mid)
    final = "".join(parts)
    if not final.startswith("http"):
        return None
    # 3. 访问正文页
    h3 = dict(headers)
    h3["Referer"] = link
    r2 = opener.open(urllib.request.Request(final, headers=h3), timeout=TIMEOUT)
    body = r2.read().decode("utf-8", errors="replace")
    content = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*<script', body, re.S)
    if not content:
        return None
    text = re.sub(r"<[^>]+>", "", content.group(1))
    text = html_lib.unescape(text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text or None


async def _show_list(ctx: PluginContext, topic: str, extra: str = ""):
    """显示列表"""
    title, query = TOPICS[topic]
    if extra:
        query += " " + extra
    items = await _get_list(query)
    if not items:
        await ctx.reply("😢 暂时没有获取到内容，请稍后再试")
        return
    uid = str(getattr(ctx, "sender_id", "") or "")
    set_reading(uid, {"topic": topic, "extra": extra, "items": items, "page": 1})
    lines = [title, "━━━━━━━━━━━━"]
    for i, it in enumerate(items, 1):
        t = it['title'] if len(it['title']) <= 30 else it['title'][:30] + '…'
        tm = it.get('time', '')
        lines.append(f"{i}. {t}  [{tm}]")
    lines.append("━━━━━━━━━━━━")
    lines.append("💡 回复序号看正文，如 1")
    await ctx.reply("\n".join(lines))


async def _show_detail(ctx: PluginContext, idx: int, page: int):
    """显示正文某页"""
    uid = str(getattr(ctx, "sender_id", "") or "")
    ctx_info = get_reading(uid)
    if not ctx_info:
        await ctx.reply("请先发送热点/攻略命令获取列表，再回复序号")
        return
    items = ctx_info["items"]
    if not (1 <= idx <= len(items)):
        await ctx.reply(f"序号超出范围（1-{len(items)}）")
        return
    it = items[idx - 1]
    text = await _get_detail(it["href"])
    if not text:
        await ctx.reply(f"📄 {it['title']}\n⚠️ 正文获取失败，请稍后再试（回复 1 重试）")
        return
    total = max(1, (len(text) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), total)
    ctx_info["idx"] = idx
    ctx_info["page"] = page
    ctx_info["total"] = total
    set_reading(uid, ctx_info)
    start = (page - 1) * PAGE_SIZE
    lines = [f"📄 {it['title']}", "━━━━━━━━━━━━"]
    lines.append(text[start:start + PAGE_SIZE])
    lines.append("━━━━━━━━━━━━")
    lines.append(f"📄 {page}/{total} 页 · 📌 {' | '.join(x for x in (it.get('account'), it.get('time')) if x)}")
    lines.append("💡 /下一页 /上一页 /尾页 /第N页")
    await ctx.reply("\n".join(lines))


@on_command("/下一页")
@plugin_handler
async def handle_next(ctx: PluginContext):
    """翻到下一页"""
    uid = str(getattr(ctx, "sender_id", "") or "")
    c = get_reading(uid)
    if not c or not c.get("idx"):
        await ctx.reply("请先回复序号开始阅读")
        return
    await _show_detail(ctx, c["idx"], c["page"] + 1)


@on_command("/上一页")
@plugin_handler
async def handle_prev(ctx: PluginContext):
    """翻到上一页"""
    uid = str(getattr(ctx, "sender_id", "") or "")
    c = get_reading(uid)
    if not c or not c.get("idx"):
        await ctx.reply("请先回复序号开始阅读")
        return
    await _show_detail(ctx, c["idx"], c["page"] - 1)


@on_command("/尾页")
@plugin_handler
async def handle_last(ctx: PluginContext):
    """翻到最后一页"""
    uid = str(getattr(ctx, "sender_id", "") or "")
    c = get_reading(uid)
    if not c or not c.get("idx"):
        await ctx.reply("请先回复序号开始阅读")
        return
    await _show_detail(ctx, c["idx"], c["total"])


@on_command("/第N页", "/第n页", "/跳页", "/转页")
@plugin_handler
async def handle_jump(ctx: PluginContext):
    """跳转到指定页，如 /第3页"""
    rest = (ctx.raw_text or "").strip()
    m = re.search(r"(\d+)", rest)
    if not m:
        await ctx.reply("用法：/第N页，如 /第3页")
        return
    uid = str(getattr(ctx, "sender_id", "") or "")
    c = get_reading(uid)
    if not c or not c.get("idx"):
        await ctx.reply("请先回复序号开始阅读")
        return
    await _show_detail(ctx, c["idx"], int(m.group(1)))


async def _handle_index_cmd(ctx: PluginContext, topic: str, extra: str = ""):
    """处理列表命令：无参数显示列表；有数字直接看对应正文"""
    rest = (ctx.raw_text or "").strip()
    parts = rest.split(None, 1)
    if len(parts) > 1 and parts[1].strip().isdigit():
        uid = str(getattr(ctx, "sender_id", "") or "")
        # 先确保有列表上下文
        title, query = TOPICS[topic]
        if extra:
            query += " " + extra
        items = ((get_reading(uid) or {}).get("items") if (get_reading(uid) or {}).get("topic") == topic and (get_reading(uid) or {}).get("extra") == extra else None)
        if not items:
            items = await _get_list(query)
            if not items:
                await ctx.reply("😢 暂时没有获取到内容，请稍后再试")
                return
            set_reading(uid, {"topic": topic, "extra": extra, "items": items, "page": 1})
        await _show_detail(ctx, int(parts[1].strip()), 1)
        return
    await _show_list(ctx, topic, extra)


@on_command("/逃跑热点", "/逃跑吧少年热点", "/逃跑资讯")
@plugin_handler
async def handle_tpbsn(ctx: PluginContext):
    """查看逃跑吧少年热点"""
    await ctx.reply("🔥 正在获取逃跑吧少年热点...")
    try:
        await _handle_index_cmd(ctx, "hot")
    except Exception as e:
        logger.error(f"逃跑热点失败: {e}")
        await ctx.reply("❌ 获取失败，请稍后再试")


@on_command("/逃跑爆料", "/逃跑吧少年爆料", "/逃跑更新")
@plugin_handler
async def handle_tpbsn_leak(ctx: PluginContext):
    """查看逃跑吧少年爆料"""
    await ctx.reply("⚡ 正在获取逃跑吧少年爆料...")
    try:
        await _handle_index_cmd(ctx, "leak")
    except Exception as e:
        logger.error(f"逃跑爆料失败: {e}")
        await ctx.reply("❌ 获取失败，请稍后再试")


@on_command("/逃跑攻略", "/逃跑吧少年攻略")
@plugin_handler
async def handle_tpbsn_guide(ctx: PluginContext):
    """查看逃跑吧少年攻略"""
    rest = (ctx.raw_text or "").strip()
    parts = rest.split(None, 1)
    hero = parts[1].strip() if len(parts) > 1 and not parts[1].strip().isdigit() else ""
    await ctx.reply("📖 正在获取逃跑吧少年攻略...")
    try:
        await _handle_index_cmd(ctx, "guide", hero)
    except Exception as e:
        logger.error(f"逃跑攻略失败: {e}")
        await ctx.reply("❌ 获取失败，请稍后再试")


@on_command("/逃跑兑换码", "/逃跑码", "/逃跑吧少年兑换码", "/兑换码")
@plugin_handler
async def handle_tpbsn_code(ctx: PluginContext):
    """查看逃跑吧少年最新通用兑换码"""
    await ctx.reply("🎁 正在获取逃跑吧少年兑换码...")
    try:
        await _handle_index_cmd(ctx, "code")
    except Exception as e:
        logger.error(f"逃跑兑换码失败: {e}")
        await ctx.reply("❌ 获取失败，请稍后再试")
