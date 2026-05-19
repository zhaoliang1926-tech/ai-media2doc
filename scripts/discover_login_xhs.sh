#!/usr/bin/env bash
# 小红书 cookies 重扫脚本（MediaCrawler QR 登录路径）。
#
# 用法:
#   bash scripts/discover_login_xhs.sh
#
# 原理:
#   discover 模块走 W1 路径——MediaCrawler 用独立 Playwright Chromium，
#   服务端会定期轮换 web_session 让静态 cookies 失效。每次失效后跑本脚本
#   1) 临时把 base_config.py 改成 LOGIN_TYPE=qrcode + HEADLESS=False
#   2) 启动 MediaCrawler search，会弹出二维码窗口
#   3) 手机扫码登录，cookies 自动写入 browser_data/xhs_user_data_dir/
#   4) 不管成功失败，always 恢复 base_config.py
#
# 重扫成功标志:
#   - terminal 看到 "INFO 登录成功" 或者抓到笔记列表
#   - 之后 cron / watcher / /发现 重新有数据
#
# 新增于 2026-05-13。

set -euo pipefail

PROJECT_ROOT="/Users/zhaoliang/Documents/GitHub/AI-Media2Doc"
MC_DIR="${PROJECT_ROOT}/external/MediaCrawler"
MC_PY="/Users/zhaoliang/.venvs/media-crawler/bin/python"
BASE_CFG="${MC_DIR}/config/base_config.py"

# 1. 健康检查
if [[ ! -d "${MC_DIR}" ]]; then
    echo "❌ MediaCrawler 目录不存在: ${MC_DIR}" >&2
    exit 1
fi
if [[ ! -x "${MC_PY}" ]]; then
    echo "❌ MediaCrawler Python venv 不存在: ${MC_PY}" >&2
    exit 1
fi
if [[ ! -f "${BASE_CFG}" ]]; then
    echo "❌ base_config.py 不存在: ${BASE_CFG}" >&2
    exit 1
fi

# 2. 备份 + patch base_config.py（临时强行 qrcode + headless=False）
BACKUP="${BASE_CFG}.bak.discover_login.$$"
cp "${BASE_CFG}" "${BACKUP}"
echo "📦 已备份 base_config.py → ${BACKUP}"

# trap 保证无论如何都恢复
restore() {
    if [[ -f "${BACKUP}" ]]; then
        cp "${BACKUP}" "${BASE_CFG}"
        rm -f "${BACKUP}"
        echo "♻️  base_config.py 已恢复"
    fi
}
trap restore EXIT INT TERM

python3 - <<'PYPATCH'
import re
import sys
from pathlib import Path

cfg = Path("/Users/zhaoliang/Documents/GitHub/AI-Media2Doc/external/MediaCrawler/config/base_config.py")
src = cfg.read_text()

updates = {
    "PLATFORM": '"xhs"',
    "LOGIN_TYPE": '"qrcode"',
    "CRAWLER_TYPE": '"search"',
    "HEADLESS": 'False',
    "SAVE_LOGIN_STATE": 'True',
    "ENABLE_CDP_MODE": 'False',
    "ENABLE_GET_COMMENTS": 'False',
    "ENABLE_GET_WORDCLOUD": 'False',
    "KEYWORDS": '"销售"',
}

for key, new_val in updates.items():
    pattern = re.compile(rf'^({re.escape(key)}\s*=\s*)(.+?)(\s*(?:#.*)?)$', re.MULTILINE)
    m = pattern.search(src)
    if not m:
        print(f"WARN 未找到 {key}", file=sys.stderr)
        continue
    src = pattern.sub(rf'\g<1>{new_val}\g<3>', src, count=1)

cfg.write_text(src)
print("OK base_config.py 已临时改为 xhs + qrcode + 非 headless")
PYPATCH

# 3. 跑 MediaCrawler，会弹二维码
echo ""
echo "启动 MediaCrawler search 即将弹出二维码窗口"
echo "请用手机小红书 App 扫码登录"
echo "登录成功后会自动抓取一批笔记，然后退出"
echo ""

cd "${MC_DIR}"
"${MC_PY}" main.py || {
    echo "MediaCrawler 退出码非 0（可能正常：登录后超时退出也算成功）"
}

echo ""
echo "小红书 cookies 重扫完成"
echo "下次 cron / watcher / /发现 会直接用新 cookies"
echo "验证: pm2 logs discover-watcher --lines 20"
