"""
Mac 本地 Claude 改写服务
NAS 完成主流程后，通过 HTTP POST 触发本机 Claude CLI 二次改写
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_src_dir = os.path.join(_project_root, "src")
while _src_dir in sys.path:
    sys.path.remove(_src_dir)

from flask import Flask, request, jsonify
from src.processors import rewriter
from src.handlers import feishu_doc
from src.handlers import message as msg_handler
from src.utils.logger import logger

app = Flask(__name__)


@app.route("/rewrite", methods=["POST"])
def rewrite():
    data = request.json or {}
    doc_url = data.get("doc_url", "")
    transcript = data.get("transcript", "")
    chat_id = data.get("chat_id", "")
    title = data.get("title", "")

    if not doc_url or not transcript:
        return jsonify({"error": "缺少 doc_url 或 transcript"}), 400

    def _run():
        try:
            logger.info(f"收到 Claude 改写请求：{title}")
            if chat_id:
                msg_handler.send_text(chat_id, "一号机器人开始改写文案 ✍️")
            claude_result = rewriter.rewrite_with_claude_cli(transcript)
            feishu_doc.append_claude_rewrite(doc_url, claude_result)
            if chat_id:
                msg_handler.send_text(
                    chat_id,
                    f"🤖 1号小助理改写版已完成，已插入文档顶部\n{doc_url}"
                )
            logger.info(f"Claude 改写完成：{title}")
        except Exception as e:
            logger.error(f"Claude 改写失败：{e}")

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "accepted"}), 202


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


def _kill_port(port):
    """启动前清理残留进程，避免 Address already in use 循环重启"""
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5
        )
        for pid in result.stdout.strip().split("\n"):
            pid = pid.strip()
            if pid and pid != str(os.getpid()):
                os.kill(int(pid), 9)
                logger.info(f"清理残留进程 PID={pid}（端口 {port}）")
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.getenv("CLAUDE_SERVER_PORT", "8765"))
    _kill_port(port)
    logger.info(f"Claude 改写服务启动，监听端口 {port}")
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", port, app, use_reloader=False, passthrough_errors=True)
