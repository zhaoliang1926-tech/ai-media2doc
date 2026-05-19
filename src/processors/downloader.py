"""短视频下载路由分发器
- 平台路由：根据 URL 调对应 platform downloader（douyin / xhs）
- 共享：VideoMeta dataclass、ffmpeg 提音频工具
- 飞书文件直传入口
"""
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from src.utils.logger import logger
from src.utils.helper import is_douyin_url, is_xiaohongshu_url


@dataclass
class VideoMeta:
    title: str = ""
    author: str = ""
    like_count: Optional[int] = None
    collect_count: Optional[int] = None
    share_count: Optional[int] = None
    comment_count: Optional[int] = None
    duration: Optional[int] = None
    upload_date: str = ""
    url: str = ""
    audio_path: str = ""
    platform: str = ""  # "douyin" | "xiaohongshu" | "file"


def extract_audio(video_path: str, audio_path: str):
    """ffmpeg 提取 MP3 音频（被各平台 downloader 复用）。
    单声道 + 16kHz + libmp3lame + 128k bitrate（Paraformer ASR 兼容）。
    """
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-acodec", "libmp3lame", "-ab", "128k",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 提取音频失败：{result.stderr[-500:]}")


def download(url: str) -> VideoMeta:
    """根据 URL 路由到对应 platform downloader。

    返回的 VideoMeta.platform 字段标记来源平台，供下游统计使用。
    """
    if is_douyin_url(url):
        from src.processors.douyin_downloader import download as _douyin_download
        meta = _douyin_download(url)
        meta.platform = "douyin"
        return meta
    if is_xiaohongshu_url(url):
        from src.processors.xhs_downloader import download as _xhs_download
        meta = _xhs_download(url)
        meta.platform = "xiaohongshu"
        return meta
    raise RuntimeError(f"不支持的视频平台 URL：{url}")


def download_from_file(file_path: str) -> VideoMeta:
    """飞书文件直传：用 ffmpeg 转成 Paraformer 兼容 mp3，不解析元数据。"""
    logger.info(f"使用本地文件：{file_path}")
    audio_path = os.path.splitext(file_path)[0] + "_mono.mp3"
    extract_audio(file_path, audio_path)
    return VideoMeta(audio_path=audio_path, platform="file")
