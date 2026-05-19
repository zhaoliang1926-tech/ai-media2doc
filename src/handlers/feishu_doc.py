import os
from lark_oapi.api.drive.v1 import (
    ListFileRequest, CreateFolderFileRequest, CreateFolderFileRequestBody
)
from lark_oapi.api.docx.v1 import (
    CreateDocumentRequest, CreateDocumentRequestBody,
    CreateDocumentBlockChildrenRequest, CreateDocumentBlockChildrenRequestBody,
    Block, Text, TextElement, TextRun, TextElementStyle, Divider,
)
from src.utils.logger import logger
from src.utils.helper import get_month_folder_name, format_number
from src.handlers.feishu_client import client

ROOT_FOLDER_TOKEN = os.getenv("FEISHU_FOLDER_TOKEN")


# ── 文档块构建辅助函数 ──

def _heading2(text) -> Block:
    return Block.builder().block_type(4).heading2(
        Text.builder().elements([
            TextElement.builder().text_run(
                TextRun.builder().content(text).build()
            ).build()
        ]).build()
    ).build()


def _paragraph(text, bold=False) -> Block:
    text_run = TextRun.builder().content(text)
    if bold:
        text_run = text_run.text_element_style(
            TextElementStyle.builder().bold(True).build()
        )
    return Block.builder().block_type(2).text(
        Text.builder().elements([
            TextElement.builder().text_run(text_run.build()).build()
        ]).build()
    ).build()


def _safe_paragraphs(text, bold=False, max_len=1500) -> list:
    """超长文本自动拆分为多个块。"""
    result = []
    while len(text) > max_len:
        result.append(_paragraph(text[:max_len], bold))
        text = text[max_len:]
    result.append(_paragraph(text, bold))
    return result


def _divider() -> Block:
    return Block.builder().block_type(22).divider(Divider()).build()


def _get_or_create_month_folder() -> str:
    month_name = get_month_folder_name()

    # 列出根目录下的文件
    req = ListFileRequest.builder() \
        .folder_token(ROOT_FOLDER_TOKEN) \
        .build()
    resp = client.drive.v1.file.list(req)

    if resp.success():
        for item in (resp.data.files or []):
            if item.name == month_name and item.type == "folder":
                logger.info(f"找到已有月份文件夹：{month_name}")
                return item.token

    # 不存在则创建
    create_req = CreateFolderFileRequest.builder() \
        .request_body(CreateFolderFileRequestBody.builder()
                      .name(month_name)
                      .folder_token(ROOT_FOLDER_TOKEN)
                      .build()) \
        .build()
    create_resp = client.drive.v1.file.create_folder(create_req)
    if create_resp.success():
        logger.info(f"创建月份文件夹：{month_name}")
        return create_resp.data.token

    logger.error(f"创建文件夹失败：{create_resp.msg}")
    return ROOT_FOLDER_TOKEN


def create_doc(
    title: str,
    rewrite_result,
    meta,
    transcript: str,
) -> str:
    folder_token = _get_or_create_month_folder()
    # 飞书文档标题限制 256 字符
    title = title[:256] if title else "无标题"

    # 创建空白文档
    create_req = CreateDocumentRequest.builder() \
        .request_body(CreateDocumentRequestBody.builder()
                      .title(title)
                      .folder_token(folder_token)
                      .build()) \
        .build()
    create_resp = client.docx.v1.document.create(create_req)

    if not create_resp.success():
        logger.error(f"创建飞书文档失败：{create_resp.msg}")
        return ""

    doc_id = create_resp.data.document.document_id
    doc_url = f"https://bytedance.feishu.cn/docx/{doc_id}"
    logger.info(f"文档创建成功：{doc_url}")

    # 构建文档内容块
    blocks = _build_doc_blocks(rewrite_result, meta, transcript)

    # 批量写入内容（飞书限制每次最多 50 个 block）
    BATCH_SIZE = 50
    for i in range(0, len(blocks), BATCH_SIZE):
        batch = blocks[i:i + BATCH_SIZE]
        patch_req = CreateDocumentBlockChildrenRequest.builder() \
            .document_id(doc_id) \
            .block_id(doc_id) \
            .request_body(CreateDocumentBlockChildrenRequestBody.builder()
                          .children(batch)
                          .build()) \
            .build()
        patch_resp = client.docx.v1.document_block_children.create(patch_req)

        if not patch_resp.success():
            logger.error(f"写入文档内容失败（批次{i // BATCH_SIZE + 1}）：{patch_resp.msg}")
            break

    return doc_url


def append_claude_rewrite(doc_url: str, rewrite_result) -> None:
    """将 Claude 改写结果插入到文档最前面（index=0）。"""
    doc_id = doc_url.rstrip("/").split("/")[-1]

    blocks = [
        _divider(),
        _heading2("🤖 1号小助理改写版"),
        _paragraph("【改写标题】", bold=True),
    ]
    for i, t in enumerate(rewrite_result.titles, 1):
        label = ["疑问式", "数字式", "共鸣式"][i - 1] if i <= 3 else f"备选{i}"
        blocks.extend(_safe_paragraphs(f"  {label}：{t}"))
    blocks.append(_paragraph(""))

    blocks.append(_paragraph("【结构拆解】", bold=True))
    if rewrite_result.hook:
        blocks.extend(_safe_paragraphs(f"  开头钩子（前5秒）：{rewrite_result.hook}"))
    if rewrite_result.hook_analysis:
        blocks.extend(_safe_paragraphs(f"  钩子分析：{rewrite_result.hook_analysis}"))
    if rewrite_result.body_analysis:
        blocks.extend(_safe_paragraphs(f"  中间锚点&痛点：{rewrite_result.body_analysis}"))
    if rewrite_result.ending_analysis:
        blocks.extend(_safe_paragraphs(f"  结尾引导：{rewrite_result.ending_analysis}"))
    blocks.append(_paragraph(""))

    blocks.append(_paragraph("【情绪&用户画像】", bold=True))
    if rewrite_result.emotion:
        blocks.extend(_safe_paragraphs(f"  情绪价值：{rewrite_result.emotion}"))
    if rewrite_result.audience:
        blocks.extend(_safe_paragraphs(f"  用户画像：{rewrite_result.audience}"))
    if rewrite_result.format:
        blocks.extend(_safe_paragraphs(f"  呈现形式：{rewrite_result.format}"))
    blocks.append(_paragraph(""))

    content_len = len(rewrite_result.content.replace(" ", "").replace("\n", ""))
    blocks.append(_paragraph(f"【改写文案】（共{content_len}字）", bold=True))
    for line in rewrite_result.content.split("\n"):
        blocks.extend(_safe_paragraphs(line))
    blocks.append(_paragraph(""))

    tags = " ".join([f"#{t}" for t in rewrite_result.hashtags])
    blocks.extend(_safe_paragraphs(f"话题标签：{tags}"))
    blocks.append(_paragraph(""))

    blocks.append(_paragraph("【引导互动评论】", bold=True))
    for i, c in enumerate(rewrite_result.comments, 1):
        blocks.extend(_safe_paragraphs(f"  备选{i}：{c}"))

    # 飞书单次写入限制 50 个 block，分批插入
    CHUNK = 50
    for i, start in enumerate(range(0, len(blocks), CHUNK)):
        chunk = blocks[start:start + CHUNK]
        patch_req = CreateDocumentBlockChildrenRequest.builder() \
            .document_id(doc_id) \
            .block_id(doc_id) \
            .request_body(CreateDocumentBlockChildrenRequestBody.builder()
                          .children(chunk)
                          .index(i * CHUNK)
                          .build()) \
            .build()
        patch_resp = client.docx.v1.document_block_children.create(patch_req)
        if not patch_resp.success():
            logger.error(f"Claude 改写追加失败：{patch_resp.msg}")
            return
    logger.info(f"Claude 改写插入成功：{doc_url}")


def _build_doc_blocks(rewrite_result, meta, transcript: str) -> list:
    blocks = []

    # ── Qwen 改写区 ──
    blocks.append(_heading2("✍️ 2号小助理改写版"))

    blocks.append(_paragraph("【改写标题】", bold=True))
    for i, t in enumerate(rewrite_result.titles, 1):
        label = ["疑问式", "数字式", "共鸣式"][i - 1] if i <= 3 else f"备选{i}"
        blocks.append(_paragraph(f"  {label}：{t}"))
    blocks.append(_paragraph(""))

    blocks.append(_paragraph("【结构拆解】", bold=True))
    if rewrite_result.hook:
        blocks.append(_paragraph(f"  开头钩子（前5秒原文）：{rewrite_result.hook}"))
    if rewrite_result.hook_analysis:
        blocks.append(_paragraph(f"  钩子分析：{rewrite_result.hook_analysis}"))
    if rewrite_result.body_analysis:
        blocks.append(_paragraph(f"  中间锚点&痛点：{rewrite_result.body_analysis}"))
    if rewrite_result.ending_analysis:
        blocks.append(_paragraph(f"  结尾引导：{rewrite_result.ending_analysis}"))
    blocks.append(_paragraph(""))

    blocks.append(_paragraph("【情绪&用户画像】", bold=True))
    if rewrite_result.emotion:
        blocks.append(_paragraph(f"  情绪价值：{rewrite_result.emotion}"))
    if rewrite_result.audience:
        blocks.append(_paragraph(f"  用户画像：{rewrite_result.audience}"))
    if rewrite_result.format:
        blocks.append(_paragraph(f"  呈现形式：{rewrite_result.format}"))
    blocks.append(_paragraph(""))

    content_len = len(rewrite_result.content.replace(" ", "").replace("\n", ""))
    blocks.append(_paragraph(f"【改写文案】（共{content_len}字）", bold=True))
    for line in rewrite_result.content.split("\n"):
        blocks.extend(_safe_paragraphs(line))
    blocks.append(_paragraph(""))

    tags = " ".join([f"#{t}" for t in rewrite_result.hashtags])
    blocks.append(_paragraph(f"话题标签：{tags}"))
    blocks.append(_paragraph(""))

    blocks.append(_paragraph("【引导互动评论】", bold=True))
    for i, c in enumerate(rewrite_result.comments, 1):
        blocks.append(_paragraph(f"  备选{i}：{c}"))

    # ── 分割线 ──
    blocks.append(_divider())

    # ── 原始素材区 ──
    blocks.append(_heading2("📄 原始素材"))

    if meta.title:
        blocks.append(_paragraph(f"标题：{meta.title}"))
    if meta.author:
        blocks.append(_paragraph(f"来源账号：@{meta.author}"))
    if meta.url:
        blocks.append(_paragraph(f"原始链接：{meta.url}"))

    stats_parts = []
    if meta.like_count is not None:
        stats_parts.append(f"点赞 {format_number(meta.like_count)}")
    if meta.collect_count is not None:
        stats_parts.append(f"收藏 {format_number(meta.collect_count)}")
    if meta.share_count is not None:
        stats_parts.append(f"转发 {format_number(meta.share_count)}")
    if meta.comment_count is not None:
        stats_parts.append(f"评论 {format_number(meta.comment_count)}")
    if stats_parts:
        blocks.append(_paragraph("  ".join(stats_parts)))

    if meta.duration:
        duration_secs = meta.duration // 1000 if meta.duration > 3600 else meta.duration
        mins, secs = divmod(duration_secs, 60)
        blocks.append(_paragraph(f"视频时长：{mins}分{secs}秒"))

    blocks.append(_paragraph(""))
    transcript_text = transcript or "（转写内容为空）"
    transcript_len = len(transcript_text.replace(" ", "").replace("\n", ""))
    blocks.append(_paragraph(f"【原文字幕】（共{transcript_len}字）", bold=True))
    for line in transcript_text.split("\n"):
        blocks.extend(_safe_paragraphs(line))

    return blocks
