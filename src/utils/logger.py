import sys
import os
from pathlib import Path
from loguru import logger

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logger.remove()
logger.add(
    sys.stdout,
    level=LOG_LEVEL,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    colorize=True,
)
# 默认走项目根/logs；Docker 里通过 env LOG_DIR=/app/logs 覆盖
# 兜底：env 给的路径不可写（如 /app/logs 在 macOS 直装时是 Read-only）
# → 自动 fallback 到项目根/logs，不阻塞 import
_default_log_dir = str(Path(__file__).resolve().parents[2] / "logs")
_log_dir = os.getenv("LOG_DIR") or _default_log_dir
try:
    os.makedirs(_log_dir, exist_ok=True)
except OSError as e:
    print(f"[logger] LOG_DIR={_log_dir} 不可写 ({e})，回 fallback {_default_log_dir}", file=sys.stderr)
    _log_dir = _default_log_dir
    os.makedirs(_log_dir, exist_ok=True)
logger.add(
    os.path.join(_log_dir, "app.log"),
    level=LOG_LEVEL,
    rotation="10 MB",
    retention="30 days",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{line} - {message}",
)
