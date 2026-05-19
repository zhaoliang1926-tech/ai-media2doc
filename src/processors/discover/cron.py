"""discover 定时触发器（PM2 调度入口）。

PM2 配置：cron_restart "0 8 * * *"（每天 8:00 跑一次）

流程：load config → engine.fetch_all → filter_batch → score+classify → dedupe → insert + 通知

新增于 2026-05-11。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import json as _json
import yaml
import requests

from src.utils.logger import logger
from src.processors.discover import engine, filters, scorer, dedupe, candidates


CONFIG_PATH = PROJECT_ROOT / "config" / "discover.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def notify_feishu(chat_id: str, text: str = "", card: dict = None) -> bool:
    """发飞书消息。

    优先 card（飞书 interactive card），card=None 时降级 text。
    旧调用 notify_feishu(chat_id, text) 仍兼容。
    """
    if not chat_id or (not text and not card):
        return False
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": os.getenv("FEISHU_APP_ID"), "app_secret": os.getenv("FEISHU_APP_SECRET")},
            timeout=10,
        )
        token = r.json().get("tenant_access_token")
        if not token:
            return False
        if card is not None:
            msg_type = "interactive"
            content_str = _json.dumps(card, ensure_ascii=False)
        else:
            msg_type = "text"
            content_str = _json.dumps({"text": text}, ensure_ascii=False)
        r = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": msg_type, "content": content_str},
            timeout=10,
        )
        return r.json().get("code") == 0
    except Exception as e:
        logger.error(f"[cron.notify_feishu] {e}")
        return False


def send_card_and_return_id(chat_id: str, card: dict) -> str:
    """发交互卡片，返回 message_id（成功）或空串（失败）。

    用于需要后续 PATCH 卡片状态的场景（如「写作中」→「已生成」一卡两状态）。
    """
    if not chat_id or not card:
        return ""
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": os.getenv("FEISHU_APP_ID"), "app_secret": os.getenv("FEISHU_APP_SECRET")},
            timeout=10,
        )
        token = r.json().get("tenant_access_token")
        if not token:
            return ""
        r = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "interactive",
                  "content": _json.dumps(card, ensure_ascii=False)},
            timeout=10,
        )
        body = r.json()
        if body.get("code") != 0:
            logger.error(f"[cron.send_card_and_return_id] {body}")
            return ""
        return body.get("data", {}).get("message_id", "")
    except Exception as e:
        logger.error(f"[cron.send_card_and_return_id] {e}")
        return ""


# ─────────────────────────────────────────────────────────
# 热搜卡本地缓存（callback 时重建原卡用）
# 飞书 GET /im/v1/messages 返回的是渲染后的扁平结构（text 段集合），
# 不是原始 card JSON——所以无法 GET 后重建。改为发卡时自己缓存。
# ─────────────────────────────────────────────────────────
_TRENDING_CARD_CACHE_DIR = PROJECT_ROOT / "ops" / "cache" / "trending_cards"


def save_trending_card_cache(message_id: str, card: dict) -> None:
    """发热搜卡后立刻调，把原卡 JSON 落盘，供后续 callback 重建。"""
    if not message_id or not card:
        return
    try:
        _TRENDING_CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _TRENDING_CARD_CACHE_DIR / f"{message_id}.json"
        with open(p, "w", encoding="utf-8") as f:
            _json.dump(card, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[cron.save_trending_card_cache] {e}")


def load_trending_card_cache(message_id: str) -> dict:
    """callback 内取出已发的原卡，找不到返空 dict。"""
    if not message_id:
        return {}
    try:
        p = _TRENDING_CARD_CACHE_DIR / f"{message_id}.json"
        if not p.exists():
            return {}
        with open(p, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception as e:
        logger.warning(f"[cron.load_trending_card_cache] {e}")
        return {}


def cleanup_trending_card_cache(ttl_days: int = 7) -> int:
    """清理 ttl_days 之前的缓存文件（飞书消息 7 天内可改）。返回清理数。"""
    import time as _time
    if not _TRENDING_CARD_CACHE_DIR.exists():
        return 0
    cutoff = _time.time() - ttl_days * 86400
    cleaned = 0
    for p in _TRENDING_CARD_CACHE_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                cleaned += 1
        except Exception:
            pass
    return cleaned


def get_message_card(message_id: str) -> dict:
    """GET 已发消息的卡片 content，返回 dict（解析自 content 字符串）；失败返空 dict。

    用于在 card_action callback 内取出原热搜卡数据，重建后局部更新被点词条。
    """
    if not message_id:
        return {}
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": os.getenv("FEISHU_APP_ID"), "app_secret": os.getenv("FEISHU_APP_SECRET")},
            timeout=10,
        )
        token = r.json().get("tenant_access_token")
        if not token:
            return {}
        r = requests.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        body = r.json()
        if body.get("code") != 0:
            logger.error(f"[cron.get_message_card] message_id={message_id} resp={body}")
            return {}
        items = body.get("data", {}).get("items", []) or []
        if not items:
            return {}
        content_str = items[0].get("body", {}).get("content", "") or ""
        return _json.loads(content_str) if content_str else {}
    except Exception as e:
        logger.error(f"[cron.get_message_card] {e}")
        return {}


def patch_card_message(message_id: str, card: dict) -> bool:
    """PATCH 已发的交互卡片，替换为新内容。

    限制：飞书要求消息在 7 天内可改；只能 PATCH interactive 类型；同源 app 的消息。
    """
    if not message_id or not card:
        return False
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": os.getenv("FEISHU_APP_ID"), "app_secret": os.getenv("FEISHU_APP_SECRET")},
            timeout=10,
        )
        token = r.json().get("tenant_access_token")
        if not token:
            return False
        r = requests.patch(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"content": _json.dumps(card, ensure_ascii=False)},
            timeout=10,
        )
        body = r.json()
        if body.get("code") != 0:
            logger.error(f"[cron.patch_card_message] message_id={message_id} resp={body}")
            return False
        return True
    except Exception as e:
        logger.error(f"[cron.patch_card_message] {e}")
        return False


def run(trigger: str = "cron") -> dict:
    """主流程入口。trigger: cron / watcher / command"""
    config = load_config()
    logger.info(f"[discover.{trigger}] === 开始 ===")

    raw_candidates = engine.fetch_all(config)
    if not raw_candidates:
        logger.info(f"[discover.{trigger}] 无候选")
        return {"input": 0, "passed": 0, "candidate": 0, "auto_rewrite": 0, "duplicate": 0, "drop": 0}

    # 把热搜词单独抽出来发飞书情报消息（不入候选清单，因为不是视频）
    triggers_cfg_early = (config.get("triggers") or {}).get(trigger) or {}
    chat_id_early = triggers_cfg_early.get("notify_chat_id", "")
    trending_words = [c for c in raw_candidates if c.source == engine.SOURCE_TRENDING_WORD]
    if trending_words and chat_id_early:
        from src.handlers.message import build_trending_card
        from datetime import datetime as _dt
        # 业务相关检测（用 yaml.business_signal_keywords）
        biz_kws = config.get("business_signal_keywords") or []
        def _is_biz(title: str) -> bool:
            return any(kw and kw in title for kw in biz_kws)
        ts = _dt.now().strftime("%m-%d %H:%M")

        # 按平台拆分：抖音 + 小红书各发一张独立卡，标题/业务相关检测互不干扰
        # 缓存（ops/cache/trending_cards/{message_id}.json）也按平台各存一份
        # → main.on_card_action callback 时按原卡 message_id 重建对应平台的卡
        for plat_key, plat_label in [("douyin", "抖音"), ("xiaohongshu", "小红书")]:
            plat_words = [c for c in trending_words if c.platform == plat_key]
            if not plat_words:
                continue
            words_tuples = [
                (t.title, (t.source_meta or {}).get("hot_value", 0), _is_biz(t.title))
                for t in plat_words[:30]
            ]
            card = build_trending_card(plat_label, words_tuples, timestamp=ts)
            if not card:
                continue
            mid = send_card_and_return_id(chat_id_early, card)
            if mid:
                save_trending_card_cache(mid, card)
            biz_count = sum(1 for _, _, b in words_tuples if b)
            logger.info(
                f"[discover.{trigger}] 推送 {plat_label}热搜卡 {len(plat_words)} 条"
                f"（业务相关 {biz_count} 条，cache_mid={mid or 'N/A'}）"
            )

    passed = filters.filter_batch(raw_candidates, config)

    limits = config.get("limits") or {}
    max_candidates = int(limits.get("daily_max_candidates", 100))
    max_auto = int(limits.get("daily_max_auto_rewrite", 10))
    ttl_days = int(limits.get("dedupe_window_days", 30))

    already_today = candidates.daily_count(trigger=trigger)

    stats = {
        "input": len(raw_candidates),
        "passed": len(passed),
        "candidate": 0,
        "auto_rewrite": 0,
        "duplicate": 0,
        "drop": 0,
        "limit_exceeded": 0,
    }
    auto_rewrite_urls = []

    for meta, hits in passed:
        if dedupe.is_duplicate(meta.platform, meta.note_id, ttl_days=ttl_days):
            stats["duplicate"] += 1
            continue

        score_value = scorer.score(meta, hits, config)
        classification = scorer.classify(meta, score_value, config)

        if classification == scorer.CLASS_DROP:
            stats["drop"] += 1
            dedupe.mark_seen(meta.platform, meta.note_id, status="drop")
            continue

        if already_today + stats["candidate"] + stats["auto_rewrite"] >= max_candidates:
            stats["limit_exceeded"] += 1
            continue

        effective = classification
        if classification == scorer.CLASS_AUTO_REWRITE and stats["auto_rewrite"] >= max_auto:
            effective = scorer.CLASS_CANDIDATE

        record_id = candidates.insert_candidate(meta, hits, score_value, trigger,
                                                classification=effective)
        if record_id:
            if effective == scorer.CLASS_AUTO_REWRITE:
                stats["auto_rewrite"] += 1
                if meta.url:
                    auto_rewrite_urls.append((meta.url, record_id))
                dedupe.mark_seen(meta.platform, meta.note_id, status="auto_rewrite")
            else:
                stats["candidate"] += 1
                dedupe.mark_seen(meta.platform, meta.note_id, status="candidate")

    triggers_cfg = (config.get("triggers") or {}).get(trigger) or {}
    chat_id = triggers_cfg.get("notify_chat_id", "")
    if chat_id:
        from src.handlers.message import build_cron_done_card
        # 候选清单飞书表 URL（候选表 app_token + table_id）
        cand_url = (
            f"https://rwnb5yzunf4.feishu.cn/base/"
            f"{os.getenv('FEISHU_CANDIDATE_APP_TOKEN', '')}"
            f"?table={os.getenv('FEISHU_CANDIDATE_TABLE_ID', 'tblRqFfAdrCJVWAF')}"
        )
        card = build_cron_done_card(trigger, stats, candidate_table_url=cand_url)
        notify_feishu(chat_id, card=card)

    logger.info(f"[discover.{trigger}] === 完成 === {stats}")

    if auto_rewrite_urls:
        # 自动改写候选已入库 + 状态标 auto_rewrite，后续由独立 worker 或下次 cron 触发 main.py.process()
        # 当前阶段简化为标记（Step 10 端到端集成时接 process）
        logger.info(f"[discover.{trigger}] {len(auto_rewrite_urls)} 条 auto_rewrite 候选已入库")

    return stats


if __name__ == "__main__":
    trigger = sys.argv[1] if len(sys.argv) > 1 else "cron"
    result = run(trigger=trigger)
    print(f"\n=== {trigger} 完成 ===")
    print(result)
