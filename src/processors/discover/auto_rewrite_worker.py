"""自动改写 worker（PM2 cron 消费者）。

每 5 分钟拉一次飞书"候选清单"中状态=auto_rewrite 的记录，
为每条调用 main.process() 走完整的下载→转写→改写→落文档流程，
完成后更新状态 → "已改写"（带改写文档链接）或 "失败"。

PM2: pm2 start auto_rewrite_worker.py --name discover-rewriter \
                  --cron-restart "*/5 * * * *" --no-autorestart

为什么限 MAX_PER_RUN=3：
  - 保护 Claude / Qwen 配额（一条 6-10 分钟、跑 LLM 多次）
  - 飞书 IM 通知不刷屏
  - 单次 cron 不超过 30 分钟（PM2 cron 重启窗口）

新增于 2026-05-13。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import yaml

from src.utils.logger import logger
from src.processors.discover import candidates


CONFIG_PATH = PROJECT_ROOT / "config" / "discover.yaml"
MAX_PER_RUN = 3


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"[rewriter] 读 config 失败: {e}")
        return {}


def _extract_url(fields: dict) -> str:
    """从 record fields 提取链接。

    飞书"链接"字段可能形式：
      - {"text": "...", "link": "..."}
      - [{"text": "...", "link": "..."}]
      - 字符串
    """
    raw = fields.get("链接")
    if not raw:
        return ""
    # 飞书 URL 字段返回 list 包裹的对象（新格式）
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        return raw.get("link") or raw.get("text") or ""
    if isinstance(raw, str):
        return raw
    return ""


def _extract_title(fields: dict) -> str:
    """飞书文本字段可能是 str 或 list of dict（含 "text" key）。"""
    raw = fields.get("标题")
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            return first.get("text", "")
        return str(first)
    return str(raw or "")


def _build_parsed(record: dict, chat_id: str) -> dict:
    """把飞书 record 转成 main.process() 期望的 parsed dict。

    main.on_message 构造的 parsed schema（main.py:185-193）：
      chat_id, message_id, sender_open_id, msg_type, url, file_key, file_name
    """
    fields = record.get("fields", {}) or {}
    url = _extract_url(fields)
    return {
        "chat_id": chat_id,
        "message_id": "",  # worker 触发，无飞书消息
        # 不传伪 ID 避免飞书"提交人"字段 UserFieldConvFail（必须是 ou_xxx 格式）
        # 业务表"提交人"字段对 worker 触发的留空
        "sender_open_id": "",
        "msg_type": "text",
        "url": url,
        "file_key": None,
        "file_name": None,
        "_discover_record_id": record.get("record_id"),
        "_discover_title": _extract_title(fields),
    }


def _has_doc_url(fields: dict) -> bool:
    """已写入改写文档？"""
    raw = fields.get("改写文档")
    if not raw:
        return False
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            return bool(first.get("link"))
    if isinstance(raw, dict):
        return bool(raw.get("link"))
    return bool(raw)


def run_once() -> dict:
    """跑一轮：拉单 → process → 更新状态。"""
    logger.info("[rewriter] === 启动一轮 ===")
    stats = {"pulled": 0, "skipped_has_doc": 0, "success": 0, "failed": 0, "no_url": 0}

    config = _load_config()
    cron_cfg = (config.get("triggers") or {}).get("cron") or {}
    chat_id = cron_cfg.get("notify_chat_id", "")

    pending = candidates.list_auto_rewrite_pending(limit=MAX_PER_RUN * 2)
    stats["pulled"] = len(pending)
    if not pending:
        logger.info("[rewriter] 无待改写记录，本轮退出")
        return stats

    # 延迟导入 main.process（依赖 lark_oapi 等重模块），避免 PM2 启动报错
    try:
        from src.main import process as main_process
    except Exception as e:
        logger.error(f"[rewriter] import main.process 失败: {e}")
        return stats

    processed = 0
    for rec in pending:
        if processed >= MAX_PER_RUN:
            logger.info(f"[rewriter] 达到本轮上限 MAX_PER_RUN={MAX_PER_RUN}")
            break

        rec_id = rec.get("record_id")
        fields = rec.get("fields", {}) or {}

        # 跳过已写文档的（双重保险，避免重复改写）
        if _has_doc_url(fields):
            stats["skipped_has_doc"] += 1
            continue

        parsed = _build_parsed(rec, chat_id)
        if not parsed["url"]:
            logger.warning(f"[rewriter] record {rec_id} 无链接，标失败")
            candidates.update_status(rec_id, candidates.STATUS_FAILED)
            stats["no_url"] += 1
            continue

        title = parsed.get("_discover_title", "")
        logger.info(f"[rewriter] 处理 {rec_id} → {title[:30]} → {parsed['url'][:60]}")

        # 立刻锁状态：防止 worker 重启时下一轮拉到同一条 → 重复下载浪费配额
        if not candidates.update_status(rec_id, candidates.STATUS_PROCESSING):
            logger.warning(f"[rewriter] {rec_id} 锁状态失败，跳过本条")
            continue

        # main.process 内部 catch 异常不上抛——只能通过验证飞书业务表是否写入新记录来判断真实成败
        from src.handlers import feishu_bitable
        try:
            main_process(parsed)
        except Exception as e:
            logger.exception(f"[rewriter] {rec_id} main.process 抛异常: {e}")

        # verify：业务表是否真的写入了这条 URL
        try:
            existing = feishu_bitable.find_by_url(parsed["url"])
            if existing:
                candidates.update_status(rec_id, candidates.STATUS_REWRITTEN)
                stats["success"] += 1
                logger.info(f"[rewriter] {rec_id} 改写成功（业务表已 verify）")
            else:
                candidates.update_status(rec_id, candidates.STATUS_FAILED)
                stats["failed"] += 1
                logger.warning(f"[rewriter] {rec_id} 改写失败（业务表无记录）")
        except Exception as e:
            logger.exception(f"[rewriter] {rec_id} verify 业务表失败: {e}")
            candidates.update_status(rec_id, candidates.STATUS_FAILED)
            stats["failed"] += 1
        processed += 1
        # 邻接两条之间休息，缓 LLM 配额
        time.sleep(3)

    logger.info(f"[rewriter] === 完成 === stats={stats}")

    # 通知飞书（有变动才发）
    if chat_id and (stats["success"] + stats["failed"] + stats["no_url"]) > 0:
        try:
            from src.processors.discover.cron import notify_feishu
            text = (
                f"🤖 discover/rewriter 跑完一轮\n"
                f"  拉单: {stats['pulled']}\n"
                f"  成功: {stats['success']}\n"
                f"  失败: {stats['failed']}\n"
                f"  无链接: {stats['no_url']}\n"
                f"  已有文档跳过: {stats['skipped_has_doc']}"
            )
            notify_feishu(chat_id, text)
        except Exception as e:
            logger.error(f"[rewriter] 通知飞书失败: {e}")

    return stats


def main_loop(interval_sec: int = 900):
    """常驻 worker 主循环。

    PM2 autorestart 模式启动，无 cron-restart 杀任务问题。
    interval_sec=900（15 分钟）足够单条 main.process（典型 5-15 分钟）跑完。

    启动时调 reclaim_stale_processing 把上一次 worker 卡死（异常退出 / kill -9）
    的"🔄 处理中"记录回滚回"📝 待改写"，让本轮重试。
    """
    logger.info(f"[rewriter] === 常驻 worker 启动 interval={interval_sec}s ===")
    # 启动时回滚卡死记录（防上轮异常退出）
    try:
        reclaimed = candidates.reclaim_stale_processing(timeout_minutes=60)
        if reclaimed:
            logger.info(f"[rewriter] 启动时 reclaim 回滚 {reclaimed} 条")
    except Exception as e:
        logger.error(f"[rewriter] 启动 reclaim 异常: {e}")

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logger.info("[rewriter] 中断退出")
            break
        except Exception as e:
            logger.exception(f"[rewriter] 主循环异常: {e}")
        time.sleep(interval_sec)


def main():
    """入口：默认常驻；--once 跑一次退出（调试用）。"""
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        result = run_once()
        print(f"\n=== rewriter 跑一次完成 ===\n{result}")
    else:
        main_loop()


if __name__ == "__main__":
    main()
