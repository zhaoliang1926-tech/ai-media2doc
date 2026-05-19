"""
poller.py - 轮询飞书文档，自动触发 1号小助理改写
每次运行：扫描当月文件夹 → 找未做 1号改写的文档 → POST 给 claude_server
"""
import os
import sys
import json
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

from src.handlers.feishu_doc import client
from src.utils.logger import logger
from lark_oapi.api.drive.v1 import ListFileRequest
from lark_oapi.api.docx.v1 import ListDocumentBlockRequest

CLAUDE_SERVER_URL = os.getenv("CLAUDE_SERVER_URL_LOCAL", "http://127.0.0.1:8765")
MARCH_FOLDER_TOKEN = os.getenv("MARCH_FOLDER_TOKEN", "")


def list_docs_in_folder(folder_token: str) -> list[dict]:
    """列出文件夹内所有 docx 文档"""
    docs = []
    page_token = None
    while True:
        b = ListFileRequest.builder().folder_token(folder_token).page_size(50)
        if page_token:
            b = b.page_token(page_token)
        resp = client.drive.v1.file.list(b.build())
        if not resp.success():
            break
        for f in (resp.data.files or []):
            if f.type == "docx":
                docs.append({"name": f.name, "token": f.token})
        if not resp.data.has_more:
            break
        page_token = resp.data.next_page_token
    return docs


def get_doc_blocks(doc_id: str) -> list:
    req = ListDocumentBlockRequest.builder().document_id(doc_id).page_size(500).build()
    resp = client.docx.v1.document_block.list(req)
    if not resp.success():
        return []
    return resp.data.items or []


def has_claude_rewrite(blocks) -> bool:
    """
    检查文档是否已有 1 号小助理改写版。
    修复：原版只看 b.text，跳过了 heading2 块（标题 "🤖 1号小助理改写版" 是 heading2）
    导致永远判定 "未改写"，每跑一次 poller 就重复提交一次。
    现在：把整个 block 序列化为 JSON，grep 所有可能字段（heading/text/quote 等都覆盖）。
    """
    import lark_oapi as lark
    for b in blocks:
        try:
            raw = lark.JSON.marshal(b)
            if "1号小助理" in raw:
                return True
        except Exception:
            # 兜底：序列化失败时退回原逻辑
            if b.text:
                content = "".join(
                    e.text_run.content for e in (b.text.elements or []) if e.text_run
                )
                if "1号小助理" in content:
                    return True
    return False


def get_transcript(blocks) -> str:
    texts = []
    in_transcript = False
    for b in blocks:
        if not b.text:
            continue
        content = "".join(
            e.text_run.content for e in (b.text.elements or []) if e.text_run
        )
        if "原文字幕" in content:
            in_transcript = True
            continue
        if in_transcript and content.strip():
            texts.append(content.strip())
    return "\n".join(texts)


def notify_claude_server(doc_url: str, transcript: str, title: str):
    payload = json.dumps(
        {"doc_url": doc_url, "transcript": transcript, "chat_id": "", "title": title}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{CLAUDE_SERVER_URL}/rewrite",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


def run():
    logger.info("开始轮询飞书文档...")
    docs = list_docs_in_folder(MARCH_FOLDER_TOKEN)
    logger.info(f"共找到 {len(docs)} 个文档")

    pending = []
    for doc in docs:
        doc_id = doc["token"]
        name = doc["name"]
        # 跳过测试文档
        if any(k in name for k in ["测试", "API", "分割线", "肖厂长"]):
            continue
        blocks = get_doc_blocks(doc_id)
        if not blocks:
            continue
        if has_claude_rewrite(blocks):
            logger.info(f"  [已有1号] {name[:30]}")
            continue
        transcript = get_transcript(blocks)
        if not transcript:
            logger.info(f"  [无转写] {name[:30]}")
            continue
        pending.append({"doc_id": doc_id, "name": name, "transcript": transcript})
        logger.info(f"  [待改写] {name[:30]} transcript={len(transcript)}字")

    if not pending:
        logger.info("没有待改写的文档")
        return

    for item in pending:
        doc_url = f"https://bytedance.feishu.cn/docx/{item['doc_id']}"
        try:
            status = notify_claude_server(doc_url, item["transcript"], item["name"])
            logger.info(f"已提交 1号改写：{item['name'][:30]} → {status}")
        except Exception as e:
            logger.error(f"提交失败 {item['name'][:30]}：{e}")


if __name__ == "__main__":
    run()
