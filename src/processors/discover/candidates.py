"""候选清单飞书多维表格 CRUD。

把 CandidateRaw + hits + score 转成飞书表 record 写入"候选清单"。
表 schema 18 字段（建表已通过飞书 API 完成）。

状态字段语义：
  待审         - 普通候选（人工筛选）
  auto_rewrite - 命中自动改写阈值（auto_rewrite_worker 拉单消费）
  已改写       - worker 完成 process()，写入了改写文档链接
  失败         - worker 调用 main.process() 抛异常

新增于 2026-05-11。
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import List, Optional

import requests

from src.utils.logger import logger
from src.processors.discover.engine import CandidateRaw


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

# 状态枚举
STATUS_PENDING = "待审"             # 默认初始态（用户人工筛选）
STATUS_AUTO_REWRITE = "auto_rewrite"  # 系统判定高分自动改写（worker 拉走）
STATUS_USER_REQUESTED = "✅ 确认改写"  # 用户人工触发改写（worker 拉走）
STATUS_PROCESSING = "🔄 处理中"      # worker 已拉单正在跑 process（防重复拉单的锁）
STATUS_REWRITTEN = "已改写"          # worker 改写成功
STATUS_FAILED = "失败"               # worker 改写失败
STATUS_IGNORED = "已忽略"            # 用户跳过


def _get_token() -> Optional[str]:
    try:
        r = requests.post(
            f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": os.getenv("FEISHU_APP_ID"),
                "app_secret": os.getenv("FEISHU_APP_SECRET"),
            },
            timeout=10,
        )
        data = r.json()
        if data.get("code") == 0:
            return data["tenant_access_token"]
        logger.error(f"[candidates] 飞书 token 失败: {data}")
    except Exception as e:
        logger.error(f"[candidates] 飞书 token 异常: {e}")
    return None


def _app_token() -> str:
    return os.getenv("FEISHU_CANDIDATE_APP_TOKEN", "")


def _table_id() -> str:
    return os.getenv("FEISHU_CANDIDATE_TABLE_ID", "tblRqFfAdrCJVWAF")


def _build_record(meta: CandidateRaw, hits: dict, score_value: int, trigger: str,
                  classification: str = "candidate") -> dict:
    platform_label = "小红书" if meta.platform == "xiaohongshu" else "抖音"
    # classification → 状态：auto_rewrite 走自动改写 worker，其余进人工候选清单
    if classification == "auto_rewrite":
        status = STATUS_AUTO_REWRITE
    else:
        status = STATUS_PENDING
    fields = {
        "标题": (meta.title or "")[:200],
        "平台": platform_label,
        "作者": meta.author or "",
        "作者ID": meta.author_id or "",
        "点赞": int(meta.like_count or 0),
        "收藏": int(meta.collect_count or 0),
        "评论": int(meta.comment_count or 0),
        "分享": int(meta.share_count or 0),
        "发现时间": int(time.time() * 1000),
        "触发条件": trigger,
        "分数": int(score_value),
        "状态": status,
    }
    if meta.url:
        fields["链接"] = {"link": meta.url, "text": meta.url[:60]}
    if meta.published_at:
        try:
            fields["发布时间"] = int(meta.published_at.timestamp() * 1000)
        except (AttributeError, TypeError):
            pass
    if hits.get("keywords"):
        fields["命中关键词"] = list(hits["keywords"])
    if hits.get("account"):
        fields["命中账号"] = hits["account"]
    if hits.get("topics"):
        fields["命中话题"] = list(hits["topics"])
    return fields


def insert_candidate(meta: CandidateRaw, hits: dict, score_value: int, trigger: str,
                     classification: str = "candidate") -> Optional[str]:
    """写入候选清单。返回新 record_id；失败返回 None。

    classification: "auto_rewrite" → 状态写 "auto_rewrite"（待 worker 消费）
                    其他            → 状态写 "待审"（人工筛选）
    """
    token = _get_token()
    if not token:
        return None
    fields = _build_record(meta, hits, score_value, trigger, classification)
    try:
        r = requests.post(
            f"{FEISHU_API_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=15,
        )
        data = r.json()
        if data.get("code") == 0:
            rec_id = data.get("data", {}).get("record", {}).get("record_id")
            logger.info(f"[candidates] 写入 {meta.platform}/{meta.note_id} → {rec_id}")
            return rec_id
        logger.warning(f"[candidates] 写入失败 ({meta.note_id}): {data.get('msg')}")
        return None
    except Exception as e:
        logger.error(f"[candidates] 写入异常 ({meta.note_id}): {e}")
        return None


def update_status(record_id: str, status: str, doc_url: str = "") -> bool:
    """更新状态 + 可选改写文档链接。"""
    token = _get_token()
    if not token:
        return False
    fields = {"状态": status}
    if doc_url:
        fields["改写文档"] = {"link": doc_url, "text": "查看文档"}
    try:
        r = requests.put(
            f"{FEISHU_API_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records/{record_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10,
        )
        data = r.json()
        if data.get("code") == 0:
            logger.info(f"[candidates] 状态更新 {record_id} → {status}")
            return True
        logger.warning(f"[candidates] 状态更新失败 {record_id}: {data.get('msg')}")
        return False
    except Exception as e:
        logger.error(f"[candidates] 状态更新异常: {e}")
        return False


def find_by_note_id(note_id: str) -> Optional[dict]:
    """通过 note_id 查候选记录（防重复写入的二次保险）。"""
    token = _get_token()
    if not token:
        return None
    try:
        r = requests.post(
            f"{FEISHU_API_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records/search",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {"field_name": "链接", "operator": "contains", "value": [note_id]},
                    ],
                },
                "page_size": 1,
            },
            timeout=10,
        )
        items = r.json().get("data", {}).get("items", [])
        return items[0] if items else None
    except Exception:
        return None


def daily_count(trigger: Optional[str] = None) -> int:
    """统计今天写入的候选记录数（用于 daily_max_candidates 限流）。"""
    token = _get_token()
    if not token:
        return 0
    today_start_ms = int(
        datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    conditions = [
        {"field_name": "发现时间", "operator": "isGreater", "value": ["ExactDate", str(today_start_ms)]}
    ]
    if trigger:
        conditions.append({"field_name": "触发条件", "operator": "is", "value": [trigger]})
    try:
        r = requests.post(
            f"{FEISHU_API_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records/search",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"filter": {"conjunction": "and", "conditions": conditions}, "page_size": 1},
            timeout=10,
        )
        return r.json().get("data", {}).get("total", 0)
    except Exception:
        return 0


def list_auto_rewrite_pending(limit: int = 5) -> List[dict]:
    """列出待改写的记录。

    两种触发路径合并查询：
      - 状态 = auto_rewrite（系统高分自动判定）
      - 状态 = "📝 待改写"（用户在飞书表手动选择触发）

    返回 list[record dict]，每个 record 含 record_id 和 fields。worker 拉单消费。
    返回空 list 表示无任务（或飞书 token 失败）。
    """
    token = _get_token()
    if not token:
        return []
    all_items: List[dict] = []
    # 飞书 search 单选 "is" 一次只查一个值；两个状态分别查
    for status_val in (STATUS_AUTO_REWRITE, STATUS_USER_REQUESTED):
        try:
            r = requests.post(
                f"{FEISHU_API_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records/search",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "filter": {
                        "conjunction": "and",
                        "conditions": [
                            {"field_name": "状态", "operator": "is", "value": [status_val]},
                        ],
                    },
                    "page_size": max(1, min(limit, 50)),
                },
                timeout=15,
            )
            data = r.json()
            if data.get("code") != 0:
                logger.warning(f"[candidates.list_auto_rewrite_pending] {status_val} 查询失败: {data.get('msg')}")
                continue
            items = data.get("data", {}).get("items", []) or []
            all_items.extend(items)
            if len(all_items) >= limit:
                break
        except Exception as e:
            logger.error(f"[candidates.list_auto_rewrite_pending] {status_val} 异常: {e}")
    return all_items[:limit]


def reclaim_stale_processing(timeout_minutes: int = 60) -> int:
    """回收"处理中"超时的候选 → 回滚成"📝 待改写"。

    worker 重启或异常退出时，原本标"🔄 处理中"的记录会卡住——
    本函数定期 / 启动时调用，把超时的回滚让下次再跑。

    实际生产中：worker 进入 main_loop 第一件事就调一次（重启时清理上一批卡死的）
    """
    token = _get_token()
    if not token:
        return 0
    try:
        r = requests.post(
            f"{FEISHU_API_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records/search",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {"field_name": "状态", "operator": "is", "value": [STATUS_PROCESSING]},
                    ],
                },
                "page_size": 50,
            },
            timeout=15,
        )
        data = r.json()
        if data.get("code") != 0:
            logger.warning(f"[reclaim_stale_processing] 查询失败: {data.get('msg')}")
            return 0
        items = data.get("data", {}).get("items", []) or []

        # 用飞书系统字段 last_modified_time（毫秒）判断超时
        # 实际飞书 record.fields 不含 last_modified_time，需 record 自带的字段
        # 简化：worker 启动 reclaim 时**全部回滚**（启动时机说明这些记录是上一次没跑完的）
        # 定时 reclaim 用 timeout_minutes 做 cutoff
        import time as _t
        cutoff_ms = int((_t.time() - timeout_minutes * 60) * 1000)
        reclaimed = 0
        for it in items:
            rid = it.get("record_id")
            # last_modified_time 在飞书 search 返回字段里（v1 API 返回顶层）
            last_mt = it.get("last_modified_time") or it.get("record_last_modified_time") or 0
            try:
                last_mt = int(last_mt)
            except (ValueError, TypeError):
                last_mt = 0
            # 没拿到 last_mt 也回滚（保守策略——重启场景下都应回滚）
            if last_mt == 0 or last_mt < cutoff_ms:
                if update_status(rid, STATUS_USER_REQUESTED):
                    reclaimed += 1
        if reclaimed:
            logger.info(f"[reclaim_stale_processing] 回滚 {reclaimed} 条超时的'处理中'记录 → 📝 待改写")
        return reclaimed
    except Exception as e:
        logger.error(f"[reclaim_stale_processing] 异常: {e}")
        return 0
