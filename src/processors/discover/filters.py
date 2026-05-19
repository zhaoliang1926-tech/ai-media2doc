"""5 维筛选器 + 黑名单

输入 CandidateRaw + discover.yaml config，判断该候选是否命中筛选维度。
命中任一维度即通过；平台热门/热搜词直通（不要求命中维度）。
**先过黑名单**：命中 blacklist 直接 drop（不进 score/classify，不入候选清单）。

新增于 2026-05-11。黑名单于 2026-05-13 加入。
"""
from __future__ import annotations

from typing import Optional

from src.utils.logger import logger
from src.processors.discover.engine import CandidateRaw, SOURCE_HOT, SOURCE_TRENDING_WORD


def _hit_blacklist(meta: CandidateRaw, blacklist: dict) -> Optional[str]:
    """检查 meta 是否命中黑名单。命中返回理由字符串（用于 log），否则返回 None。"""
    if not blacklist:
        return None
    text = f"{meta.title or ''} {meta.description or ''}".lower()

    # 1. 关键词黑名单（命中标题/描述）
    for kw in (blacklist.get("keywords") or []):
        if kw and kw.lower() in text:
            return f"keyword:{kw}"

    # 2. 作者名黑名单
    author = (meta.author or "").strip()
    if author and author in (blacklist.get("authors") or []):
        return f"author:{author}"

    # 3. 作者 ID 黑名单（sec_uid / user_id）
    author_id = (meta.author_id or "").strip()
    if author_id and author_id in (blacklist.get("author_ids") or []):
        return f"author_id:{author_id}"

    return None


def filter_candidate(meta: CandidateRaw, config: dict) -> Optional[dict]:
    """5 维筛选。命中即返回 hits 字典；不通过返回 None。

    新增：先过 blacklist，命中即 drop。
    """
    # 黑名单优先（命中即 drop）
    bl_reason = _hit_blacklist(meta, config.get("blacklist") or {})
    if bl_reason:
        return None

    hits = {
        "keywords": [],
        "account": None,
        "topics": [],
        "is_bypass": False,
    }

    # 平台热门 / 热搜词直通
    if meta.source in (SOURCE_HOT, SOURCE_TRENDING_WORD):
        hits["is_bypass"] = True
        return hits

    text = f"{meta.title or ''}  {meta.description or ''}".lower()

    # 维度 1：关键词
    for kw in (config.get("keywords") or []):
        if kw and kw.lower() in text:
            hits["keywords"].append(kw)

    # 维度 2：账号
    accounts_cfg = config.get("accounts") or {}
    platform_key = "xiaohongshu" if meta.platform == "xiaohongshu" else "douyin"
    for acc in (accounts_cfg.get(platform_key) or []):
        target_id = acc.get("user_id") or acc.get("sec_uid")
        if target_id and meta.author_id == target_id:
            hits["account"] = acc.get("nickname", str(target_id)[:20])
            break

    # 维度 3：话题
    for topic in (config.get("topics") or []):
        topic_stripped = topic.lstrip("#")
        if topic in meta.description or f"#{topic_stripped}" in meta.description:
            hits["topics"].append(topic)

    if hits["keywords"] or hits["account"] or hits["topics"]:
        return hits
    return None


def filter_batch(candidates: list[CandidateRaw], config: dict) -> list[tuple[CandidateRaw, dict]]:
    """批量筛选。返回 [(meta, hits), ...]

    隐式统计 blacklisted 和 unmatched 两种 drop 原因（写入 logger，调用方可观测）。
    """
    blacklist = config.get("blacklist") or {}
    results = []
    blacklisted = 0
    unmatched = 0
    for c in candidates:
        # 单独检查黑名单（区分 drop 原因，便于运维观测）
        bl_reason = _hit_blacklist(c, blacklist)
        if bl_reason:
            blacklisted += 1
            logger.info(f"[filters.blacklist] drop {c.platform}/{c.note_id} ({bl_reason}): {(c.title or '')[:40]}")
            continue
        hits = filter_candidate(c, config)
        if hits is not None:
            results.append((c, hits))
        else:
            unmatched += 1
    total_drop = blacklisted + unmatched
    logger.info(
        f"[filters] 输入 {len(candidates)} → 通过 {len(results)} 条（"
        f"黑名单 drop {blacklisted}，未命中维度 drop {unmatched}）"
    )
    return results
