import os
import json
from lark_oapi.api.im.v1 import *
from src.utils.logger import logger
from src.handlers.feishu_client import client


def send_text(chat_id: str, text: str):
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(CreateMessageRequestBody.builder()
                      .receive_id(chat_id)
                      .msg_type("text")
                      .content(json.dumps({"text": text}))
                      .build()) \
        .build()
    resp = client.im.v1.message.create(req)
    if not resp.success():
        logger.error(f"发送文本消息失败：{resp.msg}")


def send_card(chat_id: str, card: dict):
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(CreateMessageRequestBody.builder()
                      .receive_id(chat_id)
                      .msg_type("interactive")
                      .content(json.dumps(card))
                      .build()) \
        .build()
    resp = client.im.v1.message.create(req)
    if not resp.success():
        logger.error(f"发送卡片消息失败：{resp.msg}")


def build_success_card(meta, rewrite_result, doc_url: str) -> dict:
    from src.utils.helper import format_number
    titles_text = "\n".join([f"・{t}" for t in rewrite_result.titles])
    stats = []
    if meta.like_count is not None:
        stats.append(f"点赞 {format_number(meta.like_count)}")
    if meta.collect_count is not None:
        stats.append(f"收藏 {format_number(meta.collect_count)}")
    stats_text = "  ".join(stats) if stats else "数据获取中"

    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**🎬 视频解析完成**\n\n**标题：** {meta.title}\n**数据：** {stats_text}"
                }
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**✍️ 改写标题备选：**\n{titles_text}"
                }
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📝 运营文案（前100字）：**\n{rewrite_result.content[:100]}..."
                }
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看完整改写文档"},
                        "type": "primary",
                        "url": doc_url
                    }
                ]
            }
        ]
    }


def build_unified_processing_card(
    stage: str,
    url: str = "",
    meta=None,
    rewrite_result=None,
    doc_url: str = "",
    claude_done: bool = False,
    error_msg: str = "",
) -> dict:
    """一卡多态总函数：根据 stage 渲染 4 个状态

    stage:
      "init"        → ⏳ 已收到链接处理中（灰色）
      "qwen_done"   → 🎬 视频解析完成 + 2号小助理改写已生成（蓝色，含主体）
      "all_done"   → ✅ 双版本改写完成 + 1号小助理已插文档顶部（绿色）
      "failed"     → ❌ 处理失败（红色，含错误信息）

    主体内容（标题/数据/改写）只在 qwen_done / all_done 阶段填充；
    步骤指示器按 stage 切换；按钮在 qwen_done / all_done 阶段才有。

    新增于 2026-05-17（一卡多态合并方案，替代旧 success_card + send_text 进度提示）。
    """
    from src.utils.helper import format_number

    # ── header 标题 + 颜色 ──
    header_map = {
        "init":      ("⏳ 已收到链接，处理中...", "短视频解析 + 改写",  "grey"),
        "qwen_done": ("🎬 视频解析完成",         "2号小助理改写已生成",  "blue"),
        "all_done":  ("✅ 双版本改写完成",       "1号 + 2号小助理双版本已生成", "green"),
        "failed":    ("❌ 处理失败",             "已停在某个阶段",       "red"),
    }
    title_text, subtitle_text, template = header_map.get(stage, header_map["init"])
    header = {
        "title": {"tag": "plain_text", "content": title_text},
        "subtitle": {"tag": "plain_text", "content": subtitle_text},
        "template": template,
    }

    elements = []

    # ── 主体内容（qwen_done / all_done 阶段才有标题+数据+改写）──
    if stage in ("qwen_done", "all_done") and meta and rewrite_result:
        stats = []
        if meta.like_count is not None:
            stats.append(f"👍 {format_number(meta.like_count)}")
        if meta.collect_count is not None:
            stats.append(f"⭐ {format_number(meta.collect_count)}")
        stats_text = "  ".join(stats) if stats else "数据获取中"

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**标题：** {meta.title}\n**数据：** {stats_text}"},
        })
        elements.append({"tag": "hr"})

        if rewrite_result.titles:
            titles_text = "\n".join([f"・{t}" for t in rewrite_result.titles[:3]])
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**✍️ 改写标题备选：**\n{titles_text}"},
            })
            elements.append({"tag": "hr"})

        # 爆款评论（引导互动）— 替代原"运营文案前100字"
        comments_list = getattr(rewrite_result, "comments", None) or []
        if comments_list:
            comments_text = "\n".join([f"・{c}" for c in comments_list[:3]])
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**💬 爆款评论（引导互动）：**\n{comments_text}"},
            })
            elements.append({"tag": "hr"})
        elif rewrite_result.content:
            # fallback：没拿到 comments 时退回运营文案预览
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**📝 运营文案（前 100 字）：**\n{rewrite_result.content[:100]}..."},
            })
            elements.append({"tag": "hr"})

    # ── init 阶段显示原链接 ──
    if stage == "init" and url:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"🔗 {url[:120]}"},
        })
        elements.append({"tag": "hr"})

    # ── failed 阶段显示原链接 ──
    if stage == "failed" and url:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"🔗 原链接：{url[:120]}"},
        })

    # ── 步骤指示器（4 步骤）──
    step_states = {
        "init":      ["🟢", "⚪", "⚪", "⚪"],
        "qwen_done": ["✅", "✅", "✅", "🟢"],
        "all_done":  ["✅", "✅", "✅", "✅"],
    }
    step_labels = ["下载视频", "转写文字", "2号小助理改写", "1号小助理改写"]
    states = step_states.get(stage, step_states["init"])

    if stage == "failed":
        err = (error_msg or "").lower()
        if "下载" in error_msg or "download" in err:
            states = ["❌", "—", "—", "—"]
        elif "asr" in err or "转写" in error_msg or "识别" in error_msg:
            states = ["✅", "❌", "—", "—"]
        elif "2号" in error_msg or "改写文案" in error_msg:
            states = ["✅", "✅", "❌", "—"]
        else:
            states = ["✅", "✅", "✅", "❌"]

    step_lines = [f"・{s} {label}" for s, label in zip(states, step_labels)]
    if stage == "all_done":
        step_lines[-1] += "  ⭐ 已插文档顶部"
    elif stage == "qwen_done" and not claude_done:
        step_lines[-1] += "（约 60s）"

    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md",
                 "content": "**当前阶段：**\n" + "\n".join(step_lines)},
    })

    if stage == "init":
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "⏱ 预计完成约 1-3 分钟"},
        })

    if stage == "failed" and error_msg:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**错误原因：** {error_msg[:200]}"},
        })

    # ── 文档按钮 ──
    if stage in ("qwen_done", "all_done") and doc_url:
        button_text = "📄 查看完整改写文档（含双版本）" if stage == "all_done" else "📄 查看完整改写文档"
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": button_text},
                "type": "primary",
                "url": doc_url,
            }],
        })

    return {
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": elements,
    }


def build_duplicate_card(existing: dict, url: str) -> dict:
    from datetime import datetime
    submit_time = existing.get("submit_time", 0)
    dt = datetime.fromtimestamp(submit_time / 1000).strftime("%Y-%m-%d %H:%M") if submit_time else "未知时间"
    doc_url = existing.get("doc_url", "")
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**⚠️ 该视频已处理过**\n\n"
                    f"**标题：** {existing.get('title', '未知')}\n"
                    f"**处理时间：** {dt}\n\n"
                    f"如需重新转写，请回复：**重新转写**"
                )
            }
        },
        {"tag": "hr"},
    ]
    if doc_url:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看已有文档"},
                "type": "primary",
                "url": doc_url,
            }]
        })
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def build_error_card(url: str, error_msg: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**❌ 处理失败**\n\n链接：{url}\n\n原因：{error_msg}"
                }
            }
        ]
    }



# ─────────────────────────────────────────────────────────
# discover 模块卡片（2026-05-16 新增）
# ─────────────────────────────────────────────────────────

def _format_wan(n) -> str:
    """整数美化：1.2 亿 / 873 万 / 1568"""
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n)
    if n >= 100_000_000:
        return f"{n/1e8:.1f} 亿"
    if n >= 10_000:
        return f"{n/1e4:.0f} 万"
    return f"{n}"


def build_xhs_alert_card(platform: str, context: str, reason: str,
                         action_cmd: str = "", candidate_table_url: str = "") -> dict:
    """小红书/抖音 发现 0 条告警卡（红色 header）"""
    platform_label = "小红书" if platform in ("xhs", "xiaohongshu") else "抖音"
    content_lines = [f"**🔍 上下文**\n{context}"]
    if reason:
        content_lines.append(f"**🛠 最可能原因**\n{reason}")
    if action_cmd:
        content_lines.append(f"**✅ 解决方案**\n`{action_cmd}`")
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(content_lines)}},
        {"tag": "note", "elements": [
            {"tag": "plain_text", "content": "⏰ 6 小时内不重复告警"}
        ]},
    ]
    if candidate_table_url:
        elements.append({"tag": "action", "actions": [
            {"tag": "button",
             "text": {"tag": "plain_text", "content": "📋 查看候选清单"},
             "type": "default",
             "url": candidate_table_url},
        ]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🚨 {platform_label} 发现 0 条"},
            "template": "red",
        },
        "elements": elements,
    }


def build_trending_card(platform_label: str, words: list, timestamp: str = "") -> dict:
    """抖音热搜词情报卡（橙色 header）。

    words 支持两种格式（向后兼容）：
      - list[(title:str, hot_value:int)]                       # 旧 2-tuple
      - list[(title:str, hot_value:int, is_biz_relevant:bool)] # 新 3-tuple，业务相关高亮

    渲染：
      - 业务相关（is_biz=True）单独分组 + ⭐ 前缀 + 排最前
      - TOP3 总榜（按热度）次之
      - 其余按热度排序
    """
    if not words:
        return None
    # 规范化为 3-tuple
    norm = []
    for w in words:
        if len(w) == 3:
            norm.append((w[0], w[1], bool(w[2])))
        elif len(w) == 2:
            norm.append((w[0], w[1], False))
    if not norm:
        return None

    elements = []
    biz = [(t, h) for t, h, b in norm if b]
    # 业务相关分组优先 — 每条单独 div + 一行 ✍️ 写脚本按钮（触发 card_action callback）
    if biz:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**🎯 业务相关（{len(biz)} 条）** — 点按钮基于该热点写 60s 短视频脚本"},
        })
        for title, hot in biz[:8]:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"⭐ **{title}**  ·  🔥 {_format_wan(hot)}"},
            })
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✍️ 写这条脚本"},
                    "type": "primary",
                    "value": {
                        "action": "write_short_video_script",
                        "word": title,
                        "hot": int(hot) if hot else 0,
                    },
                }],
            })
        elements.append({"tag": "hr"})

    # TOP3 总榜
    medals = ["🥇", "🥈", "🥉"]
    top3 = []
    for i, (title, hot, _) in enumerate(norm[:3]):
        top3.append(f"{medals[i]} **{title}**  ·  🔥 {_format_wan(hot)}")
    if top3:
        section = "**🔥 热度 TOP3**\n" + "\n".join(top3) if biz else "\n".join(top3)
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": section}})

    # 其余
    rest = []
    for i, (title, hot, b) in enumerate(norm[3:30], start=4):
        prefix = "⭐ " if b else ""
        rest.append(f"{i:02d}. {prefix}{title}  ·  {_format_wan(hot)}")
    if rest:
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rest)}},
        ]

    try:
        total_hot = sum(int(h) for _, h, _ in norm)
    except (ValueError, TypeError):
        total_hot = 0
    avg_hot = total_hot // max(len(norm), 1)
    biz_note = f" · 业务相关 ⭐ {len(biz)} 条" if biz else ""
    elements += [
        {"tag": "hr"},
        {"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": f"📊 总热度 {_format_wan(total_hot)}  ·  平均 {_format_wan(avg_hot)}  ·  共 {len(norm)} 条{biz_note}"},
        ]},
    ]
    subtitle = f"{timestamp} · 情报参考" if timestamp else "情报参考·不入候选清单"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔥 {platform_label}今日热搜词"},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": "orange",
        },
        "elements": elements,
    }


def build_cron_done_card(trigger: str, stats: dict, duration_sec: float = 0,
                        candidate_table_url: str = "") -> dict:
    """cron / watcher / command 完成卡（绿色有产出 / 灰色无产出）"""
    inp = stats.get("input", 0)
    passed = stats.get("passed", 0)
    cand = stats.get("candidate", 0)
    auto = stats.get("auto_rewrite", 0)
    dup = stats.get("duplicate", 0)
    drop = stats.get("drop", 0)
    le = stats.get("limit_exceeded", 0)
    new_total = cand + auto

    funnel = f"输入 **{inp}** → 通过 **{passed}** → 入库 **{new_total}**"
    rows = [
        f"📝 **候选清单**     {cand} 条",
        f"🤖 **自动改写**     {auto} 条" + ("  ⭐⭐⭐⭐⭐" if auto > 0 else ""),
        f"🔁 **去重跳过**     {dup} 条",
        f"🚫 **分数过低**     {drop} 条",
    ]
    if le > 0:
        rows.append(f"🚧 **限流跳过**     {le} 条")

    subtitle = ""
    if duration_sec > 0:
        if duration_sec < 60:
            subtitle = f"耗时 {duration_sec:.0f}s"
        else:
            subtitle = f"耗时 {int(duration_sec//60)}m {int(duration_sec%60)}s"

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": funnel}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(rows)}},
    ]
    if candidate_table_url and new_total > 0:
        elements += [
            {"tag": "hr"},
            {"tag": "action", "actions": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "📋 查看候选清单"},
                 "type": "primary",
                 "url": candidate_table_url},
            ]},
        ]
    header = {
        "title": {"tag": "plain_text", "content": f"✅ discover/{trigger} 完成"},
        "template": "green" if new_total > 0 else "grey",
    }
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": subtitle}
    return {
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": elements,
    }


def download_feishu_file(message_id: str, file_key: str, file_name: str) -> str:
    from src.utils.helper import ensure_tmp_dir
    tmp_dir = ensure_tmp_dir()
    save_path = os.path.join(tmp_dir, file_name)

    req = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(file_key) \
        .type("file") \
        .build()
    resp = client.im.v1.message_resource.get(req)

    if resp.success():
        with open(save_path, "wb") as f:
            f.write(resp.file.read())
        logger.info(f"下载飞书文件成功：{save_path}")
        return save_path
    else:
        logger.error(f"下载飞书文件失败：{resp.msg}")
        return ""
