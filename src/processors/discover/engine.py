"""发现引擎调度层（MediaCrawler 重写版）

统一封装多个底层引擎的调用，输出 CandidateRaw 列表（未筛选、未打分）。

引擎清单：
  小红书：
    - MediaCrawler PLATFORM=xhs CRAWLER_TYPE=search/creator → jsonl 落地
  抖音：
    - MediaCrawler PLATFORM=dy CRAWLER_TYPE=search → jsonl 落地
    - douyin-downloader.get_user_post / get_user_like / get_user_collect_mix（账号订阅）
    - dy-cli trending（热搜词）

底层逻辑：W1 路径——MediaCrawler 用独立 Playwright Chromium，cookies 不被用户浏览器轮换。
重写于 2026-05-11（xhs-cli subprocess → MediaCrawler jsonl 模式）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.utils.logger import logger

# ────────────────────────────────────────────────
# 路径常量
# ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[3]
MEDIACRAWLER_DIR = PROJECT_ROOT / "external" / "MediaCrawler"
MEDIACRAWLER_PY = Path(os.environ.get(
    "MEDIACRAWLER_PY",
    str(Path.home() / ".venvs" / "media-crawler" / "bin" / "python"),
))
MEDIACRAWLER_DATA = MEDIACRAWLER_DIR / "data"

DY_CLI = os.environ.get(
    "DY_CLI",
    str(Path.home() / ".venvs" / "dy-cli" / "bin" / "dy"),
)

# MediaCrawler 串行锁
_MC_LOCK = threading.Lock()

# Source 类型枚举（写入候选清单"触发条件"字段）
SOURCE_SEARCH = "search"
SOURCE_HOT = "hot"
SOURCE_USER_POSTS = "user_posts"
SOURCE_USER_LIKES = "user_likes"
SOURCE_USER_COLLECTS = "user_collects"
SOURCE_TOPIC = "topic"
SOURCE_TRENDING_WORD = "trending_word"


@dataclass
class CandidateRaw:
    """统一原始候选 schema。去重 key: (platform, note_id)。"""
    platform: str
    note_id: str
    title: str = ""
    author: str = ""
    author_id: str = ""
    url: str = ""
    description: str = ""
    like_count: int = 0
    collect_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    published_at: Optional[datetime] = None
    source: str = ""
    source_meta: dict = field(default_factory=dict)


def _parse_count(s) -> int:
    if s is None or s == "":
        return 0
    s = str(s).strip()
    try:
        if "亿" in s:
            return int(float(s.replace("亿", "")) * 100_000_000)
        if "万" in s:
            return int(float(s.replace("万", "")) * 10_000)
        return int(s)
    except (ValueError, TypeError):
        return 0


# ────────────────────────────────────────────────
# MediaCrawler 调用封装
# ────────────────────────────────────────────────

def _patch_mediacrawler_config(updates: dict[str, str]) -> dict[str, str]:
    """运行时修改 base_config.py 的 KEY = "VALUE" 赋值。返回旧值。"""
    cfg = MEDIACRAWLER_DIR / "config" / "base_config.py"
    src = cfg.read_text()
    old = {}
    for key, new_val in updates.items():
        pattern = re.compile(rf'^({re.escape(key)}\s*=\s*)(.+?)(\s*(?:#.*)?)$', re.MULTILINE)
        m = pattern.search(src)
        if not m:
            continue
        old[key] = m.group(2)
        if new_val in ("True", "False"):
            replacement = rf'\g<1>{new_val}\g<3>'
        else:
            replacement = rf'\g<1>"{new_val}"\g<3>'
        src = pattern.sub(replacement, src, count=1)
    cfg.write_text(src)
    return old


def _restore_mediacrawler_config(old: dict[str, str]):
    cfg = MEDIACRAWLER_DIR / "config" / "base_config.py"
    src = cfg.read_text()
    for key, old_val in old.items():
        pattern = re.compile(rf'^({re.escape(key)}\s*=\s*)(.+?)(\s*(?:#.*)?)$', re.MULTILINE)
        src = pattern.sub(rf'\g<1>{old_val}\g<3>', src, count=1)
    cfg.write_text(src)


def _today_jsonl(platform: str, kind: str) -> Optional[Path]:
    """找今天的 jsonl 文件。platform 'xhs'/'dy' → 目录 'xhs'/'douyin'"""
    dt = datetime.now().strftime("%Y-%m-%d")
    actual = "douyin" if platform == "dy" else platform
    p = MEDIACRAWLER_DATA / actual / "jsonl" / f"{kind}_{dt}.jsonl"
    return p if p.exists() else None


def _truncate_jsonl(platform: str, kind: str):
    """跑前清空今日 jsonl 避免读到残留。"""
    dt = datetime.now().strftime("%Y-%m-%d")
    actual = "douyin" if platform == "dy" else platform
    p = MEDIACRAWLER_DATA / actual / "jsonl" / f"{kind}_{dt}.jsonl"
    if p.exists():
        p.unlink()


def run_mediacrawler(platform: str, crawler_type: str, keywords: list[str] = None,
                     creator_id: str = None, timeout: int = 600) -> bool:
    """启动 MediaCrawler 子进程跑一次抓取。"""
    with _MC_LOCK:
        updates = {
            "PLATFORM": platform,
            "CRAWLER_TYPE": crawler_type,
            "LOGIN_TYPE": "cookie",
            "ENABLE_CDP_MODE": "False",
            "HEADLESS": "True",
            "SAVE_LOGIN_STATE": "True",
            "ENABLE_GET_COMMENTS": "False",
            "ENABLE_GET_WORDCLOUD": "False",
        }
        if keywords:
            updates["KEYWORDS"] = ",".join(keywords)
        if creator_id:
            if platform == "xhs":
                updates["XHS_CREATOR_ID_LIST"] = creator_id
            elif platform == "dy":
                updates["DY_CREATOR_ID_LIST"] = creator_id

        kind = "search_contents" if crawler_type == "search" else "creator_contents"
        _truncate_jsonl(platform, kind)

        old = _patch_mediacrawler_config(updates)
        try:
            logger.info(f"[MediaCrawler] platform={platform} type={crawler_type} kw={keywords or creator_id}")
            result = subprocess.run(
                [str(MEDIACRAWLER_PY), "main.py"],
                cwd=str(MEDIACRAWLER_DIR),
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            if result.returncode != 0:
                logger.warning(f"[MediaCrawler] rc={result.returncode} stderr: {result.stderr[-300:]}")
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning(f"[MediaCrawler] 超时 {timeout}s")
            return False
        except Exception as e:
            logger.error(f"[MediaCrawler] 异常: {e}")
            return False
        finally:
            _restore_mediacrawler_config(old)


def _read_jsonl(path: Path) -> list[dict]:
    if not path or not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


# ────────────────────────────────────────────────
# Cookies 失效告警（去重：每平台 6 小时只发一次）
# ────────────────────────────────────────────────
_ALERT_DEDUPE_DIR = PROJECT_ROOT / "data" / "discover"
_ALERT_DEDUPE_SEC = 6 * 3600  # 6 小时去重窗口


def _alert_cookies_failure(platform: str, context: str = ""):
    """MediaCrawler 跑出 0 条 → 推断 cookies 失效，飞书告警 + 落文件 marker。"""
    import time as _t
    try:
        _ALERT_DEDUPE_DIR.mkdir(parents=True, exist_ok=True)
        marker = _ALERT_DEDUPE_DIR / f".cookies_fail_{platform}"
        now = int(_t.time())
        if marker.exists():
            try:
                last = int(marker.read_text().strip() or "0")
                if now - last < _ALERT_DEDUPE_SEC:
                    return  # 去重窗口内，不重复告警
            except (ValueError, OSError):
                pass

        try:
            marker.write_text(str(now))
        except OSError:
            pass

        try:
            import yaml as _yaml
            cfg_path = PROJECT_ROOT / "config" / "discover.yaml"
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = _yaml.safe_load(f) or {}
            chat_id = ((cfg.get("triggers") or {}).get("cron") or {}).get("notify_chat_id", "")
            if chat_id:
                from src.processors.discover.cron import notify_feishu
                from src.handlers.message import build_xhs_alert_card
                helper = "bash scripts/discover_login_xhs.sh" if platform == "xhs" else ""
                reason = "cookies 失效（被服务端轮换）或账号被风控（-104）"
                cand_url = (
                    f"https://rwnb5yzunf4.feishu.cn/base/"
                    f"{os.getenv('FEISHU_CANDIDATE_APP_TOKEN', '')}"
                    f"?table={os.getenv('FEISHU_CANDIDATE_TABLE_ID', 'tblRqFfAdrCJVWAF')}"
                )
                card = build_xhs_alert_card(
                    platform=platform,
                    context=context or "未知",
                    reason=reason,
                    action_cmd=helper,
                    candidate_table_url=cand_url,
                )
                notify_feishu(chat_id, card=card)
                logger.warning(f"[engine.cookies_alert] {platform} 已飞书告警(卡片)")
        except Exception as e:
            logger.error(f"[engine.cookies_alert] 飞书通知失败: {e}")
    except Exception as e:
        logger.error(f"[engine.cookies_alert] 告警自身异常: {e}")


# ────────────────────────────────────────────────
# 小红书：MediaCrawler jsonl → CandidateRaw
# ────────────────────────────────────────────────

def _parse_xhs_note(raw: dict, source: str, source_meta: dict) -> Optional[CandidateRaw]:
    try:
        note_id = raw.get("note_id") or raw.get("id")
        if not note_id:
            return None
        # 关键修复：URL 必须带 xsec_token，否则 XHS-Downloader 调 xhs-api 必然返回
        # "获取小红书作品数据失败"（README 明确要求 xsec_token 参数）
        xsec_token = raw.get("xsec_token", "")
        if xsec_token:
            url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}"
        else:
            url = f"https://www.xiaohongshu.com/explore/{note_id}"
        return CandidateRaw(
            platform="xiaohongshu",
            note_id=note_id,
            title=(raw.get("title") or raw.get("display_title") or "").strip(),
            author=raw.get("nickname", ""),
            author_id=raw.get("user_id", ""),
            url=url,
            description=raw.get("desc", ""),
            like_count=_parse_count(raw.get("liked_count")),
            collect_count=_parse_count(raw.get("collected_count")),
            comment_count=_parse_count(raw.get("comment_count")),
            share_count=_parse_count(raw.get("shared_count")),
            source=source,
            source_meta={**(source_meta or {}), "xsec_token": xsec_token},
        )
    except (ValueError, TypeError) as e:
        logger.debug(f"xhs 解析失败: {e}")
        return None


def _xhs_provider() -> str:
    """读 env XHS_PROVIDER 决定走 tikhub 还是 mediacrawler。默认 tikhub。"""
    return (os.getenv("XHS_PROVIDER") or "tikhub").strip().lower()


def fetch_xhs_search(keywords: list[str], limit: int = 20) -> list[CandidateRaw]:
    """小红书 search：根据 XHS_PROVIDER 路由
       tikhub      → tikhub_xhs.fetch_search_notes（推荐，免登录）
       mediacrawler → MediaCrawler 子进程（旧路径回滚兜底）
    """
    if not keywords:
        return []
    provider = _xhs_provider()
    if provider == "tikhub":
        from src.processors.discover.tikhub_xhs import fetch_search_notes as _tikhub_search
        out = []
        for kw in keywords:
            try:
                out.extend(_tikhub_search(kw, page=1)[:limit])
            except Exception as e:
                logger.error(f"[xhs.search/tikhub] keyword={kw!r} 失败: {e}")
        logger.info(f"[xhs.search/tikhub] keywords={keywords} → {len(out)} 条")
        if not out:
            _alert_cookies_failure("xhs", f"tikhub search keywords={','.join(keywords)} 返回 0 条")
        return out

    # ── 旧路径：MediaCrawler subprocess（回滚兜底）──
    logger.info(f"[xhs.search/mediacrawler] keywords={keywords}")
    ok = run_mediacrawler("xhs", "search", keywords=keywords)
    if not ok:
        _alert_cookies_failure("xhs", f"search keywords={','.join(keywords)} 子进程失败")
        return []
    jsonl = _today_jsonl("xhs", "search_contents")
    items = _read_jsonl(jsonl)
    out = []
    for raw in items[:limit * max(len(keywords), 1)]:
        kw = raw.get("source_keyword", keywords[0])
        c = _parse_xhs_note(raw, SOURCE_SEARCH, {"keyword": kw})
        if c:
            out.append(c)
    logger.info(f"[xhs.search/mediacrawler] → {len(out)} 条")
    if not out:
        _alert_cookies_failure("xhs", f"search keywords={','.join(keywords)} 返回 0 条")
    return out


def fetch_xhs_user_posts(user_id: str, limit: int = 20, nickname: str = "") -> list[CandidateRaw]:
    """小红书账号作品：根据 XHS_PROVIDER 路由。"""
    provider = _xhs_provider()
    if provider == "tikhub":
        from src.processors.discover.tikhub_xhs import fetch_user_posted_notes as _tikhub_user
        try:
            out = _tikhub_user(user_id, nickname=nickname)[:limit]
            logger.info(f"[xhs.user_posts/tikhub] {nickname or user_id} → {len(out)} 条")
            return out
        except Exception as e:
            logger.error(f"[xhs.user_posts/tikhub] {nickname or user_id} 失败: {e}")
            return []

    # ── 旧路径：MediaCrawler creator ──
    logger.info(f"[xhs.user_posts/mediacrawler] user={nickname or user_id}")
    ok = run_mediacrawler("xhs", "creator", creator_id=user_id)
    if not ok:
        _alert_cookies_failure("xhs", f"creator {nickname or user_id} 子进程失败")
        return []
    jsonl = _today_jsonl("xhs", "creator_contents")
    items = _read_jsonl(jsonl)
    out = []
    for raw in items[:limit]:
        c = _parse_xhs_note(raw, SOURCE_USER_POSTS, {"nickname": nickname, "user_id": user_id})
        if c:
            out.append(c)
    logger.info(f"[xhs.user_posts/mediacrawler] {nickname or user_id} → {len(out)} 条")
    if not out:
        _alert_cookies_failure("xhs", f"creator {nickname or user_id} 返回 0 条")
    return out


def fetch_xhs_trending(top_n: int = 20) -> list[CandidateRaw]:
    """小红书热搜词榜（类比抖音 trending_words，仅展示不入候选清单）。

    走 TikHub /web_v2/fetch_hot_list；mediacrawler 路径无对应接口，会返空。
    """
    provider = _xhs_provider()
    if provider == "tikhub":
        from src.processors.discover.tikhub_xhs import fetch_hot_list as _tikhub_hot
        try:
            out = _tikhub_hot(top_n=top_n)
            logger.info(f"[xhs.trending/tikhub] → {len(out)} 条")
            return out
        except Exception as e:
            logger.error(f"[xhs.trending/tikhub] 失败: {e}")
            return []
    logger.warning("[xhs.trending] mediacrawler provider 不支持热榜，跳过")
    return []


# ────────────────────────────────────────────────
# 抖音：MediaCrawler jsonl → CandidateRaw
# ────────────────────────────────────────────────

def _parse_dy_aweme_from_jsonl(raw: dict, source: str, source_meta: dict) -> Optional[CandidateRaw]:
    try:
        aweme_id = raw.get("aweme_id") or raw.get("id")
        if not aweme_id:
            return None
        return CandidateRaw(
            platform="douyin",
            note_id=str(aweme_id),
            title=(raw.get("title") or raw.get("desc") or "").strip(),
            author=raw.get("nickname", ""),
            author_id=raw.get("sec_uid") or str(raw.get("user_id", "")),
            url=raw.get("aweme_url") or f"https://www.douyin.com/video/{aweme_id}",
            description=raw.get("desc", ""),
            like_count=_parse_count(raw.get("liked_count")),
            collect_count=_parse_count(raw.get("collected_count")),
            comment_count=_parse_count(raw.get("comment_count")),
            share_count=_parse_count(raw.get("share_count")),
            published_at=datetime.fromtimestamp(raw["create_time"]) if raw.get("create_time") else None,
            source=source,
            source_meta=source_meta,
        )
    except (ValueError, TypeError, KeyError) as e:
        logger.debug(f"douyin 解析失败: {e}")
        return None


def fetch_dy_search(keywords: list[str], limit: int = 20) -> list[CandidateRaw]:
    """抖音 search：MediaCrawler search 模式。"""
    if not keywords:
        return []
    logger.info(f"[dy.search] keywords={keywords}")
    ok = run_mediacrawler("dy", "search", keywords=keywords)
    if not ok:
        _alert_cookies_failure("dy", f"search keywords={','.join(keywords)} 子进程失败")
        return []
    jsonl = _today_jsonl("dy", "search_contents")
    items = _read_jsonl(jsonl)
    # 客户端 30 天时间过滤（抖音 API 原生只有 0/1/7/180 档，最接近的是 180=半年）
    # dy_config.py PUBLISH_TIME_TYPE=180 + 此处 30 天 cutoff = 实现"近 30 天"
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=30)
    out = []
    stale = 0
    for raw in items[:limit * max(len(keywords), 1)]:
        kw = raw.get("source_keyword", keywords[0])
        c = _parse_dy_aweme_from_jsonl(raw, SOURCE_SEARCH, {"keyword": kw})
        if not c:
            continue
        if c.published_at and c.published_at < cutoff:
            stale += 1
            continue
        out.append(c)
    logger.info(f"[dy.search] → {len(out)} 条（30 天内）；过期 drop {stale} 条")
    if not out:
        _alert_cookies_failure("dy", f"search keywords={','.join(keywords)} 返回 0 条")
    return out


# ────────────────────────────────────────────────
# 抖音账号订阅：douyin-downloader Python API
# ────────────────────────────────────────────────

def _load_douyin_cookies() -> dict:
    from src.processors.douyin_downloader import _load_cookies
    return _load_cookies()


def _parse_dy_aweme_from_api(aweme: dict, source: str, source_meta: dict) -> Optional[CandidateRaw]:
    try:
        aweme_id = aweme.get("aweme_id") or aweme.get("id")
        if not aweme_id:
            return None
        stats = aweme.get("statistics") or {}
        author = aweme.get("author") or {}
        return CandidateRaw(
            platform="douyin",
            note_id=str(aweme_id),
            title=(aweme.get("desc") or "").strip(),
            author=author.get("nickname", ""),
            author_id=author.get("sec_uid", "") or str(author.get("uid", "")),
            url=f"https://www.douyin.com/video/{aweme_id}",
            description=aweme.get("desc", ""),
            like_count=int(stats.get("digg_count") or 0),
            collect_count=int(stats.get("collect_count") or 0),
            comment_count=int(stats.get("comment_count") or 0),
            share_count=int(stats.get("share_count") or 0),
            published_at=datetime.fromtimestamp(aweme["create_time"]) if aweme.get("create_time") else None,
            source=source,
            source_meta=source_meta,
        )
    except (ValueError, TypeError, KeyError):
        return None


async def _fetch_dy_user_async(sec_uid: str, method: str, limit: int) -> list[dict]:
    try:
        from core.api_client import DouyinAPIClient
    except ImportError:
        return []
    cookies = _load_douyin_cookies()
    client = DouyinAPIClient(cookies)
    try:
        func = getattr(client, method, None)
        if not func:
            return []
        result = await func(sec_uid=sec_uid, max_cursor=0)
        if isinstance(result, dict):
            return result.get("aweme_list", [])[:limit]
        if isinstance(result, list):
            return result[:limit]
        return []
    except Exception as e:
        logger.error(f"调 {method} 失败: {e}")
        return []
    finally:
        try:
            await client.close()
        except Exception:
            pass


def fetch_dy_user_posts(sec_uid: str, limit: int = 20, nickname: str = "") -> list[CandidateRaw]:
    aweme_list = asyncio.run(_fetch_dy_user_async(sec_uid, "get_user_post", limit))
    out = []
    for aweme in aweme_list:
        c = _parse_dy_aweme_from_api(aweme, SOURCE_USER_POSTS, {"nickname": nickname, "sec_uid": sec_uid})
        if c:
            out.append(c)
    logger.info(f"[dy.user_posts] {nickname or sec_uid[:20]} → {len(out)} 条")
    return out


def fetch_dy_user_likes(sec_uid: str, limit: int = 20, nickname: str = "") -> list[CandidateRaw]:
    aweme_list = asyncio.run(_fetch_dy_user_async(sec_uid, "get_user_like", limit))
    out = []
    for aweme in aweme_list:
        c = _parse_dy_aweme_from_api(aweme, SOURCE_USER_LIKES, {"nickname": nickname, "sec_uid": sec_uid})
        if c:
            out.append(c)
    logger.info(f"[dy.user_likes] {nickname or sec_uid[:20]} → {len(out)} 条")
    return out


def fetch_dy_user_collects(sec_uid: str, limit: int = 20, nickname: str = "") -> list[CandidateRaw]:
    aweme_list = asyncio.run(_fetch_dy_user_async(sec_uid, "get_user_collect_mix", limit))
    out = []
    for aweme in aweme_list:
        c = _parse_dy_aweme_from_api(aweme, SOURCE_USER_COLLECTS, {"nickname": nickname, "sec_uid": sec_uid})
        if c:
            out.append(c)
    logger.info(f"[dy.user_collects] {nickname or sec_uid[:20]} → {len(out)} 条")
    return out


# ────────────────────────────────────────────────
# 抖音热搜词
# ────────────────────────────────────────────────

def fetch_dy_trending_words(top_n: int = 30) -> list[CandidateRaw]:
    """抖音热搜词（话题词，不是视频）。"""
    logger.info(f"[dy.trending] top_n={top_n}")
    try:
        result = subprocess.run(
            [DY_CLI, "trending", "--count", str(top_n), "--json-output"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        text = result.stdout
        idx = text.find("{")
        if idx < 0:
            return []
        end = text.rfind("}")
        data = json.loads(text[idx:end + 1])
    except Exception as e:
        logger.warning(f"[dy.trending] 失败: {e}")
        return []
    items = data.get("data", []) or []
    out = []
    for it in items[:top_n]:
        word = it.get("word", "").strip()
        if not word:
            continue
        out.append(CandidateRaw(
            platform="douyin",
            note_id=f"trending_word:{word}",
            title=word,
            url="",
            description=f"抖音热搜词 · 热度 {it.get('hot_value', 0)}",
            like_count=0,  # 热搜词不是视频，不参与点赞排序；hot_value 保留在 source_meta
            source=SOURCE_TRENDING_WORD,
            source_meta={"hot_value": int(it.get("hot_value") or 0)},
        ))
    logger.info(f"[dy.trending] → {len(out)} 条热搜词")
    return out


# ────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────

def fetch_all(config: dict) -> list[CandidateRaw]:
    """根据 discover.yaml 配置一次性收集所有维度的候选。单个 source 失败不影响其他。"""
    candidates: list[CandidateRaw] = []

    keywords = config.get("keywords") or []
    if keywords:
        try:
            candidates.extend(fetch_xhs_search(keywords, limit=20))
        except Exception as e:
            logger.error(f"fetch_xhs_search 失败: {e}")
        try:
            candidates.extend(fetch_dy_search(keywords, limit=20))
        except Exception as e:
            logger.error(f"fetch_dy_search 失败: {e}")

    accounts = config.get("accounts") or {}
    for acc in (accounts.get("xiaohongshu") or []):
        if acc.get("user_id"):
            try:
                candidates.extend(fetch_xhs_user_posts(
                    acc["user_id"], limit=20, nickname=acc.get("nickname", "")
                ))
            except Exception as e:
                logger.error(f"fetch_xhs_user_posts 失败: {e}")

    for acc in (accounts.get("douyin") or []):
        if not acc.get("sec_uid"):
            continue
        scan = acc.get("scan") or ["posts", "likes"]
        nick = acc.get("nickname", "")
        sec_uid = acc["sec_uid"]
        try:
            if "posts" in scan:
                candidates.extend(fetch_dy_user_posts(sec_uid, limit=20, nickname=nick))
            if "likes" in scan:
                candidates.extend(fetch_dy_user_likes(sec_uid, limit=20, nickname=nick))
            if "collects" in scan:
                candidates.extend(fetch_dy_user_collects(sec_uid, limit=20, nickname=nick))
        except Exception as e:
            logger.error(f"fetch_dy_user_* 失败: {e}")

    trending_cfg = config.get("trending") or {}
    if trending_cfg.get("douyin", {}).get("enable"):
        try:
            top_n = trending_cfg["douyin"].get("top_n", 30)
            candidates.extend(fetch_dy_trending_words(top_n=top_n))
        except Exception as e:
            logger.error(f"fetch_dy_trending_words 失败: {e}")

    # 小红书热搜词（同抖音 trending_words 模式，仅展示卡片不入候选清单）
    if trending_cfg.get("xiaohongshu", {}).get("enable"):
        try:
            top_n = trending_cfg["xiaohongshu"].get("top_n", 20)
            candidates.extend(fetch_xhs_trending(top_n=top_n))
        except Exception as e:
            logger.error(f"fetch_xhs_trending 失败: {e}")

    logger.info(f"[engine.fetch_all] 总候选数: {len(candidates)}")
    return candidates


if __name__ == "__main__":
    import yaml
    cfg_path = PROJECT_ROOT / "config" / "discover.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    candidates = fetch_all(cfg)
    print(f"\n=== 总候选 {len(candidates)} 条 ===")
    for c in candidates[:10]:
        print(f"  [{c.platform}/{c.source}] {c.title[:40]} | 👍{c.like_count} ⭐{c.collect_count}")
