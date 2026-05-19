"""飞书群命令处理器：/发现 <关键词> + 自然语言触发

由 main.py on_message 调用：
1. 严格命令：`/发现 销售` / `/discover 销售`
2. 自然语言：消息含触发词（"发现一下/找一下/搜一下/帮我找/帮我搜/扫一下"）
   + 含 yaml.keywords 任一关键词 → 提取该关键词触发

异步执行（不阻塞 ws），完成后回执飞书群。

新增于 2026-05-11。自然语言触发于 2026-05-13 加入。
"""
from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from typing import Optional, List

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.logger import logger


COMMAND_PATTERN = re.compile(r"^/(?:发现|discover)\s+(.+?)\s*$")

# 自然语言触发词——含查询/浏览意图的措辞；yaml.keywords 是二次门槛防误触发
NL_TRIGGER_WORDS = [
    # 动词类（明确"想找"）
    "发现一下", "发现下", "找一下", "搜一下", "扫一下",
    "帮我找", "帮我搜", "帮我发现", "搜搜",
    # 问句类（用户提问意图）
    "有什么", "有没有", "今天有",
    # 推荐 / 浏览类
    "推荐", "看几条", "刷一下",
]

# 自然语言消息最大长度（超过认为不是命令）
NL_MAX_LEN = 60


def parse_command(text: str) -> Optional[str]:
    """严格命令解析：/发现 <kw>。"""
    if not text:
        return None
    m = COMMAND_PATTERN.match(text.strip())
    if m:
        return m.group(1).strip()
    return None


def parse_natural_intent(text: str, yaml_keywords: List[str]) -> Optional[str]:
    """自然语言意图识别：双门槛——必须含触发词 + 必须含 yaml.keywords 任一。

    例如：
      "帮我发现一下销售相关的视频" → 含"发现一下" + 含"销售" → 返回 "销售"
      "今天聊聊销售" → 不含触发词 → None（避免误触发）
      "帮我看下" → 不含 yaml 关键词 → None
    """
    if not text:
        return None
    s = text.strip()
    if len(s) > NL_MAX_LEN:
        return None
    if not any(w in s for w in NL_TRIGGER_WORDS):
        return None
    for kw in yaml_keywords or []:
        if kw and kw in s:
            return kw
    return None


def _execute(keyword: str, chat_id: str):
    """后台执行（不阻塞 ws）。"""
    from src.processors.discover import engine, filters, scorer, dedupe, candidates
    from src.processors.discover.cron import notify_feishu, load_config

    try:
        config = load_config()
        limits = config.get("limits") or {}
        ttl_days = int(limits.get("dedupe_window_days", 30))

        notify_feishu(chat_id, f"🔍 收到 /发现 {keyword}，开始扫描两个平台...")

        raw_xhs = engine.fetch_xhs_search([keyword], limit=10)
        raw_dy = engine.fetch_dy_search([keyword], limit=10)
        raw_all = raw_xhs + raw_dy

        if not raw_all:
            notify_feishu(chat_id, f"🔍 /发现 {keyword} 无结果（引擎未登录或反爬拦截）")
            return

        passed = filters.filter_batch(raw_all, config)
        stats = {"candidate": 0, "auto_rewrite": 0, "duplicate": 0, "drop": 0}

        for meta, hits in passed:
            if keyword not in (hits.get("keywords") or []):
                hits.setdefault("keywords", []).append(keyword)

            if dedupe.is_duplicate(meta.platform, meta.note_id, ttl_days=ttl_days):
                stats["duplicate"] += 1
                continue
            score_value = scorer.score(meta, hits, config)
            classification = scorer.classify(meta, score_value, config)
            if classification == scorer.CLASS_DROP:
                stats["drop"] += 1
                dedupe.mark_seen(meta.platform, meta.note_id, status="drop")
                continue
            record_id = candidates.insert_candidate(meta, hits, score_value, trigger="command",
                                                    classification=classification)
            if record_id:
                if classification == scorer.CLASS_AUTO_REWRITE:
                    stats["auto_rewrite"] += 1
                    dedupe.mark_seen(meta.platform, meta.note_id, status="auto_rewrite")
                else:
                    stats["candidate"] += 1
                    dedupe.mark_seen(meta.platform, meta.note_id, status="candidate")

        text = (
            f"🔍 /发现 {keyword} 完成\n"
            f"输入 {len(raw_all)} → 通过 {len(passed)} →\n"
            f"  候选清单: {stats['candidate']}\n"
            f"  自动改写: {stats['auto_rewrite']}\n"
            f"  去重跳过: {stats['duplicate']}\n"
            f"  分数过低: {stats['drop']}"
        )
        notify_feishu(chat_id, text)
        logger.info(f"[command] /发现 {keyword}: {stats}")
    except Exception as e:
        logger.error(f"[command] /发现 {keyword} 异常: {e}")
        try:
            notify_feishu(chat_id, f"❌ /发现 {keyword} 失败: {str(e)[:80]}")
        except Exception:
            pass


def handle_discover_command(text: str, chat_id: str) -> bool:
    """供 main.py on_message 调用。匹配返回 True 并 spawn 异步任务。

    两路触发：
      1. 严格命令 /发现 <kw> （parse_command）
      2. 自然语言（parse_natural_intent，需 yaml.keywords 上下文）
    """
    # 路径 1：严格命令
    keyword = parse_command(text)
    matched_by = "command"

    # 路径 2：fallback 自然语言意图
    if not keyword:
        try:
            from src.processors.discover.cron import load_config
            yaml_kw = (load_config().get("keywords") or [])
            keyword = parse_natural_intent(text, yaml_kw)
            if keyword:
                matched_by = "natural"
        except Exception as e:
            logger.error(f"[command.nl] 加载 yaml 异常: {e}")

    if not keyword:
        return False

    threading.Thread(target=_execute, args=(keyword, chat_id), daemon=True).start()
    logger.info(f"[command] 匹配 ({matched_by}) → {keyword}，已 spawn 异步任务")
    return True
