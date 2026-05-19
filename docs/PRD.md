# AI-Media2Doc · 短视频改写项目 PRD

> 项目主架构文档，记录系统设计、决策、运维 SOP，供未来维护者（含未来的你 / 协作者 / AI 助手）阅读。
>
> **最后更新**：2026-05-10
> **版本**：v2.0（NAS Docker → Mac PM2 单端 + 小红书能力扩展）

---

## 0. TL;DR · 一图看懂

```
飞书群发短视频链接（抖音 / 小红书）或上传视频文件
            ↓
   Mac PM2: ai-media2doc-main（飞书 ws 长连接）
            ↓
   URL 路由识别（is_douyin_url / is_xiaohongshu_url）
            ↓
   ┌─────────────┬─────────────┬─────────────┐
   抖音下载       小红书下载     视频文件直传
   （自定义 API）  （xhs-api 5556）（飞书 file_key）
   └─────┬───────┴─────┬───────┴─────┬───────┘
            ↓
       ffmpeg 提音频 → /tmp/media2doc/*.mp3
            ↓
   阿里百炼 Paraformer ASR 转文字
            ↓
   2 号小助理改写（Qwen-max）
            ↓
   创建飞书文档 + 写入多维表格 + IM 卡片回执
            ↓
   1 号小助理改写（Claude CLI 异步线程，结果追加到文档底部）
```

**关键决策一览**：
- 单端架构：所有处理在 Mac 上，NAS 容器作灰度兜底（停而不删）
- 双 venv：`media2doc` (Python 3.10) 跑业务 + `xhs-downloader` (Python 3.12) 跑 GPL 引擎
- HTTP API 隔离：XHS-Downloader (GPL) 通过 5556 HTTP API 调用，代码不互相 import
- 飞书 ws 单客户端：抖音 / 小红书 / 文件全走同一个机器人 + 同一个群

---

## 1. 产品定位

### 1.1 是什么
- **私域 Service**，单一用户私人工作站使用
- 将短视频（口播类）一键转为结构化运营文案
- 双 AI 改写产出（Qwen 速度优先 + Claude 质量优先）
- 结果自动归档到飞书文档库 + 多维表格统计

### 1.2 不是什么
- ❌ 不是公开 SaaS / 不对外提供 API
- ❌ 不是批量爬虫工具（一次处理一条链接）
- ❌ 不做视频字幕硬叠 / 转码 / 剪辑等媒体处理

### 1.3 输入输出契约

| 输入 | 描述 |
|---|---|
| 飞书群文本消息 | 含抖音长链 `https://www.douyin.com/video/...` 或 `https://v.douyin.com/...` 短链 |
| 飞书群文本消息 | 含小红书长链 `https://www.xiaohongshu.com/explore/...` / `discovery/item/...` 或 `https://xhslink.com/o/...` 短链 |
| 飞书群文件消息 | 直接发送 mp4 / 音频文件到群 |
| 飞书群文本消息 | "重新转写"（针对已查重的旧记录）|

| 输出 | 描述 |
|---|---|
| 飞书文档 | 含 2 号改写 + 原始素材 + 1 号改写（异步追加），按月份归档 |
| 飞书多维表格 | 1 行 = 1 条视频处理记录（标题、链接、来源账号、改写概要、点赞数等 16 字段）|
| 飞书 IM 卡片 | 处理完成卡片 + 1 号改写完成回执 |

---

## 2. 系统架构

### 2.1 总体架构图

```
┌──────────────────────────────────────────────────────────────┐
│ macOS 工作站 (192.168.50.92)                                 │
│                                                              │
│  ┌──────────── 业务层 ────────────────┐                       │
│  │ AI-Media2Doc 项目                  │                       │
│  │ ~/Documents/GitHub/AI-Media2Doc/   │                       │
│  │                                    │                       │
│  │ PM2: ai-media2doc-main             │                       │
│  │   - src/main.py（飞书 ws 长连接）  │                       │
│  │   - venv: ~/.venvs/media2doc       │                       │
│  │   - Python 3.10                    │                       │
│  └────────┬───────────────────────────┘                       │
│           │                                                   │
│           ├─ douyin URL → src/processors/douyin_downloader.py │
│           │   （直接调抖音内部 API，自定义反爬）              │
│           │                                                   │
│           ├─ xhs URL → src/processors/xhs_downloader.py       │
│           │   ↓ POST localhost:5556                           │
│           │   ┌─── 引擎层 ───────────────────────┐           │
│           │   │ XHS-Downloader（GPL 项目）        │           │
│           │   │ external/XHS-Downloader/          │           │
│           │   │   - .gitignore 排除（GPL 隔离）   │           │
│           │   │   - 独立 git，独立升级            │           │
│           │   │ PM2: xhs-api                      │           │
│           │   │   - venv: ~/.venvs/xhs-downloader │           │
│           │   │   - Python 3.12 (uv 管理)         │           │
│           │   │   - 监听 0.0.0.0:5556             │           │
│           │   └───────────────────────────────────┘           │
│           │                                                   │
│           └─ video file → 飞书 SDK download_file               │
│                                                               │
│  ┌──────────── 共享处理 ──────────────┐                       │
│  │ ASR (transcriber.py)              │ → 阿里百炼 (云)       │
│  │ Qwen 改写 (rewriter.py)            │ → 阿里百炼 qwen-max   │
│  │ Claude 改写 (rewriter.py)          │ → 本机 claude CLI     │
│  │ 飞书文档+多维表格 (handlers/)      │ → 飞书开放平台        │
│  └────────────────────────────────────┘                       │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 物理部署清单

| 资源 | 位置 | 用途 |
|---|---|---|
| AI-Media2Doc 主代码 | `~/Documents/GitHub/AI-Media2Doc/src/` | 业务逻辑 |
| 项目内外部依赖 | `~/Documents/GitHub/AI-Media2Doc/external/XHS-Downloader/` | GPL 引擎源码（被 .gitignore 排除）|
| 主项目 venv | `~/.venvs/media2doc/` | Python 3.10，业务依赖 |
| XHS 引擎 venv | `~/.venvs/xhs-downloader/` | Python 3.12，引擎依赖 |
| Python 3.12 解释器 | `~/.local/share/uv/python/cpython-3.12.11-macos-aarch64-none/` | uv 管理，不污染系统 brew |
| 临时文件 | `/tmp/media2doc/` | mp3 / mp4 中间产物，处理完即删 |
| 业务日志 | `~/Documents/GitHub/AI-Media2Doc/logs/app.log` | loguru 输出 |
| PM2 日志 | `~/.pm2/logs/<process>-{out,err}.log` | stdout/stderr |
| 抖音 cookies | `~/Documents/GitHub/AI-Media2Doc/config/douyin_cookies.txt` | Netscape 格式，6 周需刷 |
| 小红书 cookies | 不需要（258p 低清免登录可下载）| — |

### 2.3 模块解耦边界

| 模块 | 性质 | 隔离方式 |
|---|---|---|
| 抖音 downloader | AI-Media2Doc 内部代码 | — |
| 小红书 downloader（薄客户端）| AI-Media2Doc 内部代码 | — |
| **XHS-Downloader 引擎**（GPL）| **外部底层引擎** | HTTP API arm's length（不 import 代码）+ .gitignore 排除（git 不跟踪）|
| 阿里百炼 ASR / Qwen | 外部 SaaS | HTTP API + dashscope SDK |
| Claude CLI | 外部 CLI 工具 | subprocess.run + OAuth |
| 飞书 API | 外部 SaaS | HTTP API + lark_oapi SDK |

### 2.4 数据流（成功路径）

```
1. 飞书 ws → on_message(data: P2ImMessageReceiveV1)
2. 提取 message.content 文本 → extract_url(text)
3. URL 路由：
   - is_douyin_url(url) → 标记 platform="douyin"
   - is_xiaohongshu_url(url) → 标记 platform="xiaohongshu"
4. 多维表格查重（find_by_url）：
   - 已存在 → 弹出"重新转写？"卡片，等用户确认
   - 不存在 → threading.Thread 异步进入 process()
5. process(parsed):
   a. 平台路由 download(url, platform)
      - douyin: douyin_downloader.download()
      - xhs: xhs_downloader.download() → POST localhost:5556 → 拿 mp4 URL → requests 下载 → ffmpeg 提音频
      - file: download_feishu_file() → ffmpeg 提音频
      → 返回 VideoMeta(title, audio_path, uploader, duration, like_count, ...)
   b. ASR: transcriber.transcribe(audio_path) → 文本
   c. 改写: rewriter.rewrite(transcript) → RewriteResult(category, title, summary, ...)
   d. 飞书文档: feishu_doc.create_doc(title, rewrite_result, meta, transcript)
   e. 多维表格: feishu_bitable.insert_record(meta, rewrite_result, doc_url, sender_open_id)
   f. IM 卡片: msg_handler.send_card(success_card)
6. Step 7（异步 Claude 改写 + 三档群通知）：
   - 群发 `🤖 1号小助理开始改写，预计 1 分钟左右...`（启动时刻，用户感知）
   - threading.Thread → rewriter.rewrite_with_claude_cli(transcript) → feishu_doc.append_claude_rewrite() （插入文档顶部）
   - 成功 → 群发 `🤖 1号小助理改写版已完成，已插入文档顶部`
   - 失败 → 群发 `❌ 1号小助理改写失败：...`（不影响主流程已完成的 2 号产出）
```

### 2.5 数据流（错误路径）

| 错误位置 | 反应 |
|---|---|
| 下载失败（抖音 / xhs API 异常 / 无视频流）| 飞书发"❌ 视频下载失败，请检查链接是否有效" |
| ASR 失败 | 飞书发"❌ 语音识别失败，音频可能无人声或格式不支持" |
| Qwen 改写失败 | feishu_doc 错误卡片 |
| Claude CLI 失败（异步）| logger.error 但不发飞书（不影响主流程，2 号改写已成功）|
| 多维表格写入失败 | logger.error，文档已创建（半完成状态）|

---

## 3. 能力清单

### 3.1 输入能力
- 飞书 ws 长连接（lark.ws.Client，单客户端独占）
- URL 提取（`extract_url` 在 helper.py，正则 `https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+`）
- 抖音 URL 识别（`is_douyin_url`，覆盖 douyin.com / v.douyin.com / iesdouyin.com）
- 小红书 URL 识别（`is_xiaohongshu_url`，覆盖 xiaohongshu.com / xhslink.com）
- 视频文件接收（`message.message_type in ("file", "audio", "media")`）

### 3.2 下载能力

| 平台 | 实现方式 | cookies | 备注 |
|---|---|---|---|
| 抖音 | 自定义反爬（直接调抖音内部 API）| 需要 douyin_cookies.txt（6 周失效）| 高清原画 |
| 小红书 | XHS-Downloader API（HTTP 5556）+ requests 下载 mp4 | 不需要（258p 低清）| 高清需 cookies（未启用）|
| 飞书文件 | lark.client.im.v1.message.get_file | — | 用户直传到群 |

### 3.3 ASR 转写
- 服务：阿里百炼 Paraformer-realtime-v2
- 输入：mp3 音频
- 输出：纯文字（无时间戳、无说话人标签）
- 凭据：`DASHSCOPE_API_KEY`

### 3.4 改写能力

| 改写 | 名称 | 实现 | 用途 |
|---|---|---|---|
| 2 号小助理 | Qwen-max | 阿里百炼 SaaS | 速度优先（5-15s）、稳定 |
| 1 号小助理 | Claude CLI | 本机 `claude --print`（OAuth）| 质量优先（30-60s）、深度分析 |

两者**串行**：先 Qwen 写主文档，再 Claude 异步追加。Claude 失败不影响主流程。

**1 号机器人群通知三档**（保证用户全程有感知）：
- 启动：`🤖 1号小助理开始改写，预计 1 分钟左右...`
- 完成：`🤖 1号小助理改写版已完成，已插入文档顶部`
- 失败：`❌ 1号小助理改写失败：<error 前 100 字>`

### 3.5 输出能力

- 飞书文档（docx）：按月份归档（2026年5月 / 2026年6月...），含 2 号 + 原始素材 + 1 号 三段
- 飞书多维表格：16 字段（标题、抖音链接、来源账号、改写概要、视频分类、月份、处理状态、查看文档、提交时间、是否已录制、点赞数、收藏数、转发数、评论数、视频时长、提交人）
- 飞书 IM：成功卡片 + 1 号完成文本通知 / 失败错误卡片

---

## 4. 代码组织

### 4.1 目录结构

```
~/Documents/GitHub/AI-Media2Doc/
├── src/
│   ├── main.py                          ← 飞书 ws 入口 + 业务编排
│   ├── handlers/
│   │   ├── feishu_client.py             ← 飞书 SDK 客户端封装
│   │   ├── feishu_doc.py                ← 文档创建 / Claude 追加
│   │   ├── feishu_bitable.py            ← 多维表格 CRUD
│   │   └── message.py                   ← 消息卡片构造 + 文件下载
│   ├── processors/
│   │   ├── downloader.py                ← 路由分发入口（薄）+ VideoMeta dataclass
│   │   ├── douyin_downloader.py         ← 抖音下载（自定义反爬）
│   │   ├── xhs_downloader.py            ← 小红书下载（调 xhs-api）
│   │   ├── transcriber.py               ← ASR
│   │   └── rewriter.py                  ← Qwen / Claude 改写 Provider
│   └── utils/
│       ├── helper.py                    ← URL 识别 / 格式工具
│       └── logger.py                    ← loguru 配置
├── external/                            ← 外部底层引擎（.gitignore 排除）
│   └── XHS-Downloader/                  ← GPL 项目源码（独立 git）
├── config/
│   └── douyin_cookies.txt               ← 抖音 cookies（Chrome 插件导出）
├── logs/
│   └── app.log                          ← 业务日志（loguru）
├── docs/
│   └── PRD.md                           ← 本文档
├── claude_server.py                     ← 旧 Mac Claude 中转服务（已闲置，可清）
├── poller.py                            ← （历史遗留）
├── requirements.txt
├── .env                                 ← 凭据（git 不跟踪）
├── .env.example
├── .gitignore                           ← 含 external/
└── README.md
```

### 4.2 各模块职责（一句话）

| 文件 | 职责 |
|---|---|
| `main.py` | 飞书 ws 接消息 → URL 路由 → 异步 spawn process() |
| `processors/downloader.py` | 路由分发：根据 platform 调 douyin/xhs downloader；定义 VideoMeta |
| `processors/douyin_downloader.py` | 抖音视频下载 + ffmpeg 提音频 |
| `processors/xhs_downloader.py` | 调 xhs-api → 下载 mp4 → ffmpeg 提音频 |
| `processors/transcriber.py` | 调阿里百炼 ASR API |
| `processors/rewriter.py` | Qwen / Claude CLI 双 Provider 抽象 |
| `handlers/feishu_doc.py` | 创建文档 / 追加 Claude 改写 |
| `handlers/feishu_bitable.py` | 多维表格写入 / 查重 |
| `handlers/message.py` | 卡片构造 + 文件下载 + 文本/卡片发送 |
| `utils/helper.py` | URL 识别 / 文本清理 / 临时文件 |
| `utils/logger.py` | loguru 配置（LOG_DIR 环境变量驱动）|

### 4.3 抽象边界 · VideoMeta 是平台共契约

```python
@dataclass
class VideoMeta:
    title: str            # 视频标题（必填）
    audio_path: str       # 提取后的音频本地路径（必填）
    url: str              # 原始视频链接（必填，写多维表格"抖音链接"字段）
    uploader: str         # 来源账号
    duration: float       # 视频时长（秒）
    like_count: int       # 点赞数
    collect_count: int    # 收藏数
    share_count: int      # 转发数
    comment_count: int    # 评论数
    description: str      # 描述（话题标签）
    platform: str         # "douyin" | "xiaohongshu" | "file"
```

任一 platform 的 downloader **必须返回这个 VideoMeta**，下游 ASR / 改写 / 飞书写入完全平台无关。

---

## 5. 外部依赖

### 5.1 SaaS

| 服务 | 用途 | 凭据 env | 失效后果 |
|---|---|---|---|
| 飞书开放平台 | ws 接消息 / 文档 / 多维表格 / IM | `FEISHU_APP_ID` + `FEISHU_APP_SECRET` | 业务全停 |
| 阿里百炼 | ASR + Qwen 改写 | `DASHSCOPE_API_KEY` | ASR / 2 号停 |
| Anthropic Claude（OAuth）| 1 号改写 | `~/.claude/...` keychain（OAuth）| 1 号停（不影响主流程）|

### 5.2 本地服务

| 服务 | PM2 名 | 端口 | 用途 |
|---|---|---|---|
| AI-Media2Doc 主进程 | `ai-media2doc-main` | — | 飞书 ws + 业务编排 |
| XHS-Downloader API | `xhs-api` | 5556 | 小红书反爬 + 视频 URL 提取 |

### 5.3 系统依赖

| 工具 | 版本要求 | 安装方式 |
|---|---|---|
| Python 3.10 | brew 装 | `brew install python@3.10` |
| Python 3.12 | uv 装 | `uv python install 3.12`（不污染 brew）|
| ffmpeg | 任意现代版 | `brew install ffmpeg` |
| yt-dlp | 任意 | `pip install yt-dlp` |
| Claude CLI | macOS 桌面端 OR `claude` 命令 | Anthropic 官网 |
| pm2 | 任意 | `npm install -g pm2` |
| uv | 任意 | curl 一键装 |

---

## 6. 配置管理

### 6.1 .env 字段表

> 文件位置：`~/Documents/GitHub/AI-Media2Doc/.env`（git 不跟踪）

| 字段 | 用途 | 来源 | 必填 |
|---|---|---|---|
| `FEISHU_APP_ID` | 飞书机器人 App ID | 飞书开放平台 → 应用凭证 | ✅ |
| `FEISHU_APP_SECRET` | 飞书机器人 App Secret | 飞书开放平台 → 应用凭证 | ✅ |
| `FEISHU_FOLDER_TOKEN` | 飞书云文档目录 token | 飞书云文档 URL 末段 | ✅ |
| `FEISHU_BITABLE_APP_TOKEN` | 多维表格 App token | 多维表格 URL 中 `app_token=` | ✅ |
| `FEISHU_BITABLE_TABLE_ID` | 多维表格 Table ID | 多维表格 URL 中 `table=` | ✅ |
| `DASHSCOPE_API_KEY` | 阿里百炼 API Key | bailian.aliyun.com | ✅ |
| `DASHSCOPE_ASR_MODEL` | ASR 模型名 | 默认 paraformer-realtime-v2 | — |
| `REWRITER_PROVIDER` | 默认改写 provider | qwen / openai / claude | — |
| `CLAUDE_API_KEY` | （备用）Claude API key | console.anthropic.com | — |
| `CLAUDE_MODEL` | Claude 模型名 | 如 claude-sonnet-4-5 | — |
| `ENABLE_CLAUDE_CLI` | 是否启用 1 号 | true / false | — |
| `CLAUDE_SERVER_URL` | （废弃）Mac 中转 server URL | 已废弃留空 | — |
| `DOUYIN_COOKIES_FILE` | 抖音 cookies 路径 | config/douyin_cookies.txt | ✅ |
| `XHS_API_URL` | XHS 引擎 API 地址 | http://127.0.0.1:5556 | ✅ |
| `LOG_DIR` | 日志目录 | logs/ | ✅ |
| `TMP_DIR` | 临时文件目录 | /tmp/media2doc | — |

### 6.2 cookies 管理

| Cookies | 失效周期 | 刷新方式 |
|---|---|---|
| 抖音 douyin_cookies.txt | ~6 周 | Chrome 登录抖音 → 「Get cookies.txt LOCALLY」插件 → Export → 替换 config/douyin_cookies.txt |
| 小红书 | 不需要（低清免登录）| — |

### 6.3 secret 不进 git

- `.env` 在 `.gitignore` 中
- `config/douyin_cookies.txt` 在 `.gitignore` 中
- `external/` 在 `.gitignore` 中（防 GPL 传染）
- `.env.example` 提供字段模板（无值）

---

## 7. PM2 进程清单

### 7.1 ai-media2doc-main

```bash
pm2 start /Users/zhaoliang/Documents/GitHub/AI-Media2Doc/src/main.py \
  --name ai-media2doc-main \
  --interpreter /Users/zhaoliang/.venvs/media2doc/bin/python \
  --cwd /Users/zhaoliang/Documents/GitHub/AI-Media2Doc
```

- **用途**：飞书 ws 长连接 + 业务编排
- **依赖**：venv `~/.venvs/media2doc`、所有 SaaS 凭据、xhs-api 健康
- **重启场景**：改 src/ 代码、改 .env 字段

### 7.2 xhs-api

```bash
pm2 start /Users/zhaoliang/Documents/GitHub/AI-Media2Doc/external/XHS-Downloader/main.py \
  --name xhs-api \
  --interpreter /Users/zhaoliang/.venvs/xhs-downloader/bin/python \
  --cwd /Users/zhaoliang/Documents/GitHub/AI-Media2Doc/external/XHS-Downloader \
  -- API
```

- **用途**：小红书反爬 + 视频 URL 提取
- **端口**：5556（FastAPI + uvicorn）
- **健康检查**：`curl localhost:5556/docs` HTTP 200
- **依赖**：venv `~/.venvs/xhs-downloader`（Python 3.12）+ 互联网（访问 xhscdn.com）

### 7.3 SessionStart hook · 开机自动 resurrect

- 配置：`~/.claude/settings.json` 的 `hooks.SessionStart` 数组追加
- 脚本：`~/bin/pm2-auto-resurrect.sh`（幂等，daemon 活就跳过）
- 触发：每次 Claude Code（桌面端 / Terminal `claude` / cc-connect）启动时
- **代价**：你必须**登录系统后开 Claude Code 一次**，PM2 才会被拉起

---

## 8. 部署 / 启动 / 升级

### 8.1 首次安装（新机器）

```bash
# 1. 装 brew + python 3.10
brew install python@3.10 ffmpeg
pip install -g yt-dlp pm2

# 2. 装 uv + python 3.12
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12

# 3. clone 主项目
mkdir -p ~/Documents/GitHub/
cd ~/Documents/GitHub/
git clone <ai-media2doc-repo> AI-Media2Doc
cd AI-Media2Doc

# 4. 主项目 venv
python3.10 -m venv ~/.venvs/media2doc
~/.venvs/media2doc/bin/pip install -r requirements.txt

# 5. clone XHS-Downloader 引擎
mkdir external && cd external
git clone --depth 1 https://github.com/JoeanAmier/XHS-Downloader.git
cd XHS-Downloader
uv venv ~/.venvs/xhs-downloader --python 3.12
uv pip install --python ~/.venvs/xhs-downloader/bin/python -r requirements.txt

# 6. 配置
cp ~/Documents/GitHub/AI-Media2Doc/.env.example ~/Documents/GitHub/AI-Media2Doc/.env
# 编辑 .env 填飞书 / 阿里百炼 / etc

# 7. 抖音 cookies（Chrome 装插件 → 导出）→ config/douyin_cookies.txt

# 8. 启动
cd ~/Documents/GitHub/AI-Media2Doc
pm2 start src/main.py --name ai-media2doc-main --interpreter ~/.venvs/media2doc/bin/python
pm2 start external/XHS-Downloader/main.py --name xhs-api --interpreter ~/.venvs/xhs-downloader/bin/python --cwd external/XHS-Downloader -- API
pm2 save
```

### 8.2 升级 AI-Media2Doc 主代码

```bash
cd ~/Documents/GitHub/AI-Media2Doc
git pull
~/.venvs/media2doc/bin/pip install -r requirements.txt  # 如果依赖变了
pm2 restart ai-media2doc-main
```

### 8.3 升级 XHS-Downloader 引擎

```bash
cd ~/Documents/GitHub/AI-Media2Doc/external/XHS-Downloader
git pull
uv pip install --python ~/.venvs/xhs-downloader/bin/python -r requirements.txt
pm2 restart xhs-api
```

### 8.4 重启 Mac 后

- 你登录系统后**开一次 Claude Code**（桌面端 / Terminal）→ SessionStart hook 自动 `pm2 resurrect` → 所有进程拉起
- 不开 Claude Code 时业务不可用（飞书消息会漏）

---

## 9. 故障切回兜底

### 9.1 NAS 容器（灰度兜底）

- 位置：NAS 192.168.50.202 绿联云 → Docker → ai-media2doc 容器（已停止，未删除）
- 切回：9999 UI → 容器列表 → ai-media2doc → 点"启动"（30 秒恢复）
- Mac 切停：`pm2 stop ai-media2doc-main && pm2 stop xhs-api && pm2 save`
- 飞书 ws 自动重连到 NAS，业务恢复

### 9.2 PM2 进程级 rollback

```bash
# 看历史 dump.pm2 备份
ls ~/.pm2/dump.pm2*

# 还原到指定版本
cp ~/.pm2/dump.pm2.bak-<ts> ~/.pm2/dump.pm2
pm2 kill && pm2 resurrect
```

### 9.3 .env 备份命名约定

- 命名：`.env.backup-pre-<reason>-<timestamp>`
- 例：`.env.backup-pre-mac-cutover-1778421285`
- 切回：`cp .env.backup-pre-xxx .env && pm2 restart ai-media2doc-main`

### 9.4 应急联络

| 故障 | 第一步 |
|---|---|
| 飞书 token 续命失败 | Terminal 跑 `claude setup-token`（弹浏览器登录）|
| 抖音 cookies 失效 | Chrome 重导（5 分钟）|
| 小红书 cookies 失效 | 不需要（除非要高清）|
| Mac IP 变化（DHCP）| .env 不需要改（已用 127.0.0.1）|

---

## 10. 维护检查表

### 10.1 周期性任务

| 周期 | 任务 | 行为 |
|---|---|---|
| 每 6 周 | 抖音 cookies 刷新 | Chrome 插件重导 → 替换 config/douyin_cookies.txt |
| 每 24h | OAuth token 续命 | PM2 cron `token-refresh`（4,16 点）自动跑 |
| 每月 | XHS-Downloader 升级 | `cd external/XHS-Downloader && git pull && pm2 restart xhs-api` |
| 每月 | 多维表格清理 | 飞书 UI 手动归档老记录 |

### 10.2 故障诊断 SOP

| 症状 | 排查 |
|---|---|
| 飞书发链接没反应 | `pm2 logs ai-media2doc-main`，看 ws 是否 ping/pong；`pm2 list` 看进程在 |
| 抖音下载失败 | 看 logs/app.log "下载失败"；尝试 cookies 刷新；ffmpeg 装否 |
| 小红书下载失败 | `curl localhost:5556/docs` 看 xhs-api 在；`pm2 restart xhs-api`；`pm2 logs xhs-api` 看错误 |
| ASR 失败 | `DASHSCOPE_API_KEY` 是否过期；`echo "test" \| dashscope` 测连通 |
| 1 号改写不出 | logs 看 "Claude CLI 失败"；`echo "ping" \| claude --print` 测 token |
| 多维表格写不进去 | 飞书 token 续命；`FEISHU_BITABLE_APP_TOKEN` 是否变更 |
| TCC Operation not permitted | SessionStart hook 是否触发；`launchctl list \| grep pm2-resurrect` 应为空 |

### 10.3 健康检查（一键命令）

```bash
echo "=== PM2 ===" && pm2 list
echo "=== ws 心跳 ===" && pm2 logs ai-media2doc-main --lines 3 --nostream | grep ping
echo "=== xhs-api ===" && curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:5556/docs
echo "=== Claude ===" && echo "ping" | claude --print --output-format text 2>&1 | head -1
```

---

## 11. 演进历史

### 2026-04-19 之前 · NAS Docker 时代
- 主链路跑在绿联云 NAS Docker 容器
- Mac 端只跑 claude_server.py 做 1 号改写中转

### 2026-05-10 · TCC bug 修复
- 5/9 brief 流被全局 hook 元注入污染（PUA / ECC 等）
- 5/10 02:01 装 `com.zhaoliang.pm2-resurrect.plist` 试图开机自启 PM2
- launchd 启动的 PM2 进程撞上 macOS Sonoma+ TCC 围栏（不能访问 ~/Documents/）
- ai-media2doc errored 91 次（5/10 04:00 cron restart 后）
- 修复：删 plist + 改用 Claude Code SessionStart hook 触发 `pm2 resurrect`

### 2026-05-10 · NAS → Mac 单端迁移
- 决策：避免 NAS 维护麻烦（绿联云 UI 部署 / SMB 改 compose）
- 关键：飞书 ws 不是抢占式，必须先停 NAS 才能让 Mac 接管
- 步骤：装 SMB 凭据 → 读 NAS .env 补 Mac .env → rsync 3 个 src 文件 → pm2 启动 main.py → 9999 UI 停 NAS 容器
- 验证：飞书发抖音链接 → Mac 完整链路（07:46-07:47）跑通

### 2026-05-10 · 小红书能力扩展（Phase 1 部署调研）
- 调研：yt-dlp XiaoHongShu extractor 失效（4/19 issue 还 open）
- 选型：JoeanAmier/XHS-Downloader（11119 ⭐，5/10 还在更新，GPL）
- 隔离：external/.gitignore 排除 + HTTP API arm's length（避免 GPL 传染）
- 重构：downloader.py 拆成路由 + douyin_downloader.py + xhs_downloader.py
- 验证：3 条用户提供的小红书 URL 全部拿到 mp4 下载链接

### 2026-05-10 · 小红书能力实施完成（Phase 2 编码 + 端到端验证）

**架构定位修正**（对齐用户视角）：
- 抖音 / 小红书 / 视频文件三种能力是 AI-Media2Doc 的**平等兄弟模块**，不是"主项目 + 外部依赖"
- XHS-Downloader 是小红书能力的**底层引擎**（地位同阿里百炼 ASR API、飞书 API）
- 所有代码物理上都在 `~/Documents/GitHub/AI-Media2Doc/` 一个目录内

**代码改动清单**：
- 新建 `src/processors/xhs_downloader.py`（薄客户端，调 xhs-api 5556）
- 新建 `src/processors/douyin_downloader.py`（抖音逻辑从 downloader.py 抽出，行为 100% 不变）
- 重构 `src/processors/downloader.py` 为路由分发入口 + 共享 `VideoMeta` / `extract_audio()`
- `src/utils/helper.py` 加 `is_xiaohongshu_url`（支持 xiaohongshu.com 长链 + xhslink.com 短链）
- `src/main.py` line 188/199/202 URL 路由扩展 + 抖音特化查重保留
- `.env` 加 `XHS_API_URL=http://127.0.0.1:5556`

**环境改动**：
- 用 uv 装 Python 3.12.11（独立位置，不污染 brew）
- 创独立 venv `~/.venvs/xhs-downloader/`（28 个依赖）
- git clone XHS-Downloader 到 `external/XHS-Downloader/`（12MB，独立 git）
- PM2 新增 `xhs-api` 进程（端口 5556，常驻 + 已 save）

**端到端验证**（用户飞书发 `xhslink.com/o/75yjzWInAwU`）：
```
11:25:29  飞书 ws 收到消息
11:25:32  xhs_downloader 开始（间隔 2s）
11:25:47  → xhs-api 返回 mp4 URL（15s）
11:25:58  → ffmpeg 提音频完成「从小白到销冠，怎么做需求挖掘」
11:26:40  → ASR 完成（42s, 1016 字）
11:27:06  → Qwen 2 号改写完成（24s）
11:27:08  → 飞书文档创建 Rj7mdq4KKo74HXxhFbYcvplMngf
11:27:10  → 多维表格写入（点赞 2314 / 收藏 3081）
11:27:11  → 飞书 IM 卡片
11:28:12  → Claude 1 号改写插入文档（61s）
总耗时：~3 分钟
```

**小红书数字字段解析验证**：`_parse_count("5.8万") → 58000` / `_parse_count("2.5万") → 25000` / `_parse_count("1862") → 1862`，工作正确。

**已知短板**：xhs API 不返回 `duration` 字段 → 多维表格「视频时长」列对小红书为空（非阻塞，未来可用 ffprobe 补全）。

### 2026-05-13 · discover 模块 Phase 2 实施完成（8 文件 + 端到端集成）

**目标闭环**：cron 8 点 + watcher 60min 轮询 + 飞书 `/发现 <关键词>` 命令——5 维筛选 + 阈值分流自动改写 + 候选清单飞书表。

**代码改动**（`src/processors/discover/` 全部 8 个核心文件 + main.py 接入）：
- `engine.py` (510 行)：重写 MediaCrawler jsonl 模式（替代不稳定的 xhs-cli subprocess）；统一 CandidateRaw schema；保留 douyin-downloader 账号订阅 + dy-cli trending 热搜词
- `filters.py` (76 行)：5 维筛选（关键词/账号/话题/平台热门/叠加）；热搜+热门源直通
- `scorer.py` (76 行)：打分 0-100（互动 40 + 关键词 30 + 账号 15 + 话题 10 + 新鲜度 5）；阈值分流 auto_rewrite / candidate / drop
- `dedupe.py` (89 行)：SQLite `data/discover/dedupe.db` 持久化已发现 ID；30 天 TTL
- `candidates.py` (180 行)：候选清单飞书表 CRUD（insert/update_status/find_by_note_id/daily_count）
- `cron.py` (155 行)：PM2 `--cron-restart "0 8 * * *"` 入口，主流程编排
- `watcher.py` (138 行)：常驻轮询订阅账号（默认 60min），增量发现新作品
- `command.py` (108 行)：飞书群 `/发现 <关键词>` 命令，异步执行 + 飞书回执
- `src/main.py::on_message`：line 198 注入 `handle_discover_command` 拦截（命中即 return，不走下游 URL 路由）

**架构决策**：
- W1 路径——MediaCrawler 用独立 Playwright Chromium，cookies 不被用户主浏览器轮换影响
- HTTP API arm's length 隔离 GPL（external/MediaCrawler/.gitignore）
- 单 source 失败只 log 不抛——cron 整体跑稳，单个引擎挂不影响其他维度

**PM2 进程清单**：
- `ai-media2doc-main` (业务主进程 + ws 接 /发现 命令)
- `xhs-api` (XHS-Downloader API server, 5556)
- `discover-cron` (cron 8:00 调度)
- `discover-watcher` (常驻 60min 轮询)

**飞书资源**：
- 候选清单多维表格：`https://rwnb5yzunf4.feishu.cn/base/Nd1jb8sIraN9e0sRkL0cEYBVnyQ`（18 字段）

**端到端验证**：
- 8 个模块 import 全部成功（CandidateRaw / fetch_all / filter_candidate / score / classify / is_duplicate / insert_candidate / cron.run / handle_discover_command 符号全可调）
- ai-media2doc-main 重启后 ws 心跳正常（conn_id=7638333357437553641，无 ImportError）
- pm2 list 4 个 discover 相关进程 online + pm2 save 持久化

**已知约束 / 维护 SOP**：
- 小红书 cookies 会被服务端**周期性轮换**——MediaCrawler 用独立浏览器持久化但仍会定期失效；当 cron 失败时用户需重扫小红书 QR 码
- 抖音 search 反爬比详情严，MediaCrawler 是已验证唯一可用开源路径
- auto_rewrite 候选当前仅写入候选清单 + 状态标记 `auto_rewrite`，由后续 worker 触发 main.py.process()（独立 task）

**未做的（明确 backlog）**：
- auto_rewrite worker（候选状态=auto_rewrite → 调 process 改写流程）
- watcher 失败告警（cookies 过期主动飞书通知）
- PM2 cron 实际跑通真业务证据（需小红书 cookies 重扫码后测）

---

### 2026-05-13 · discover 模块 Phase 3 缺口收口（auto_rewrite worker + 告警 + 一键重扫）

**触发**：用户「先把缺口都补了」——Phase 2 的 3 个明确 backlog 全部交付。

**变更清单（6 改 + 2 新）**：

- **NEW** `src/processors/discover/auto_rewrite_worker.py`（~160 行）
  - PM2 cron `*/5 * * * *` `--no-autorestart`：拉单 → 调 `main.process()` → 更新候选状态
  - `MAX_PER_RUN=3` 限频，保护 Claude/Qwen 配额（一条 6-10 分钟）
  - 飞书通知拉单/成功/失败/无链接/已有文档 5 维 stats
  - 延迟 import `from src.main import process` 避免 PM2 启动报错（main.py 有 `if __name__ == "__main__":` guard）

- **NEW** `scripts/discover_login_xhs.sh`（可执行）
  - 一键重扫小红书 cookies：临时改 `base_config.py` 为 `LOGIN_TYPE=qrcode` + `HEADLESS=False`，跑 MediaCrawler 弹二维码，扫完恢复
  - `trap restore EXIT INT TERM` 保证配置不残留
  - cookies 告警飞书消息直接指向这条命令

- **MOD** `src/processors/discover/engine.py`：
  - 新增 `_alert_cookies_failure(platform, context)`：6 小时去重窗口（marker 文件 `data/discover/.cookies_fail_<platform>`），通过 cron.notify_feishu 发飞书
  - 在 `fetch_xhs_search` / `fetch_dy_search` / `fetch_xhs_user_posts` 子进程失败或 0 条返回时调用

- **MOD** `src/processors/discover/candidates.py`：
  - 新增 4 状态常量：`STATUS_PENDING="待审"` / `STATUS_AUTO_REWRITE="auto_rewrite"` / `STATUS_REWRITTEN="已改写"` / `STATUS_FAILED="失败"`
  - `insert_candidate` 增 5th 参数 `classification`（默认 `candidate`）→ classification == "auto_rewrite" 时写入状态为 `auto_rewrite`，否则 `待审`
  - 新增 `list_auto_rewrite_pending(limit=5) -> List[dict]`：供 worker 拉单

- **MOD** `src/processors/discover/cron.py` / `watcher.py` / `command.py`：3 个调用点全部传 `classification=` kwarg 给 `insert_candidate`

- **MOD** 飞书候选清单"状态"字段：通过 API PUT 新增选项 `auto_rewrite`（原 4 选项扩为 5：待审 / 已改写 / 已忽略 / 失败 / auto_rewrite）

**端到端验证证据**：
```
$ python -c "from src.processors.discover import auto_rewrite_worker; r=auto_rewrite_worker.run_once(); print(r)"
[rewriter] === 启动一轮 ===
[rewriter] 无待改写记录，本轮退出
{'pulled': 0, 'skipped_has_doc': 0, 'success': 0, 'failed': 0, 'no_url': 0}

$ pm2 list | grep discover
discover-cron      stopped (cron 模式)
discover-rewriter  online (cron */5 *)
discover-watcher   online (60min poll)
```

**PM2 process 拓扑**：3 个 discover 进程（cron 8:00 一次性、watcher 60 分钟轮询、rewriter 每 5 分钟拉一次单）

**状态机**：
- 命中 auto_rewrite 阈值 → `状态="auto_rewrite"` → rewriter 5 分钟内拉单 → process() → `已改写`/`失败`
- 未命中 auto_rewrite 阈值 → `状态="待审"` → 人工筛选 → `已改写`/`已忽略`

**Phase 2 backlog 清账**：
- ✅ auto_rewrite worker
- ✅ cookies 失效告警
- ⚠️ PM2 cron 真业务证据 - rewriter 因当前候选清单无 auto_rewrite 状态记录，pulled=0；待 watcher 抓到首条命中阈值的候选后自动验证

**已知约束**：
- 小红书 cookies 仍需用户手动扫码刷新（无自动登录），告警会指引重扫
- rewriter 假设 main.process() 内部已写飞书业务表，不在候选清单存 doc_url（避免双源不同步）

---

### 2026-05-13 · 小红书 xsec_token bug 根因复盘（discover 自动改写全失败的真根因）

**触发**：用户问"小红书这块开发跑偏了，还是什么原因导致瘫痪"。

**症状**：
- discover 自动改写 worker 跑所有小红书 URL → xhs-api 返回"获取小红书作品数据失败"
- 5 条 auto_rewrite 小红书候选全 fail
- discover 候选清单的 xhs URL 实际**全部不可消费**

**RCA 5-Why**：
| Why | 答案 |
|-----|------|
| 1. 为什么 worker 跑 xhs 全失败？| xhs-api 返回失败 |
| 2. 为什么 xhs-api 返回失败？| URL 缺 `xsec_token` query string |
| 3. 为什么缺 token？| `engine.py:_parse_xhs_note` 拼 URL 时丢字段 |
| 4. 为什么没用 token？| 实施时只取 note_id 拼裸 URL，未读 xsec_token 字段 |
| 5. 为什么没测出来？| 5/10 端到端验证用的是**短链** `xhslink.com/o/...`（XHS-Downloader 内部自动 resolve）；discover 5/11 加的自动拼**长链路径从未真测** |

**实测证据**：
```
✅ xhslink.com/o/75yjzWInAwU（短链 5/10 跑通）→ 现在仍 18 字段成功
✅ 长链含 xsec_token → 18 字段成功
❌ 长链不含 xsec_token → "获取小红书作品数据失败"
```

**结论**：
- ✅ **5/10 选 XHS-Downloader 决策正确**（11.1k⭐ + 5 种接口 + 维护活跃）
- ❌ **5/11 discover 实施跑偏 1 行代码**：`engine.py:266 url=f"https://www.xiaohongshu.com/explore/{note_id}"` 丢失 jsonl 提供的 `xsec_token`

**变更清单（1 修 + 1 清 + 1 文档）**：

- **MOD** `src/processors/discover/engine.py:_parse_xhs_note`：
  - 拼 URL 时读取 `raw.get("xsec_token")`，存在则带入 query string
  - 同时存入 `source_meta["xsec_token"]` 便于后续调试
- **DATA** 飞书候选清单批量删除 57 条小红书无 token 历史脏数据
- **DOC** 本条 PRD 演进史

**架构层固化的约束（避免未来再踩）**：
- ❌ 不要直接 `f"https://www.xiaohongshu.com/explore/{note_id}"` 拼 URL
- ✅ 必须带 `xsec_token` query string（XHS-Downloader README 明确要求）
- ✅ MediaCrawler jsonl 字段 `xsec_token` 是契约的一部分

**遗留 backlog**：
- ⚠️ MediaCrawler xhs.search 当前 tenacity.RetryError（cookies 失效）
- 用户需手动跑 `bash scripts/discover_login_xhs.sh` 重扫小红书 cookies
- 重扫后 cron 拉到的 xhs URL 都会带 token（修复已生效），worker auto_rewrite 即可跑通

---

### 2026-05-13 · discover 业务流程闭环（4 道断点全修 + 用户 SOP）

**触发**：用户「全修」——业务流程实测断在 4 处，候选清单 35 条全废。

**4 道断点真相（修复前实测数据）**：

| # | 断点 | 实测证据 | 修法 |
|---|------|---------|------|
| 1 | engine.py:507 字段污染 | `like_count=1088 万`（实为热搜 hot_value） | `like_count=0`，hot_value 仅保留在 source_meta |
| 2 | trending_word 污染候选清单 | 30/30 候选全是热搜词非视频 | scorer.classify → `CLASS_DROP`；cron.py 单独发飞书热搜情报消息（不入表） |
| 3 | status 字段未写入 | 35 条 record 拉 status 全空 → worker 拉单 0 命中 | candidates.py 早已修；35 条历史脏数据通过 `batch_delete` 清空 |
| 4 | 候选清单无人工触发 UI | type=3001 按钮字段缺失 | 务实方案：状态字段新增 `📝 待改写` 选项，用户下拉选中即触发 worker |

**变更清单（4 改 + 0 新）**：

- **MOD** `src/processors/discover/engine.py:507`：trending_word 的 like_count 改为 0
- **MOD** `src/processors/discover/scorer.py`：trending_word → CLASS_DROP（不入表）
- **MOD** `src/processors/discover/cron.py:run`：拉热搜词后直接 notify_feishu 发"🔥 抖音今日热搜词（情报参考·不入候选清单）"消息
- **MOD** `src/processors/discover/candidates.py`：新增 STATUS_USER_REQUESTED / STATUS_IGNORED 常量；`list_auto_rewrite_pending` 同时查 `auto_rewrite` 和 `📝 待改写` 两个状态
- **MOD** 飞书"状态"字段：通过 API PUT 加 `📝 待改写` 选项（原 5 选项 → 6：待审 / 已改写 / 已忽略 / 失败 / auto_rewrite / 📝 待改写）
- **DATA** 飞书候选清单 35 条历史脏数据全删 + dedupe.db 30 条记录清空（下次 cron 重新写）

**业务流程闭环图（终态）**：

```
discover cron 8:00 / watcher 60min / 飞书 /发现 命令
    ↓
fetch_all 拉 5 维数据（关键词/账号/话题/热门）
    ↓
热搜词（trending_word）── 单独发飞书消息（情报参考，不入表）
其他真视频候选 ↓
filter_batch + scorer.classify
    ├─→ auto_rewrite 阈值（xhs 50K+10K / 抖音 200K）→ 状态=auto_rewrite ──┐
    ├─→ candidate（命中维度但未达阈值）→ 状态=待审                       │
    └─→ drop（连 candidate_only 阈值都不到）→ 不入表                     │
                                                                          │
[用户在飞书表浏览候选清单]                                                │
    │                                                                     │
    └─→ 把感兴趣的 行状态 改成 "📝 待改写" ────────────────────────────┐  │
                                                                       │  │
                                                                       ▼  ▼
                                              auto_rewrite_worker (PM2 cron */5)
                                                  ↓
                                              list_auto_rewrite_pending
                                                  ↓ MAX_PER_RUN=3
                                              main.process(parsed)
                                                  ├─→ 飞书文档 + 业务多维表 + IM 卡片
                                                  └─→ 更新候选清单状态：已改写 / 失败
```

**用户操作 SOP**：

1. **每天早晨**：飞书群收到两条消息
   - "🔥 抖音今日热搜词" — 情报参考，不操作
   - "🔍 discover/cron 完成" — 看候选清单条数
2. **每天定期**（建议午后）：打开候选清单飞书表 `https://rwnb5yzunf4.feishu.cn/base/Nd1jb8sIraN9e0sRkL0cEYBVnyQ`
3. **筛选**：按"分数"排序，看标题 + 平台 + 点赞数 + 命中关键词 / 账号 / 话题
4. **触发改写**：在感兴趣行的"状态"列下拉，选 `📝 待改写`
5. **等待 5 分钟**：discover-rewriter 拉单，跑 main.process()，飞书群收到完整改写文档
6. **跳过**：不想改写的，状态选 `已忽略`

**端到端验证证据**：
```
$ python -c "from src.processors.discover import auto_rewrite_worker, candidates"
$ scorer.classify(trending_word, ...) → 'drop'  ✓
$ candidates.STATUS_USER_REQUESTED → '📝 待改写'  ✓
$ 写一条 fake 候选 status='📝 待改写' → list_auto_rewrite_pending 拉到 1 条  ✓
$ 飞书表 35 → 0（清理）→ 0（dedupe.db）  ✓
$ PM2 discover-watcher / discover-rewriter 重启 online ✓
```

**状态机（最终版）**：

```
                                    ┌─→ 已改写
状态=auto_rewrite ──worker拉─→ process ┤
                                    └─→ 失败
                                    ┌─→ 已改写
状态=📝 待改写  ──worker拉─→ process ┤
                                    └─→ 失败

状态=待审 ──user→ 📝 待改写 / 已忽略
```

**断点全修后 backlog**：
- ⚠️ 小红书 cookies 仍需手动扫码（设计层无解，告警 + 一键脚本已就位）
- ⚠️ 抖音订阅账号 / 小红书账号配置（`config/discover.yaml::accounts` 还是空，用户需手动加 user_id/sec_uid）

---

### 2026-05-10 · Step 7（1 号机器人）体验改进
- **问题**：2 号改写完成后到 1 号完成的 1 分钟内，飞书群没有任何消息，用户以为系统挂了
- **改动**：`src/main.py` `_claude_rewrite` 闭包内
  - 启动时追加：`msg_handler.send_text(chat_id, "🤖 1号小助理开始改写，预计 1 分钟左右...")`
  - 异常时追加：`msg_handler.send_text(chat_id, f"❌ 1号小助理改写失败：{str(e)[:100]}")`（之前只 log 不发群）
- **改后体验**：T+0「2 号已完成」→ T+3s「1 号开始」→ T+60s「1 号已完成」 三档群消息覆盖全程，用户全程有感知

---

## 12. 扩展指南 · 加新平台（B 站 / 快手 / 视频号 / Twitter）

### 5 步 SOP

```
[Step 1] URL 识别
  src/utils/helper.py 加：
    def is_bilibili_url(url) -> bool: ...

[Step 2] 选下载引擎
  - 优先 yt-dlp（已支持 B 站）→ 不用新外部依赖
  - 否则找 GitHub 顶级开源项目（>1k star + 半年内更新）
  - 引擎放 external/<NAME>/，加 .gitignore（如已加 external/ 则自动覆盖）
  - 装独立 venv（如需特殊 Python 版本）+ PM2 启动 API server

[Step 3] 写 platform-specific downloader
  src/processors/<platform>_downloader.py：
    def download(url: str) -> VideoMeta:
        # 调引擎 → 拿视频 URL → 下载 mp4 → ffmpeg 提音频
        # 返回 VideoMeta(必含字段)

[Step 4] 路由分发
  src/processors/downloader.py::download() 加分支：
    elif is_bilibili_url(url): return bilibili_downloader.download(url)
  src/main.py::on_message() 加路由：
    if url and (is_douyin_url(url) or is_xiaohongshu_url(url) or is_bilibili_url(url)):

[Step 5] 测试
  - 单元：is_bilibili_url 各种 URL 形态
  - 端到端：飞书发条 B 站链接 → 全链路
  - 灰度：观察 1 周稳定后写入 PRD 演进历史
```

### 关键约束（防止架构腐烂）

- ❌ 不要在 main.py 里写平台特定逻辑（必须封装在 platform_downloader.py）
- ❌ 不要让 platform_downloader 直接写飞书 / 多维表格（违反单一职责）
- ❌ 新引擎必须**外部隔离**（不直接 import GPL/AGPL 代码到 src/ 内）
- ✅ VideoMeta 是平台间唯一共契约，新平台必须填齐必填字段
- ✅ 新增 .env 字段必须更新本 PRD 的 6.1 章节

---

## 附录 A · 常用命令速查

```bash
# 健康检查
pm2 list
pm2 logs ai-media2doc-main --lines 30 --nostream
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5556/docs

# 日志
tail -f ~/Documents/GitHub/AI-Media2Doc/logs/app.log
pm2 logs xhs-api --lines 50 --nostream

# 重启
pm2 restart ai-media2doc-main
pm2 restart xhs-api

# 切回 NAS
pm2 stop ai-media2doc-main && pm2 stop xhs-api
# → 9999 UI 启动 NAS 容器

# 清理临时文件
rm -rf /tmp/media2doc/*

# 测试小红书
curl -sS -X POST http://127.0.0.1:5556/xhs/detail \
  -H "Content-Type: application/json" \
  -d '{"url": "<xhslink_url>", "download": false}' | python3 -m json.tool
```

## 附录 B · 联系点

- 项目主代码：`~/Documents/GitHub/AI-Media2Doc/`
- 引擎源码：`~/Documents/GitHub/AI-Media2Doc/external/XHS-Downloader/`
- PM2 dump：`~/.pm2/dump.pm2`
- venv：`~/.venvs/media2doc/` + `~/.venvs/xhs-downloader/`
- 日志：`logs/app.log` + `~/.pm2/logs/`

---

**文档维护**：每次架构变更 / 加平台 / 改部署，必须更新对应章节 + 在第 11 章「演进历史」追加一条。
