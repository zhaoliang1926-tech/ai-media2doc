import re
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# 项目根：src/utils/helper.py 向上 2 层
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    """返回临时目录路径，默认 project_root/tmp，env TMP_DIR 可覆盖。

    旧实现 hardcode /app/tmp 是 Docker 容器路径，朋友 macOS 直接装时
    os.makedirs("/app/tmp") 报 Read-only file system，整条 import 链炸.
    """
    tmp_dir = os.getenv("TMP_DIR") or str(_PROJECT_ROOT / "tmp")
    try:
        os.makedirs(tmp_dir, exist_ok=True)
    except OSError:
        # 兜底：env 给的路径不可写 → 回项目根 tmp
        tmp_dir = str(_PROJECT_ROOT / "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


def clean_tmp_file(filepath: str):
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass
