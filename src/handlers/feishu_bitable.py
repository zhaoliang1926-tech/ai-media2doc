import os
from datetime import datetime
from lark_oapi.api.bitable.v1 import *
from src.utils.logger import logger
from src.handlers.feishu_client import client

BITABLE_APP_TOKEN = os.getenv("FEISHU_BITABLE_APP_TOKEN")
TABLE_ID = os.getenv("FEISHU_BITABLE_TABLE_ID")


def insert_record(
    meta,
    rewrite_result,
    doc_url: str,
    submitter_open_id: str = "",
) -> bool:
    now = datetime.now()

    # 业务表"视频分类"是单选，只允许 ['知识', '情感']——必须映射，否则 UserFieldConvFail
    # 含"情感/恋爱/关系/家庭"关键词 → 情感；其余统一规整为"知识"
    raw_category = (rewrite_result.category or "").strip()
    if any(kw in raw_category for kw in ("情感", "恋爱", "关系", "家庭", "婚姻")):
        mapped_category = "情感"
    else:
        mapped_category = "知识"

    fields = {
        "标题": meta.title or "未知标题",
        "抖音链接": {"link": meta.url, "text": meta.url} if meta.url else "",
        "来源账号": meta.author or "",
        "改写概要": rewrite_result.summary or "",
        "视频分类": mapped_category,
        "月份": int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000),
        "处理状态": "✅ 完成",
        "查看文档": {"link": doc_url, "text": "打开改写文档"} if doc_url else "",
        "提交时间": int(now.timestamp() * 1000),
        "是否已录制": False,
    }

    if meta.like_count is not None:
        fields["点赞数"] = int(meta.like_count)
    if meta.collect_count is not None:
        fields["收藏数"] = int(meta.collect_count)
    if meta.share_count is not None:
        fields["转发数"] = int(meta.share_count)
    if meta.comment_count is not None:
        fields["评论数"] = int(meta.comment_count)
    if meta.duration is not None:
        duration_secs = meta.duration // 1000 if meta.duration > 3600 else meta.duration
        fields["视频时长"] = int(duration_secs)
    # 飞书"提交人"字段是 user 类型，必须传 ou_xxx 格式的 open_id；
    # 防御：非 ou_ 前缀直接跳过（避免 discover-worker 等假 ID 触发 UserFieldConvFail
    # 导致整条 insert 挂掉）
    if submitter_open_id and submitter_open_id.startswith("ou_"):
        fields["提交人"] = [{"id": submitter_open_id}]
    elif submitter_open_id:
        logger.warning(f"[feishu_bitable] 跳过非 ou_ 前缀 submitter_open_id: {submitter_open_id[:30]}")

    req = CreateAppTableRecordRequest.builder() \
        .app_token(BITABLE_APP_TOKEN) \
        .table_id(TABLE_ID) \
        .request_body(AppTableRecord.builder().fields(fields).build()) \
        .build()

    resp = client.bitable.v1.app_table_record.create(req)
    if resp.success():
        logger.info(f"多维表格写入成功：{meta.title}")
        return True
    else:
        # 详细 debug 信息定位 UserFieldConvFail 罪魁字段
        field_preview = {k: (f"{type(v).__name__}: {str(v)[:60]}") for k, v in fields.items()}
        logger.error(f"多维表格写入失败：code={resp.code} msg={resp.msg}")
        logger.error(f"  fields 预览: {field_preview}")
        try:
            raw_data = getattr(resp, "raw", None)
            if raw_data:
                logger.error(f"  raw resp: {str(raw_data)[:400]}")
        except Exception:
            pass
        return False


def find_by_url(url: str) -> dict | None:
    """查询多维表格中是否已有该 URL 的记录，返回记录信息或 None。

    搜索时去掉 query string（如小红书 xsec_token 这种动态参数），
    仅用 URL path 部分（含稳定的 note_id / aweme_id）匹配，避免误判。
    """
    try:
        # 关键修复（2026-05-17）：截断 query string
        # candidate 表 url 含 xsec_token=AAA，业务表 url 可能含 xsec_token=BBB
        # → 全字符串 contains 匹配会失败，用 path 部分（含 note_id）作锚
        search_url = url.split("?", 1)[0] if url else ""
        req = SearchAppTableRecordRequest.builder() \
            .app_token(BITABLE_APP_TOKEN) \
            .table_id(TABLE_ID) \
            .request_body(SearchAppTableRecordRequestBody.builder()
                          .filter(FilterInfo.builder()
                                  .conjunction("and")
                                  .conditions([
                                      Condition.builder()
                                      .field_name("抖音链接")
                                      .operator("contains")
                                      .value([search_url])
                                      .build()
                                  ])
                                  .build())
                          .build()) \
            .build()
        resp = client.bitable.v1.app_table_record.search(req)
        if resp.success() and resp.data and resp.data.items:
            record = resp.data.items[0]
            fields = record.fields
            doc_field = fields.get("查看文档", {})
            # 标题可能是富文本列表 [{"text": "...", "type": "text"}]
            raw_title = fields.get("标题", "未知标题")
            if isinstance(raw_title, list):
                title = "".join(seg.get("text", "") for seg in raw_title if isinstance(seg, dict))
            else:
                title = str(raw_title)
            return {
                "record_id": record.record_id,
                "title": title or "未知标题",
                "doc_url": doc_field.get("link", "") if isinstance(doc_field, dict) else "",
                "submit_time": fields.get("提交时间", 0),
            }
    except Exception as e:
        logger.error(f"查询重复记录失败：{e}")
    return None

