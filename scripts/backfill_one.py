"""One-shot backfill: read transcript from an existing Feishu doc, POST to /rewrite."""
import os, sys, re, json, urllib.request, pathlib
sys.path.insert(0, "/Users/zhaoliang/Documents/GitHub/AI-Media2Doc")

from dotenv import load_dotenv
load_dotenv("/Users/zhaoliang/Documents/GitHub/AI-Media2Doc/.env")

from src.handlers.feishu_client import client
from lark_oapi.api.docx.v1 import RawContentDocumentRequest

DOC_ID = sys.argv[1] if len(sys.argv) > 1 else "QXcVdI2eRoU9CexkcJtcIWjSnsg"
TITLE = sys.argv[2] if len(sys.argv) > 2 else "(backfill)"
DRY = "--dry" in sys.argv

doc_url = f"https://bytedance.feishu.cn/docx/{DOC_ID}"

print(f"[1/3] 读取飞书文档 {DOC_ID}")
req = RawContentDocumentRequest.builder().document_id(DOC_ID).build()
resp = client.docx.v1.document.raw_content(req)
if not resp.success():
    print(f"  FAIL: code={resp.code} msg={resp.msg}")
    sys.exit(1)
raw = resp.data.content
print(f"  raw_content 长度: {len(raw)} 字")

# 飞书文档结构：... 📄 原始素材 / 标题/来源/链接/数据 / 【原文字幕】（共N字） / <transcript>
m = re.search(r'【原文字幕】[^\n]*\n+(.*?)\Z', raw, re.DOTALL)
if m:
    transcript_block = m.group(1).strip()
else:
    print("  WARN: 无法定位'【原文字幕】'段，打印前 800 字供排查：")
    print(raw[:800])
    sys.exit(2)

print(f"  提取的 transcript 长度: {len(transcript_block)} 字")
print(f"  transcript 预览: {transcript_block[:200]}...")

if DRY:
    print("\n[DRY RUN] 不发 POST，退出")
    sys.exit(0)

print(f"\n[2/3] POST /rewrite 到 Mac 服务")
payload = json.dumps({
    "doc_url": doc_url,
    "transcript": transcript_block,
    "chat_id": "",
    "title": TITLE,
}).encode("utf-8")
req_http = urllib.request.Request(
    "http://127.0.0.1:8765/rewrite",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req_http, timeout=10) as r:
    print(f"  HTTP {r.status} {r.read().decode()}")

print("\n[3/3] 已提交。查看 Mac PM2 日志观察 Claude CLI 进度：")
print("  tail -f /Users/zhaoliang/.pm2/logs/ai-media2doc-out.log")
