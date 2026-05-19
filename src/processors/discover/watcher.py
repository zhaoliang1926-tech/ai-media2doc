"""discover 实时监控 watcher（PM2 常驻进程）。

按 config.triggers.watcher.interval_minutes 轮询订阅账号增量。
不扫关键词/话题/热门（这些由 cron 每天 8:00 跑一次）。

PM2: pm2 start src/processors/discover/watcher.py --name discover-watcher

新增于 2026-05-11。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import yaml

from src.utils.logger import logger
from src.processors.discover import engine, filters, scorer, dedupe, candidates
from src.processors.discover.cron import notify_feishu


CONFIG_PATH = PROJECT_ROOT / "config" / "discover.yaml"


def _load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _process(raw_list, config, stats, ttl_days, trigger):
    if not raw_list:
        return
    stats["input"] += len(raw_list)
    passed = filters.filter_batch(raw_list, config)
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
        record_id = candidates.insert_candidate(meta, hits, score_value, trigger,
                                                classification=classification)
        if record_id:
            if classification == scorer.CLASS_AUTO_REWRITE:
                stats["auto_rewrite"] += 1
                dedupe.mark_seen(meta.platform, meta.note_id, status="auto_rewrite")
            else:
                stats["candidate"] += 1
                dedupe.mark_seen(meta.platform, meta.note_id, status="candidate")


def _scan_accounts_once(config: dict) -> dict:
    accounts_cfg = config.get("accounts") or {}
    limits = config.get("limits") or {}
    ttl_days = int(limits.get("dedupe_window_days", 30))
    stats = {"input": 0, "candidate": 0, "auto_rewrite": 0, "duplicate": 0, "drop": 0}

    for acc in (accounts_cfg.get("xiaohongshu") or []):
        if not acc.get("user_id"):
            continue
        try:
            results = engine.fetch_xhs_user_posts(
                acc["user_id"], limit=10, nickname=acc.get("nickname", "")
            )
            _process(results, config, stats, ttl_days, trigger="watcher")
        except Exception as e:
            logger.error(f"[watcher.xhs] {acc.get('nickname')} 失败: {e}")

    for acc in (accounts_cfg.get("douyin") or []):
        if not acc.get("sec_uid"):
            continue
        scan = acc.get("scan") or ["posts"]
        nick = acc.get("nickname", "")
        sec_uid = acc["sec_uid"]
        try:
            if "posts" in scan:
                _process(engine.fetch_dy_user_posts(sec_uid, limit=5, nickname=nick),
                         config, stats, ttl_days, trigger="watcher")
            if "likes" in scan:
                _process(engine.fetch_dy_user_likes(sec_uid, limit=5, nickname=nick),
                         config, stats, ttl_days, trigger="watcher")
        except Exception as e:
            logger.error(f"[watcher.dy] {nick} 失败: {e}")

    return stats


def main_loop():
    logger.info("[watcher] === 启动 ===")
    while True:
        try:
            config = _load_config()
            watcher_cfg = (config.get("triggers") or {}).get("watcher") or {}
            if not watcher_cfg.get("enable"):
                logger.info("[watcher] disabled，5 分钟后重读 config")
                time.sleep(300)
                continue

            interval_min = int(watcher_cfg.get("interval_minutes", 60))
            stats = _scan_accounts_once(config)
            new_count = stats["candidate"] + stats["auto_rewrite"]
            logger.info(f"[watcher] 本轮 stats: {stats}")

            if new_count > 0:
                chat_id = watcher_cfg.get("notify_chat_id") or (
                    (config.get("triggers") or {}).get("cron") or {}
                ).get("notify_chat_id", "")
                if chat_id:
                    text = (
                        f"🔍 watcher 新发现 {new_count} 条\n"
                        f"  候选清单: {stats['candidate']}\n"
                        f"  自动改写: {stats['auto_rewrite']}"
                    )
                    notify_feishu(chat_id, text)

            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            logger.info("[watcher] 中断退出")
            break
        except Exception as e:
            logger.error(f"[watcher] 循环异常: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main_loop()
