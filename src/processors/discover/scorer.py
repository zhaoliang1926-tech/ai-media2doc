"""打分器 + 阈值分流。

输入 CandidateRaw + hits + config，输出分数（0-100）+ classification。
打分维度 = 互动 40 + 关键词 30 + 账号 15 + 话题 10 + 新鲜度 5。

新增于 2026-05-11。
"""
from __future__ import annotations

import math
from datetime import datetime

from src.processors.discover.engine import CandidateRaw, SOURCE_TRENDING_WORD


CLASS_AUTO_REWRITE = "auto_rewrite"
CLASS_CANDIDATE = "candidate"
CLASS_DROP = "drop"


def score(meta: CandidateRaw, hits: dict, config: dict) -> int:
    weights = (config.get("scoring") or {}).get("weights") or {}
    w_interaction = int(weights.get("interaction", 40))
    w_keywords = int(weights.get("keywords", 30))
    w_account = int(weights.get("account", 15))
    w_topics = int(weights.get("topics", 10))
    w_freshness = int(weights.get("freshness", 5))

    s = 0
    interaction = (meta.like_count or 0) + (meta.collect_count or 0) * 2 + (meta.share_count or 0) * 3
    if interaction > 0:
        s += min(w_interaction, int(math.log10(interaction) * 8))

    kw_hits = len(hits.get("keywords") or [])
    if kw_hits:
        s += min(w_keywords, kw_hits * 10)

    if hits.get("account"):
        s += w_account

    topic_hits = len(hits.get("topics") or [])
    if topic_hits:
        s += min(w_topics, topic_hits * 5)

    if meta.published_at:
        try:
            age_hours = (datetime.now() - meta.published_at).total_seconds() / 3600.0
            if age_hours < 24:
                s += w_freshness
        except (TypeError, ValueError):
            pass

    return min(100, s)


def classify(meta: CandidateRaw, score_value: int, config: dict) -> str:
    """auto_rewrite / candidate / drop。

    热搜词总是 DROP——不入候选清单（因为不是视频，无 URL 可改写）。
    cron 会单独发飞书消息把热搜词作为情报参考。
    """
    if meta.source == SOURCE_TRENDING_WORD:
        return CLASS_DROP

    th = (config.get("thresholds") or {})
    p = meta.platform

    candidate_th = (th.get("candidate_only") or {}).get(p) or {}
    if candidate_th.get("like_count"):
        if (meta.like_count or 0) < candidate_th["like_count"]:
            return CLASS_DROP

    auto_th = (th.get("auto_rewrite") or {}).get(p) or {}
    if p == "xiaohongshu":
        like_th = auto_th.get("like_count")
        collect_th = auto_th.get("collect_count")
        if like_th and collect_th:
            if (meta.like_count or 0) >= like_th and (meta.collect_count or 0) >= collect_th:
                return CLASS_AUTO_REWRITE
    elif p == "douyin":
        like_th = auto_th.get("like_count")
        if like_th and (meta.like_count or 0) >= like_th:
            return CLASS_AUTO_REWRITE

    return CLASS_CANDIDATE
