import os
import sys
from dotenv import load_dotenv

load_dotenv()

# 把项目根目录加入 sys.path，并移除 src/ 目录（Python 运行脚本时自动加入）
# 目的：防止 src/utils/ 与 douyin-downloader 安装包中的 utils/ 产生命名冲突
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src_dir = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
# 移除 src/ 避免与已安装包冲突（项目内用 src.xxx 前缀导入，不依赖 src/ 在 path 中）
while _src_dir in sys.path:
    sys.path.remove(_src_dir)

import threading
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger, P2CardActionTriggerResponse,
)

from src.utils.logger import logger

# 去重：记录已处理的 message_id，防止飞书重试导致重复处理
_processed_message_ids: set = set()
# 待确认重新转写：key = f"{chat_id}:{url}"，value = parsed dict
_pending_reprocess: dict = {}
from src.utils.helper import clean_tmp_file
from src.handlers import message as msg_handler
from src.handlers import feishu_doc, feishu_bitable
from src.processors import downloader, transcriber, rewriter

APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")


def process(parsed: dict):
    """一卡多态主流程：发 1 张状态卡 → 各阶段 PATCH 内容
    阶段：init → qwen_done → all_done（或 failed）
    """
    from src.processors.discover.cron import send_card_and_return_id, patch_card_message

    chat_id = parsed["chat_id"]
    sender_open_id = parsed["sender_open_id"]
    url = parsed.get("url")
    file_key = parsed.get("file_key")
    file_name = parsed.get("file_name")

    audio_path = None
    card_mid = ""   # 主状态卡 message_id（空时第一次 send，后续 PATCH）
    meta = None
    result = None
    doc_url = ""

    def _push(stage: str, **kwargs):
        """发卡或更新卡：第一次 send_card_and_return_id 拿 mid；后续 PATCH 同卡"""
        nonlocal card_mid
        card = msg_handler.build_unified_processing_card(stage=stage, url=url or "", **kwargs)
        if not card_mid:
            card_mid = send_card_and_return_id(chat_id, card)
        else:
            if not patch_card_message(card_mid, card):
                # PATCH 失败兜底发新卡
                msg_handler.send_card(chat_id, card)

    try:
        # ── 状态 1: init ──
        _push("init")

        # Step 1: 下载视频或使用飞书文件
        if url:
            meta = downloader.download(url)
            audio_path = meta.audio_path
        elif file_key:
            local_path = msg_handler.download_feishu_file(
                parsed.get("message_id", ""), file_key, file_name or "video.mp4"
            )
            meta = downloader.download_from_file(local_path)
            audio_path = meta.audio_path
        else:
            return

        if not audio_path:
            _push("failed", error_msg="视频下载失败，请检查链接是否有效")
            return

        # Step 2: ASR 转文字
        logger.info("开始转文字...")
        transcript = transcriber.transcribe(audio_path)
        if not transcript:
            _push("failed", error_msg="语音识别失败，音频可能无人声或格式不支持")
            return

        # Step 3: 2号小助理改写
        logger.info("开始改写文案...")
        result = rewriter.rewrite(transcript)

        # Step 4: 创建飞书文档
        logger.info("创建飞书文档...")
        doc_url = feishu_doc.create_doc(
            title=meta.title or "无标题视频",
            rewrite_result=result,
            meta=meta,
            transcript=transcript,
        )

        # Step 5: 写入多维表格
        logger.info("写入多维表格...")
        feishu_bitable.insert_record(
            meta=meta,
            rewrite_result=result,
            doc_url=doc_url,
            submitter_open_id=sender_open_id,
        )

        # ── 状态 2: qwen_done（含主体内容 + 1号小助理改写中提示）──
        _push("qwen_done", meta=meta, rewrite_result=result, doc_url=doc_url)
        logger.info(f"处理完成：{meta.title}")

        # Step 7: 1号小助理二次改写（异步）
        if doc_url:
            claude_server_url = os.getenv("CLAUDE_SERVER_URL", "")
            enable_cli = os.getenv("ENABLE_CLAUDE_CLI", "true").lower() == "true"
            logger.info(f"Step7: enable_cli={enable_cli}, server_url={bool(claude_server_url)}")

            if enable_cli:
                def _claude_rewrite(card_mid=card_mid, doc_url=doc_url, transcript=transcript,
                                    meta=meta, result=result, chat_id=chat_id, url=url):
                    try:
                        logger.info("开始 1号小助理二次改写...")
                        claude_result = rewriter.rewrite_with_claude_cli(transcript)
                        feishu_doc.append_claude_rewrite(doc_url, claude_result)
                        # ── 状态 3: all_done ──
                        if card_mid:
                            final_card = msg_handler.build_unified_processing_card(
                                stage="all_done", url=url or "",
                                meta=meta, rewrite_result=result,
                                doc_url=doc_url, claude_done=True,
                            )
                            patch_card_message(card_mid, final_card)
                    except Exception as e:
                        logger.error(f"1号小助理改写失败：{e}")
                        if card_mid:
                            fail_card = msg_handler.build_unified_processing_card(
                                stage="failed", url=url or "",
                                meta=meta, rewrite_result=result,
                                doc_url=doc_url,
                                error_msg=f"1号小助理改写失败：{str(e)[:100]}",
                            )
                            patch_card_message(card_mid, fail_card)
                threading.Thread(target=_claude_rewrite, daemon=True).start()

            elif claude_server_url:
                def _notify_claude_server(chat_id=chat_id, doc_url=doc_url,
                                          transcript=transcript, title=meta.title or ""):
                    try:
                        import urllib.request as ureq
                        import json as _json
                        payload = _json.dumps({
                            "doc_url": doc_url, "transcript": transcript,
                            "chat_id": chat_id, "title": title
                        }).encode("utf-8")
                        req = ureq.Request(
                            f"{claude_server_url}/rewrite",
                            data=payload,
                            headers={"Content-Type": "application/json"},
                            method="POST"
                        )
                        with ureq.urlopen(req, timeout=5) as resp:
                            logger.info(f"已通知 1号小助理服务：{resp.status}")
                    except Exception as e:
                        logger.error(f"通知 1号小助理服务失败：{e}")
                threading.Thread(target=_notify_claude_server, daemon=True).start()

    except Exception as e:
        logger.exception(f"处理失败：{e}")
        try:
            _push("failed", meta=meta, rewrite_result=result, doc_url=doc_url,
                  error_msg=str(e)[:200])
        except Exception:
            pass

    finally:
        if audio_path:
            clean_tmp_file(audio_path)


def on_message(data: P2ImMessageReceiveV1):
    try:
        message = data.event.message
        sender = data.event.sender

        # 去重：同一消息只处理一次
        if message.message_id in _processed_message_ids:
            return
        _processed_message_ids.add(message.message_id)

        parsed = {
            "chat_id": message.chat_id,
            "message_id": message.message_id,
            "sender_open_id": sender.sender_id.open_id if sender.sender_id else "",
            "msg_type": message.message_type,
            "url": None,
            "file_key": None,
            "file_name": None,
        }

        import json
        content = json.loads(message.content) if message.content else {}

        if message.message_type == "text":
            from src.utils.helper import extract_url, is_douyin_url, is_xiaohongshu_url
            text = content.get("text", "")

            # /发现 <关键词> 命令拦截（discover 模块即时搜索）
            try:
                from src.processors.discover.command import handle_discover_command
                if handle_discover_command(text, message.chat_id):
                    return
            except Exception as _e:
                logger.error(f"[discover.command] handler 异常: {_e}")

            # 用户回复"重新转写"，触发待确认任务
            if "重新转写" in text and message.chat_id in _pending_reprocess:
                pending_parsed = _pending_reprocess.pop(message.chat_id)
                msg_handler.send_text(message.chat_id, "🔄 已确认，开始重新转写...")
                threading.Thread(target=process, args=(pending_parsed,), daemon=True).start()
                return

            url = extract_url(text)
            if url and (is_douyin_url(url) or is_xiaohongshu_url(url)):
                parsed["url"] = url
                # 查重：抖音用 aweme_id 规范化（短链每次不同），小红书用原 url
                if is_douyin_url(url):
                    from src.processors.douyin_downloader import _extract_aweme_id
                    aweme_id = _extract_aweme_id(url)
                    search_url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else url
                else:
                    search_url = url
                existing = feishu_bitable.find_by_url(search_url)
                # 兼容旧记录（旧记录存短链），没找到则再用原始 URL 搜一次
                if not existing and search_url != url:
                    existing = feishu_bitable.find_by_url(url)
                if existing:
                    _pending_reprocess[message.chat_id] = parsed
                    card = msg_handler.build_duplicate_card(existing, url)
                    msg_handler.send_card(message.chat_id, card)
                    return
            else:
                # 不是抖音链接，忽略
                return

        elif message.message_type in ("file", "audio", "media"):
            parsed["file_key"] = content.get("file_key", "")
            parsed["file_name"] = content.get("file_name", "video.mp4")

        else:
            return

        # 异步处理，立刻返回响应给飞书
        threading.Thread(target=process, args=(parsed,), daemon=True).start()

    except Exception as e:
        logger.exception(f"消息解析失败：{e}")

    return



def _disable_clicked_trending_button(original_card: dict, clicked_word: str) -> dict:
    """重建原热搜卡：被点的按钮**原位变状态**——
      - 文字：✍️ 写这条脚本  →  ✅ 已选择该热点写稿
      - 颜色：primary（蓝）→  default（灰）
      - value：清空 action，避免重复触发

    其他词条按钮 + 整张卡其他元素原样保留。
    """
    elements = original_card.get("elements", []) or []
    new_elements = []
    for el in elements:
        if el.get("tag") == "action":
            new_actions = []
            for btn in (el.get("actions") or []):
                val = btn.get("value") or {}
                if (val.get("word") == clicked_word
                        and val.get("action") == "write_short_video_script"):
                    # 被点的那个按钮 → 原位换状态
                    new_actions.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ 已选择该热点写稿"},
                        "type": "default",
                        "value": {"action": "noop"},
                    })
                else:
                    # 其他按钮原样保留
                    new_actions.append(btn)
            new_elements.append({**el, "actions": new_actions})
        else:
            new_elements.append(el)
    return {**original_card, "elements": new_elements}


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """飞书卡片按钮回调路由器。

    路由：根据 event.action.value.action 字段分发到对应 handler。
    返回：toast（即时弹窗） + card.type=raw.data=更新后的原卡（被点按钮变灰文字）。
    """
    try:
        action_payload = data.event.action.value or {}
        action_type = action_payload.get("action", "")
        ctx = data.event.context
        chat_id = ctx.open_chat_id if ctx else ""
        original_message_id = ctx.open_message_id if ctx else ""
        operator = data.event.operator.open_id if data.event.operator else ""
        logger.info(f"[on_card_action] action={action_type} chat={chat_id} "
                    f"original_mid={original_message_id} operator={operator}")

        if action_type == "write_short_video_script":
            word = action_payload.get("word", "")
            hot = action_payload.get("hot", 0)
            if not word or not chat_id:
                return P2CardActionTriggerResponse({
                    "toast": {"type": "error", "content": "参数缺失（word/chat_id）"},
                })

            # 异步起线程跑 LLM + 写文档 + PATCH 灰卡为绿卡
            from src.processors.discover.script_writer import process as run_script_writer
            threading.Thread(
                target=run_script_writer,
                args=(word, int(hot or 0), chat_id),
                daemon=True,
            ).start()

            # 构造响应：toast + 修改后的原热搜卡（被点词条按钮变灰文字）
            response_body = {
                "toast": {
                    "type": "info",
                    "content": f"✍️ 已开始基于「{word[:20]}」写脚本，约 60s 后群里出文档",
                },
            }
            if original_message_id:
                try:
                    from src.processors.discover.cron import load_trending_card_cache
                    original_card = load_trending_card_cache(original_message_id)
                    if original_card:
                        updated_card = _disable_clicked_trending_button(original_card, word)
                        response_body["card"] = {"type": "raw", "data": updated_card}
                    else:
                        logger.warning(f"[on_card_action] 缓存未命中 mid={original_message_id}（可能 cron 旧版未缓存）")
                except Exception as _e:
                    logger.warning(f"[on_card_action] 重建原卡失败（不阻塞）：{_e}")
            return P2CardActionTriggerResponse(response_body)

        # 未识别 action 类型
        logger.warning(f"[on_card_action] 未识别 action_type: {action_type!r}")
        return P2CardActionTriggerResponse({
            "toast": {"type": "warning", "content": f"未知动作：{action_type}"},
        })

    except Exception as e:
        logger.exception(f"[on_card_action] 异常：{e}")
        return P2CardActionTriggerResponse({
            "toast": {"type": "error", "content": f"处理失败：{str(e)[:80]}"},
        })


def _is_placeholder(v: str) -> bool:
    """空 / 占位值识别：.env 没填时 .env.example 标准占位是 cli_xxxxxxxxxxxxxxxxx 等"""
    if not v:
        return True
    v = v.strip()
    if not v:
        return True
    if v.startswith("<"):
        return True
    if "xxxxxxxxxxxxxxxx" in v:
        return True
    return False


def main():
    logger.info("启动 AI-Media2Doc 服务...")
    logger.info(f"飞书 App ID：{APP_ID}")

    # graceful fallback：占位 / 空凭证不进 ws.start（避免死循环 pm2 unstable restart）
    # 朋友填好 .env 后 `pm2 restart ai-media2doc-main` 立即激活
    if _is_placeholder(APP_ID) or _is_placeholder(APP_SECRET):
        logger.warning("=" * 60)
        logger.warning("FEISHU_APP_ID / FEISHU_APP_SECRET 未填（.env 占位值）")
        logger.warning("Daemon 保持 online 但不连飞书 WebSocket")
        logger.warning("填好 .env 后: pm2 restart ai-media2doc-main 即激活")
        logger.warning("=" * 60)
        import time
        while True:
            time.sleep(60)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )

    logger.info("WebSocket 长连接启动，等待飞书消息...")
    ws_client.start()


if __name__ == "__main__":
    main()
