#!/usr/bin/env bash
# AI-Media2Doc 开发机一键搭建（macOS）
#
# 用法:
#   bash scripts/bootstrap.sh
#
# 幂等：重复跑只补缺，不会破坏已有 venv / external / .env
# 边界：仅装代码可移植部分；Claude CLI / pm2 / LaunchAgent / cookies 见末尾 "下一步"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ────── 颜色 ──────
if [[ -t 1 ]]; then
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
fi
ok()   { echo "${GREEN}✓${NC} $*"; }
warn() { echo "${YELLOW}⚠${NC} $*"; }
err()  { echo "${RED}✗${NC} $*" >&2; }
hdr()  { echo ""; echo "${BLUE}── $* ──${NC}"; }

echo "=== AI-Media2Doc bootstrap ==="
echo "项目根: $PROJECT_ROOT"

# ────── 1. macOS only ──────
hdr "1/8  系统检测"
if [[ "$(uname)" != "Darwin" ]]; then
    err "本脚本仅支持 macOS"
    err "NAS / Linux 部署请走 docker compose up -d"
    exit 1
fi
ok "macOS $(sw_vers -productVersion) ($(uname -m))"

# ────── 2. Python 3.10+ ──────
hdr "2/8  Python 3.10+ 检测"
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver=$("$cand" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "")
        [[ -z "$ver" ]] && continue
        major=${ver%.*}; minor=${ver#*.}
        if [[ "$major" -eq 3 && "$minor" -ge 10 ]]; then
            PY="$(command -v "$cand")"
            ok "Python $ver → $PY"
            break
        fi
    fi
done
if [[ -z "$PY" ]]; then
    err "未找到 Python 3.10+（douyin-downloader 要求 3.10+ 语法）"
    err "请安装: brew install python@3.11"
    exit 1
fi

# ────── 3. 项目主 venv ──────
hdr "3/8  项目主 venv (.venv)"
if [[ ! -d ".venv" ]]; then
    "$PY" -m venv .venv
    ok ".venv 已创建"
else
    ok ".venv 已存在"
fi
.venv/bin/pip install -q --upgrade pip
echo "安装 requirements.txt..."
.venv/bin/pip install -q -r requirements.txt
ok "项目依赖已装"

# ────── 4. MediaCrawler 拉取 ──────
hdr "4/8  MediaCrawler 引擎 (external/MediaCrawler)"
MC_DIR="external/MediaCrawler"
MC_REPO="https://github.com/NanmiCoder/MediaCrawler.git"
MC_COMMIT="f328ee35b55e25e8aaeb9c847fe8b622e3f3447f"  # 2026-04-30 bootstrap 锁定

if [[ ! -d "$MC_DIR/.git" ]]; then
    mkdir -p external
    echo "克隆 MediaCrawler..."
    git clone "$MC_REPO" "$MC_DIR"
    ( cd "$MC_DIR" && git checkout -q "$MC_COMMIT" )
    ok "MediaCrawler @ ${MC_COMMIT:0:12}"
else
    cur=$( cd "$MC_DIR" && git rev-parse HEAD )
    if [[ "$cur" != "$MC_COMMIT" ]]; then
        warn "当前 commit ${cur:0:12} ≠ bootstrap 锁定 ${MC_COMMIT:0:12}"
        warn "  如需对齐: cd $MC_DIR && git checkout $MC_COMMIT"
    else
        ok "MediaCrawler @ ${MC_COMMIT:0:12} (已对齐)"
    fi
fi

# ────── 5. MediaCrawler venv ──────
hdr "5/8  MediaCrawler venv (~/.venvs/media-crawler)"
MC_VENV="${MEDIACRAWLER_VENV_DIR:-$HOME/.venvs/media-crawler}"
if [[ ! -d "$MC_VENV" ]]; then
    mkdir -p "$(dirname "$MC_VENV")"
    "$PY" -m venv "$MC_VENV"
    ok "venv 已创建: $MC_VENV"
else
    ok "venv 已存在: $MC_VENV"
fi
"$MC_VENV/bin/pip" install -q --upgrade pip
echo "安装 MediaCrawler 依赖..."
"$MC_VENV/bin/pip" install -q -r "$MC_DIR/requirements.txt"
echo "安装 Playwright Chromium（首次会下载 ~150MB）..."
"$MC_VENV/bin/python" -m playwright install chromium 2>&1 | tail -2 || warn "playwright install 失败，登录时再补"
ok "MediaCrawler venv 就绪"

# ────── 6. ffmpeg 检查 ──────
hdr "6/8  ffmpeg 系统依赖"
if ! command -v ffmpeg >/dev/null 2>&1; then
    warn "ffmpeg 未安装 → brew install ffmpeg"
    warn "  音频提取功能将不可用"
else
    ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
fi

# ────── 7. .env 初始化 ──────
hdr "7/8  .env 配置文件"
if [[ ! -f ".env" ]]; then
    cp .env.example .env
    warn ".env 从模板创建 → 请编辑填入 API keys / 飞书 token"
else
    ok ".env 已存在（保留不动）"
fi

# ────── 8. 必要目录 ──────
hdr "8/8  运行时目录"
mkdir -p logs tmp data/discover
ok "logs / tmp / data/discover 已就绪"

# ────── 收口提示 ──────
echo ""
echo "${GREEN}=== bootstrap 完成 ===${NC}"
echo ""
echo "下一步（脚本不自动处理的部分）："
echo "  1. 编辑 ${YELLOW}.env${NC} 填入飞书 / Claude / 阿里百炼等 API keys"
echo "  2. 装 Claude CLI（本机改写依赖）："
echo "       npm i -g @anthropic-ai/claude-code && claude setup-token"
echo "  3. 抖音 cookies 二选一："
echo "       - 浏览器方案：在 .env 设 DOUYIN_COOKIES_BROWSER=chrome（需已登录抖音）"
echo "       - 文件方案：用 \"Get cookies.txt LOCALLY\" 导出到 config/douyin_cookies.txt"
echo "  4. 小红书首次登录："
echo "       bash scripts/discover_login_xhs.sh    # 弹二维码，手机扫码"
echo "  5. 启动（任选）："
echo "       .venv/bin/python src/main.py                  # 前台跑"
echo "       pm2 start ecosystem.config.js                 # 守护跑"
echo ""
