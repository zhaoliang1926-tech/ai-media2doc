import re
import os
from datetime import datetime
from typing import Optional


def is_douyin_url(text: str) -> bool:
    patterns = [
        r"https?://(www\.)?douyin\.com/video/\d+",
        r"https?://v\.douyin\.com/\w+",
        r"https?://www\.iesdouyin\.com/\w+",
    ]
    return any(re.search(p, text) for p in patterns)


def is_xiaohongshu_url(text: str) -> bool:
    """识别小红书 URL：覆盖 xiaohongshu.com 长链 + xhslink.com 短链。"""
    patterns = [
        r"https?://(www\.)?xiaohongshu\.com/(explore|discovery/item|user/profile)/[\w]+",
        r"https?://xhslink\.com/\S+",
    ]
    return any(re.search(p, text) for p in patterns)


def extract_url(text: str) -> Optional[str]:
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


def get_month_folder_name() -> str:
    now = datetime.now()
    return f"{now.year}年{now.month}月"


def format_number(num: Optional[int]) -> str:
    if num is None:
        return "-"
    if num >= 10000:
        return f"{num / 10000:.1f}w"
    return str(num)


def ensure_tmp_dir() -> str:
    tmp_dir = os.getenv("TMP_DIR", "/app/tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


def clean_tmp_file(filepath: str):
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass
