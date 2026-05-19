"""TikHub.io 小红书 SaaS 接入 - 替代 MediaCrawler 子进程的免登录方案。

接口列表（基于 2026-05-17 真实探针）：
  - app_v2/search_notes           关键词搜索笔记（20 条/页）
  - app_v2/get_user_posted_notes  用户发布的笔记列表
  - web_v2/fetch_hot_list         小红书热搜词（20-30 词，类比抖音 trending_words）

价格：$0.001/次（错误响应免费），按量计费
依赖：requests（已装），env TIKHUB_API_KEY
循环依赖处理：CandidateRaw / SOURCE_* 通过 lazy import 避免与 engine.py 循环

新增于 2026-05-17（XHS 切换 TikHub 通道，根除账号 -104 风控）。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

import requests

from src.utils.logger import logger


TIKHUB_BASE = "https://api.tikhub.io"
DEFAULT_TIMEOUT = 30
# 客户端 30 天时间过滤（同 engine.fetch_dy_search 抖音侧逻辑对齐）
# → 过滤老旧笔记，候选清单只保留近 30 天热度
FRESH_DAYS = 30


def _get_key() -> str:
    key = os.getenv("TIKHUB_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TIKHUB_API_KEY 未配置（请检查 .env）")
    return key


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_key()}",
        "accept": "application/json",
    }


def _request(path: str, params: dict, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """通用 GET 调用 + 错误处理。返回 dict（失败返 {}）"""
    url = f"{TIKHUB_BASE}{path}"
    try:
        r = requests.get(url, params=params, headers=_headers(), timeout=timeout)
        if r.status_code != 200:
            logger.warning(f"[tikhub] {path} HTTP {r.status_code}: {r.text[:200]}")
            return {}
        return r.json() or {}
    except Exception as e:
        logger.error(f"[tikhub] {path} 网络异常: {e}")
        return {}


def _parse_score_to_int(score_str) -> int:
    """『931.4万』→ 9314000，『1.2亿』→ 120000000"""
    if not score_str:
        return 0
    s = str(score_str).strip()
    try:
        if "亿" in s:
            return int(float(s.replace("亿", "")) * 100_000_000)
        if "万" in s or "w" in s.lower() or "W" in s:
            return int(float(s.replace("万", "").replace("w", "").replace("W", "")) * 10_000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _parse_note(note: dict, source: str, source_meta: dict):
    """TikHub 笔记 dict → CandidateRaw（lazy import）"""
    from src.processors.discover.engine import CandidateRaw
    note_id = note.get("id") or note.get("note_id")
    if not note_id:
        return None
    xsec_token = note.get("xsec_token", "") or ""
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}"
    user = note.get("user") or {}
    ts = note.get("timestamp") or note.get("create_time")
    published_at = None
    if ts:
        try:
            published_at = datetime.fromtimestamp(int(ts))
        except (ValueError, TypeError, OSError):
            published_at = None
    return CandidateRaw(
        platform="xiaohongshu",
        note_id=str(note_id),
        title=(note.get("title") or note.get("desc") or "")[:300].strip(),
        author=(user.get("nickname") or user.get("name") or user.get("red_id", "")),
        author_id=str(user.get("userid") or user.get("user_id", "")),
        url=url,
        description=(note.get("desc") or "")[:1000],
        like_count=int(note.get("liked_count") or 0),
        collect_count=int(note.get("collected_count") or 0),
        comment_count=int(note.get("comments_count") or 0),
        share_count=int(note.get("shared_count") or 0),
        published_at=published_at,
        source=source,
        source_meta={
            **(source_meta or {}),
            "xsec_token": xsec_token,
            "note_type": note.get("type", ""),
        },
    )


# ─────────────────────────────────────────────────────────
# 公开接口
# ─────────────────────────────────────────────────────────

def _filter_fresh(candidates: list, label: str) -> list:
    """客户端 30 天时间过滤：过滤掉 published_at 早于 cutoff 的老旧笔记。

    没有 published_at 的笔记（解析失败）也保留——避免误杀；让下游 scorer 处理。
    """
    cutoff = datetime.now() - timedelta(days=FRESH_DAYS)
    fresh = []
    stale = 0
    for c in candidates:
        if c.published_at and c.published_at < cutoff:
            stale += 1
            continue
        fresh.append(c)
    if stale > 0:
        logger.info(f"[tikhub.{label}] 30 天时间过滤：保留 {len(fresh)} 条，过期 drop {stale} 条")
    return fresh


def fetch_search_notes(keyword: str, page: int = 1,
                        sort_type: str = "general") -> list:
    """关键词搜索笔记。返回 list[CandidateRaw]（已过滤 30 天内）。

    sort_type: general（综合）/ time（最新）/ popularity_descending（热度降序）
    """
    from src.processors.discover.engine import SOURCE_SEARCH
    logger.info(f"[tikhub.search] keyword={keyword!r} page={page}")
    resp = _request("/api/v1/xiaohongshu/app_v2/search_notes", {
        "keyword": keyword,
        "page": page,
        "sort_type": sort_type,
    })
    items = (((resp.get("data") or {}).get("data") or {}).get("items")) or []
    out = []
    for item in items:
        note = item.get("note") or {}
        c = _parse_note(note, SOURCE_SEARCH, {"keyword": keyword})
        if c:
            out.append(c)
    out = _filter_fresh(out, f"search/{keyword}")
    logger.info(f"[tikhub.search] keyword={keyword!r} → {len(out)} 条（30 天内）")
    return out


def fetch_user_posted_notes(user_id: str, nickname: str = "") -> list:
    """用户发布的笔记列表。返回 list[CandidateRaw]（已过滤 30 天内）。"""
    from src.processors.discover.engine import SOURCE_USER_POSTS
    if not user_id:
        return []
    logger.info(f"[tikhub.user_posts] user={nickname or user_id}")
    resp = _request("/api/v1/xiaohongshu/app_v2/get_user_posted_notes", {
        "user_id": user_id,
    })
    notes = (((resp.get("data") or {}).get("data") or {}).get("notes")) or []
    out = []
    for note in notes:
        c = _parse_note(note, SOURCE_USER_POSTS, {
            "user_id": user_id,
            "nickname": nickname,
        })
        if c:
            out.append(c)
    out = _filter_fresh(out, f"user_posts/{nickname or user_id[:12]}")
    logger.info(f"[tikhub.user_posts] {nickname or user_id} → {len(out)} 条（30 天内）")
    return out


def fetch_hot_list(top_n: int = 20) -> list:
    """小红书热搜词榜（类比抖音 trending_words）。返回 list[CandidateRaw]。

    每条 CandidateRaw 的 source=trending_word，source_meta.hot_value=热度数值。
    """
    from src.processors.discover.engine import CandidateRaw, SOURCE_TRENDING_WORD
    logger.info(f"[tikhub.hot_list] top_n={top_n}")
    resp = _request("/api/v1/xiaohongshu/web_v2/fetch_hot_list", {})
    items = (((resp.get("data") or {}).get("data") or {}).get("items")) or []
    out = []
    for item in items[:top_n]:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        hot_value = _parse_score_to_int(item.get("score", ""))
        out.append(CandidateRaw(
            platform="xiaohongshu",
            note_id=str(item.get("id") or f"hot_{abs(hash(title))}"),
            title=title,
            author="",
            author_id="",
            url="",
            description="",
            like_count=0,
            collect_count=0,
            comment_count=0,
            share_count=0,
            source=SOURCE_TRENDING_WORD,
            source_meta={
                "hot_value": hot_value,
                "word_type": item.get("word_type", ""),
                "rank_change": item.get("rank_change", 0),
            },
        ))
    logger.info(f"[tikhub.hot_list] → {len(out)} 条热搜词")
    return out
