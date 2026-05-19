# AI-Media2Doc · 短视频文案改写工具

飞书机器人 + 抖音下载 + AI 改写，一键将短视频转为结构化运营文案，自动存入飞书文档和多维表格。

## 功能

- 发送抖音链接或视频文件到飞书群 → 自动处理
- ASR 语音转文字（阿里百炼 Paraformer）
- AI 三步改写：拆解架构 → 提炼用户画像 → 重新创作
- 双模型改写：2号小助理（Qwen）+ 1号小助理（Claude，可选）
- 结果自动写入飞书文档 + 多维表格
- 重复视频去重提示

---

## 部署指南（绿联云 8800 Pro）

### 第一步：准备材料

**需要提前申请 / 获取的 Key：**

| 服务 | 用途 | 获取地址 |
|------|------|---------|
| 飞书开放平台 App | 接收消息、发送回复 | open.feishu.cn |
| 阿里百炼 API Key | 语音转文字 + Qwen 改写 | bailian.aliyun.com |
| Claude API Key | AI 改写（可选，有则填） | console.anthropic.com |
| 抖音 cookies.txt | 下载抖音视频 | 见下方说明 |

**获取抖音 cookies.txt：**
1. Chrome 安装插件：[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
2. 用 Chrome 登录抖音（douyin.com）
3. 点插件图标 → Export → 保存为 `douyin_cookies.txt`
4. 将文件放到项目 `config/` 目录下

---

### 第二步：配置飞书机器人

1. 进入 [飞书开放平台](https://open.feishu.cn) → 创建企业自建应用
2. **基本信息** → 记录 App ID 和 App Secret
3. **权限管理** → 开通以下权限：
   - `im.message.receive_v1`（接收消息）
   - `im.message.group_msg_all.readonly`（获取群组中所有消息）
   - `im.message:write`（发送消息）
   - `docs:doc`（创建文档）
   - `bitable:app`（操作多维表格）
   - `drive:drive`（文件夹操作）
4. **事件订阅** → 添加事件：`im.message.receive_v1`
5. **发布应用**

---

### 第三步：准备项目文件

将以下文件上传到绿联云（通过文件管理器或 SSH）：

```
/volume1/docker/ai-media2doc/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env                    ← 复制 .env.example 并填写真实值
├── src/
├── config/
│   ├── prompts.yaml
│   └── douyin_cookies.txt  ← 第一步获取的 cookies 文件
├── logs/                   ← 提前创建，用于持久化日志
└── tmp/                    ← 提前创建，用于临时文件
```

---

### 第四步：配置 .env

复制 `.env.example` 为 `.env`，填写以下必填项：

```env
# 飞书
FEISHU_APP_ID=cli_xxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxx
FEISHU_BITABLE_APP_TOKEN=xxxxxxxx
FEISHU_BITABLE_TABLE_ID=tblxxxxxxxx
FEISHU_FOLDER_TOKEN=xxxxxxxx

# 阿里百炼
DASHSCOPE_API_KEY=sk-xxxxxxxx

# 改写模型（推荐 qwen，无需额外配置；或 claude 需填 CLAUDE_API_KEY）
REWRITER_PROVIDER=qwen
QWEN_MODEL=qwen-max

# 抖音 cookies（NAS 部署用文件方式）
DOUYIN_COOKIES_FILE=/app/config/douyin_cookies.txt

# NAS 部署固定为 false
ENABLE_CLAUDE_CLI=false
```

**网络说明（绿联云双网卡）：**

docker-compose.yml 已配置 `network_mode: host`，容器共享 NAS 网络栈。
飞书、阿里百炼、抖音均为国内服务，走 LAN1（华为路由器）直接访问，无需代理。

---

### 第五步：在绿联云启动容器

**方式一：SSH 命令行（推荐）**

```bash
cd /volume1/docker/ai-media2doc
docker-compose up -d --build
```

**方式二：绿联云 Docker 管理界面**

1. 打开绿联云管理后台 → Docker → Compose
2. 上传 `docker-compose.yml`
3. 点击「部署」

**查看运行日志：**

```bash
# 实时查看
docker logs -f ai-media2doc

# 或查看日志文件
tail -f /volume1/docker/ai-media2doc/logs/app.log
```

---

### 第六步：将机器人加入飞书群

1. 在飞书创建群组
2. 群设置 → 机器人 → 添加机器人 → 选择你创建的应用
3. 向群里发送抖音链接，机器人自动响应

---

## 使用方式

| 操作 | 机器人行为 |
|------|-----------|
| 发送抖音链接（需 @ 机器人） | 自动下载、转文字、改写、存文档 |
| 直接发送视频文件 | 自动转文字、改写、存文档（无需 @） |
| 重复视频链接 | 提示已处理，展示原文档链接 |
| 回复「重新转写」 | 强制重新处理 |

---

## 多维表格字段说明

多维表格需手动创建以下字段：

| 字段名 | 类型 |
|--------|------|
| 标题 | 文本 |
| 抖音链接 | 超链接 |
| 来源账号 | 文本 |
| 改写概要 | 文本 |
| 视频分类 | 单选 |
| 月份 | 日期 |
| 处理状态 | 文本 |
| 查看文档 | 超链接 |
| 提交时间 | 日期 |
| 是否已录制 | 勾选 |
| 点赞数 | 数字 |
| 收藏数 | 数字 |
| 转发数 | 数字 |
| 评论数 | 数字 |
| 视频时长 | 数字 |
| 提交人 | 人员 |

---

## 常见问题

**Q：抖音视频下载失败？**
A：cookies 过期，重新导出 `douyin_cookies.txt` 替换后重启容器。

**Q：容器内无法访问抖音？**
A：需要配置代理，见第四步网络配置部分。

**Q：ASR 转写失败？**
A：检查 `DASHSCOPE_API_KEY` 是否正确，阿里百炼账户是否有余额。

**Q：如何更新 cookies？**
A：替换 `config/douyin_cookies.txt` 文件，无需重启容器（每次处理时实时读取）。
