"""基于热搜词调 Claude Sonnet 生成 60s 短视频脚本，写飞书文档，回卡片。

入口：process(trending_word, hot_value, chat_id) - 由 main.on_card_action 在线程内调用。

流程：
  1. _build_prompt：读 prompts.yaml short_video_script_template，填占位符
  2. generate_script：调 Claude API，解析 JSON 返回 ShortVideoScriptResult
  3. write_script_doc：创建飞书文档，写入脚本/标题/拍摄建议
  4. build_script_done_card → notify_feishu：群里回卡片+文档链接

新增于 2026-05-16（热点 → 脚本 闭环 v1）
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from typing import Optional

from src.utils.logger import logger


@dataclass
class ShortVideoScriptResult:
    titles: list = field(default_factory=list)
    # script 段
    hook: str = ""
    hook_reason: str = ""
    contrast: str = ""
    body_1: str = ""
    body_2: str = ""
    body_3: str = ""
    cta: str = ""
    # shooting tips
    shooting_format: str = ""
    subtitles_focus: str = ""
    music: str = ""
    shots: str = ""
    # 其他
    hashtags: list = field(default_factory=list)
    audience: str = ""
    summary: str = ""


def _build_prompt(trending_word: str, hot_value: int) -> str:
    from src.processors.rewriter import _load_prompts
    prompts = _load_prompts()
    template = prompts.get("short_video_script_template", "")
    if not template:
        raise RuntimeError("prompts.yaml 缺少 short_video_script_template")
    return template.format(trending_word=trending_word, hot_value=hot_value)


def _parse_script_result(raw: str) -> ShortVideoScriptResult:
    """容错解析 Claude 返回的 JSON（复用 rewriter._fix_json + json_repair 兜底）"""
    from src.processors.rewriter import _fix_json
    clean = raw.strip()
    # 去 markdown code block
    if "```json" in clean:
        clean = clean.split("```json")[1].split("```")[0]
    elif "```" in clean:
        clean = clean.split("```")[1].split("```")[0]
    else:
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start:end + 1]
    clean = _fix_json(clean.strip())
    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            data = json.loads(repair_json(clean))
        except Exception as e:
            logger.error(f"[script_writer] JSON 解析失败：{e}\n原始：{raw[:500]}")
            return ShortVideoScriptResult(summary=raw[:100])

    script = data.get("script", {}) or {}
    tips = data.get("shooting_tips", {}) or {}
    return ShortVideoScriptResult(
        titles=data.get("titles", []) or [],
        hook=script.get("hook", ""),
        hook_reason=script.get("hook_reason", ""),
        contrast=script.get("contrast", ""),
        body_1=script.get("body_1", ""),
        body_2=script.get("body_2", ""),
        body_3=script.get("body_3", ""),
        cta=script.get("cta", ""),
        shooting_format=tips.get("format", ""),
        subtitles_focus=tips.get("subtitles_focus", ""),
        music=tips.get("music", ""),
        shots=tips.get("shots", ""),
        hashtags=data.get("hashtags", []) or [],
        audience=data.get("audience", ""),
        summary=data.get("summary", ""),
    )


def _strip_ansi(text: str) -> str:
    """去掉 Claude CLI 输出里的 ANSI 控制字符"""
    import re
    return re.sub(r'\x1b\[[0-9;]*[mGKHF]|\x1b\].*?\x07|\r', '', text)


def _generate_via_cli(prompt: str, trending_word: str) -> str:
    """走 claude CLI（用 Pro 订阅，零 API 余额消耗）"""
    import subprocess
    logger.info(f"[script_writer] 通过 Claude CLI 生成脚本：{trending_word}")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["NO_COLOR"] = "1"
    result = subprocess.run(
        ["claude", "--output-format", "text", "-p", prompt],
        capture_output=True, text=True, timeout=180, env=env,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI 调用失败：{result.stderr[:200]}")
    return _strip_ansi(result.stdout)


def _generate_via_api(prompt: str, trending_word: str) -> str:
    """走 Anthropic API（需 CLAUDE_API_KEY + 账户余额）"""
    import anthropic
    api_key = os.getenv("CLAUDE_API_KEY")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY 未配置")
    client = anthropic.Anthropic(api_key=api_key)
    # 显式默认 sonnet-4-5（不继承 rewriter 的 CLAUDE_MODEL=opus-4-5）
    model = os.getenv("SHORT_VIDEO_SCRIPT_MODEL", "claude-sonnet-4-5")
    logger.info(f"[script_writer] 通过 Claude API {model} 生成脚本：{trending_word}")
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def generate_script(trending_word: str, hot_value: int) -> ShortVideoScriptResult:
    """生成 60s 短视频脚本。

    Provider 路由：SHORT_VIDEO_SCRIPT_PROVIDER env
      - cli (默认)：claude CLI 调用 Pro 订阅，零 API 消耗
      - api：Anthropic API（需 CLAUDE_API_KEY + 账户余额）
    """
    prompt = _build_prompt(trending_word, hot_value)
    provider = os.getenv("SHORT_VIDEO_SCRIPT_PROVIDER", "cli").lower()
    if provider == "api":
        raw = _generate_via_api(prompt, trending_word)
    else:
        raw = _generate_via_cli(prompt, trending_word)
    return _parse_script_result(raw)


def write_script_doc(script: ShortVideoScriptResult, trending_word: str, hot_value: int) -> str:
    """写飞书文档，返回 doc_url。"""
    from lark_oapi.api.docx.v1 import (
        CreateDocumentRequest, CreateDocumentRequestBody,
        CreateDocumentBlockChildrenRequest, CreateDocumentBlockChildrenRequestBody,
    )
    from src.handlers.feishu_client import client
    from src.handlers.feishu_doc import (
        _get_or_create_month_folder, _heading2, _paragraph, _safe_paragraphs, _divider,
    )

    folder_token = _get_or_create_month_folder()
    title = f"[短视频脚本] {trending_word[:40]}"

    req = CreateDocumentRequest.builder().request_body(
        CreateDocumentRequestBody.builder()
            .title(title)
            .folder_token(folder_token)
            .build()
    ).build()
    resp = client.docx.v1.document.create(req)
    if not resp.success():
        logger.error(f"[script_writer] 创建脚本文档失败：{resp.msg}")
        return ""
    doc_id = resp.data.document.document_id
    doc_url = f"https://feishu.cn/docx/{doc_id}"

    # ── 拼 blocks ──
    blocks = [
        _paragraph(f"🔥 热搜词：{trending_word}", bold=True),
        _paragraph(f"📊 平台热度：{hot_value:,}"),
    ]
    if script.summary:
        blocks.append(_paragraph(f"💡 卖点：{script.summary}"))
    blocks.append(_divider())
    blocks.append(_heading2("✍️ 短视频脚本（60 秒口播）"))

    sections = [
        ("【0-3s】开头钩子", script.hook, script.hook_reason),
        ("【3-15s】反差/案例", script.contrast, ""),
        ("【15-30s】观点一", script.body_1, ""),
        ("【30-40s】观点二", script.body_2, ""),
        ("【40-50s】观点三", script.body_3, ""),
        ("【50-60s】CTA", script.cta, ""),
    ]
    for header, content, analysis in sections:
        if not content:
            continue
        blocks.append(_paragraph(header, bold=True))
        blocks.extend(_safe_paragraphs(f"  {content}"))
        if analysis:
            blocks.extend(_safe_paragraphs(f"  💡 分析：{analysis}"))
        blocks.append(_paragraph(""))

    if script.titles:
        blocks.append(_divider())
        blocks.append(_heading2("🎯 改写标题备选"))
        title_labels = ["疑问式", "数字式", "共鸣式", "争议式", "数据式", "悬念式"]
        for i, t in enumerate(script.titles):
            label = title_labels[i] if i < len(title_labels) else f"备选{i+1}"
            blocks.extend(_safe_paragraphs(f"  {label}：{t}"))
        blocks.append(_paragraph(""))

    if any([script.shooting_format, script.subtitles_focus, script.music, script.shots]):
        blocks.append(_divider())
        blocks.append(_heading2("🎬 拍摄建议"))
        if script.shooting_format:
            blocks.append(_paragraph("【形式】", bold=True))
            blocks.extend(_safe_paragraphs(f"  {script.shooting_format}"))
        if script.subtitles_focus:
            blocks.append(_paragraph("【字幕重点】", bold=True))
            blocks.extend(_safe_paragraphs(f"  {script.subtitles_focus}"))
        if script.music:
            blocks.append(_paragraph("【配乐】", bold=True))
            blocks.extend(_safe_paragraphs(f"  {script.music}"))
        if script.shots:
            blocks.append(_paragraph("【机位/镜头】", bold=True))
            blocks.extend(_safe_paragraphs(f"  {script.shots}"))
        blocks.append(_paragraph(""))

    if script.audience:
        blocks.append(_divider())
        blocks.append(_heading2("👥 目标人群"))
        blocks.extend(_safe_paragraphs(script.audience))
        blocks.append(_paragraph(""))

    if script.hashtags:
        blocks.append(_paragraph("【话题标签】", bold=True))
        tags = " ".join([f"#{t}" for t in script.hashtags])
        blocks.extend(_safe_paragraphs(tags))

    # 飞书单次 50 block 限制
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
            logger.error(f"[script_writer] 脚本文档写入失败：{patch_resp.msg}")
            return doc_url  # 返已创建的 URL，至少打开能看到部分
    logger.info(f"[script_writer] 脚本文档创建成功：{doc_url}")
    return doc_url


def build_script_writing_card(trending_word: str, hot_value: int) -> dict:
    """脚本写作中过渡卡（灰色 header）— 点按钮后立刻发，告诉用户「正在写」。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⏳ 短视频脚本写作中..."},
            "subtitle": {"tag": "plain_text", "content": "正在拆解热点 · 设计钩子 · 生成 60s 口播脚本"},
            "template": "grey",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**🔥 热点：** {trending_word}\n**📊 热度：** {hot_value:,}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": "**🤖 当前阶段：**\n・拆解热点 → 设计钩子 → 写脚本 → 拍摄建议\n\n**⏱ 预计完成：** 约 60 秒\n\n_完成后会在群里发「✅ 已生成」卡片 + 飞书文档链接_"}},
        ],
    }


def build_script_done_card(trending_word: str, hot_value: int,
                            script: ShortVideoScriptResult, doc_url: str) -> dict:
    """脚本生成完成卡（绿色 header）"""
    title_preview = "\n".join([f"・{t}" for t in script.titles[:3]]) if script.titles else "（标题缺失）"
    subtitle = script.summary or f"基于「{trending_word[:30]}」"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "✅ 短视频脚本已生成"},
            "subtitle": {"tag": "plain_text", "content": subtitle[:80]},
            "template": "green",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**🔥 热点：** {trending_word}\n**📊 热度：** {hot_value:,}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**✍️ 钩子预览（0-3s）：**\n{script.hook}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**🎯 标题备选 TOP3：**\n{title_preview}"}},
            {"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📄 查看完整脚本"},
                "type": "primary",
                "url": doc_url,
            }]},
        ],
    }


def build_script_failed_card(trending_word: str, error_msg: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "❌ 脚本生成失败"},
            "template": "red",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**热点：** {trending_word}\n\n**原因：** {error_msg}"}},
        ],
    }


def process(trending_word: str, hot_value: int, chat_id: str) -> Optional[str]:
    """主入口：调 LLM → 写飞书文档 → 发卡。返回 doc_url 或 None。

    由 main.on_card_action 在 threading.Thread 内调用，故所有异常都吞掉，发失败卡。
    """
    from src.processors.discover.cron import (
        notify_feishu, send_card_and_return_id, patch_card_message,
    )
    logger.info(f"[script_writer.process] 启动 word={trending_word!r} hot={hot_value}")

    # Step 1: 发灰色「⏳ 写作中」卡 + 记 message_id（用于 60s 后 PATCH 同卡）
    writing_message_id = ""
    try:
        writing_card = build_script_writing_card(trending_word, hot_value)
        writing_message_id = send_card_and_return_id(chat_id, writing_card)
        if not writing_message_id:
            logger.warning("[script_writer.process] 发写作中卡未拿到 message_id，将降级为发新卡模式")
    except Exception as _e:
        logger.warning(f"[script_writer.process] 发写作中卡异常（不阻塞主流程）：{_e}")

    def _deliver(final_card: dict):
        """成功 / 失败统一交付：优先 PATCH 同卡；失败/无 id 时降级发新卡。"""
        if writing_message_id:
            if patch_card_message(writing_message_id, final_card):
                return
            logger.warning(f"[script_writer.process] PATCH 卡失败，降级发新卡 mid={writing_message_id}")
        try:
            notify_feishu(chat_id, card=final_card)
        except Exception as _e:
            logger.error(f"[script_writer.process] 降级发新卡也失败：{_e}")

    try:
        script = generate_script(trending_word, hot_value)
        if not script.hook:
            raise RuntimeError("Claude 返回脚本缺少钩子，可能 JSON 解析失败")
        doc_url = write_script_doc(script, trending_word, hot_value)
        if not doc_url:
            raise RuntimeError("飞书文档创建失败")
        _deliver(build_script_done_card(trending_word, hot_value, script, doc_url))
        logger.info(f"[script_writer.process] 完成 doc={doc_url}")
        return doc_url
    except Exception as e:
        logger.exception(f"[script_writer.process] 失败：{e}")
        try:
            _deliver(build_script_failed_card(trending_word, str(e)[:200]))
        except Exception:
            pass
        return None
