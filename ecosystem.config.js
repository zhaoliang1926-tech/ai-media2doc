// ai-media2doc · PM2 守护配置
//
// 启动：pm2 start ecosystem.config.js
// 状态：pm2 status
// 日志：pm2 logs ai-media2doc-main
// 重启：pm2 restart ai-media2doc-main
// 停止：pm2 stop ai-media2doc-main
// 开机自启：pm2 startup && pm2 save
//
// 设计原则（参照 wechat-pipeline ecosystem.config.js）：
//   · PM2 不自动读 .env → 用本文件的 loadDotenv() 注入到 env block
//   · 朋友机器只要 cp .env.example .env + 填值 + pm2 reload 即可生效
//   · 自动 rotation: 防止 logs/ 撑爆磁盘
//   · stable 模式：autorestart=true + max_restarts=10 + min_uptime=60s

const path = require('path');
const fs = require('fs');
const PROJECT_ROOT = __dirname;

// ─── .env 加载器（修长期 bug：PM2 不自动读 .env）──────
function loadDotenv() {
  const env = {};
  const envPath = path.join(__dirname, '.env');
  if (!fs.existsSync(envPath)) return env;
  for (const line of fs.readFileSync(envPath, 'utf-8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const idx = trimmed.indexOf('=');
    if (idx === -1) continue;
    const k = trimmed.slice(0, idx).trim();
    const v = trimmed.slice(idx + 1).trim().replace(/^["']|["']$/g, '');
    if (k) env[k] = v;
  }
  return env;
}
const DOTENV = loadDotenv();

module.exports = {
  apps: [
    {
      name: 'ai-media2doc-main',
      script: 'src/main.py',
      interpreter: 'python3',
      namespace: 'ai-media2doc',
      cwd: PROJECT_ROOT,

      // ─── 日志 ──────────────────────────────────────────
      log_file:   path.join(PROJECT_ROOT, 'logs/main.log'),
      error_file: path.join(PROJECT_ROOT, 'logs/main.err'),
      out_file:   path.join(PROJECT_ROOT, 'logs/main.out'),
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      merge_logs: true,
      time: true,

      // ─── 自愈 ──────────────────────────────────────────
      autorestart: true,
      max_restarts: 10,
      min_uptime: '60s',
      restart_delay: 4000,
      max_memory_restart: '500M',

      // ─── 环境 ──────────────────────────────────────────
      env: {
        PYTHONUNBUFFERED: '1',
        PYTHONPATH: PROJECT_ROOT,
        TZ: 'Asia/Shanghai',
        ...DOTENV,
      },
      env_production: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
        PYTHONPATH: PROJECT_ROOT,
        TZ: 'Asia/Shanghai',
        ...DOTENV,
      },

      // ─── 监控 ──────────────────────────────────────────
      watch: false,
      ignore_watch: ['logs', 'tmp', 'data', '.venv', 'external', 'node_modules'],
    },
  ],
};

// 防御：script 文件不存在时跳过该 app（避免 pm2 start 报 Script not found）
module.exports.apps = module.exports.apps.filter((app) => {
  const exists = fs.existsSync(path.join(__dirname, app.script));
  if (!exists) {
    console.log(`[ecosystem] 跳过 ${app.name}: ${app.script} 不存在`);
  }
  return exists;
});
