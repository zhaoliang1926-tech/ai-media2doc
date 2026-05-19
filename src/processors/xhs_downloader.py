"""小红书视频下载（薄客户端）

底层：调本机 XHS-Downloader HTTP API（PM2 进程 xhs-api，监听 5556）
- 调 POST /xhs/detail 拿视频元信息 + mp4 下载地址
- requests 下载 mp4 到临时目录
- ffmpeg 提取 mp3（共享 downloader.extract_audio）
- 返回 VideoMeta（platform 由路由器设置）

GPL 隔离：本文件是 AI-Media2Doc 自己代码，不 import XHS-Downloader 源码；
            只通过 HTTP API arm's length 调用，license 不传染。

新增于 2026-05-10（小红书能力扩展）。
"""
import os
import requests
from typing import Optional

from src.utils.logger import logger
from src.utils.helper import ensure_tmp_dir
from src.processors.downloader import VideoMeta, extract_audio


XHS_API_URL = os.getenv("XHS_API_URL", "http://127.0.0.1:5556").rstrip("/")


def _parse_count(s) -> Optional[int]:
    """小红书 API 数字字段为字符串，含中文单位（'5.8万' / '1862' / null / ''）。
    解析失败返回 None（保留 VideoMeta 字段语义：未知）。
    """
    if s is None or s == "":
        return None
    s = str(s).strip()
    try:
        if "亿" in s:
            return int(float(s.replace("亿", "")) * 100_000_000)
        if "万" in s:
            return int(float(s.replace("万", "")) * 10_000)
        return int(s)
    except (ValueError, TypeError):
        return None


def _parse_upload_date(publish_time: str) -> str:
    """小红书 API 时间格式 '2026-02-20_04:29:00' → '20260220'（YYYYMMDD）。"""
    if not publish_time or "_" not in publish_time:
        return ""
    date_part = publish_time.split("_")[0]
    return date_part.replace("-", "")


def _canonical_xhs_url(raw_url: str, fallback: str) -> str:
    """规范化小红书作品链接：去掉过长 query 参数（保留 path），便于查重。"""
    if not raw_url:
        return fallback
    return raw_url.split("?")[0]


def download(url: str) -> VideoMeta:
    """小红书下载入口。"""
    logger.info(f"小红书下载开始：{url}")

    # 1. 调 xhs-api
    try:
        resp = requests.post(
            f"{XHS_API_URL}/xhs/detail",
            json={"url": url, "download": False},
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()
    except requests.RequestException as e:
        raise RuntimeError(f"调 xhs-api 失败（{XHS_API_URL}）：{e}") from e
    except ValueError as e:
        raise RuntimeError(f"xhs-api 返回非 JSON：{e}") from e

    data = result.get("data")
    if not data:
        msg = result.get("message", "(no message)")
        raise RuntimeError(f"xhs-api 未返回 data（message: {msg}）")

    # 2. 校验是视频笔记（图文笔记不处理）
    work_type = data.get("作品类型", "")
    if work_type != "视频":
        raise RuntimeError(
            f"小红书笔记类型为「{work_type}」，本能力仅支持视频笔记"
        )

    # 3. 拿下载地址
    download_urls = data.get("下载地址") or []
    download_url = next((u for u in download_urls if u), None)
    if not download_url:
        raise RuntimeError("xhs-api 未返回有效的下载地址")

    note_id = data.get("作品ID", "unknown")

    # 4. 下载 mp4 到临时目录
    tmp_dir = ensure_tmp_dir()
    video_path = os.path.join(tmp_dir, f"xhs_{note_id}.mp4")
    logger.info(f"下载视频文件：{note_id}")
    try:
        with requests.get(download_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
    except requests.RequestException as e:
        raise RuntimeError(f"小红书 mp4 下载失败：{e}") from e

    # 5. ffmpeg 提音频（共享工具）
    audio_path = os.path.join(tmp_dir, f"xhs_{note_id}.mp3")
    extract_audio(video_path, audio_path)
    try:
        os.remove(video_path)
    except OSError:
        pass

    # 6. 构造 VideoMeta
    raw_url = data.get("作品链接", "")
    canonical_url = _canonical_xhs_url(raw_url, fallback=url)
    title = (data.get("作品标题") or "").strip() or "无标题"

    meta = VideoMeta(
        title=title,
        author=data.get("作者昵称", ""),
        like_count=_parse_count(data.get("点赞数量")),
        collect_count=_parse_count(data.get("收藏数量")),
        share_count=_parse_count(data.get("分享数量")),
        comment_count=_parse_count(data.get("评论数量")),
        duration=None,
        upload_date=_parse_upload_date(data.get("发布时间", "")),
        url=canonical_url,
        audio_path=audio_path,
    )

    logger.info(f"下载完成：{meta.title}，音频：{audio_path}")
    return meta
