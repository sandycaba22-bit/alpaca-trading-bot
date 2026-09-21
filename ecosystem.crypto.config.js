/**
 * PM2 — bot cripto (paper) + panel :3001
 * Usar: pm2 start ecosystem.crypto.config.js
 * Setup auto: bash deploy/setup-split.sh
 */
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const PYTHON_WIN = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const PYTHON_NIX = path.join(ROOT, '.venv', 'bin', 'python');
const PYTHON = fs.existsSync(PYTHON_WIN)
  ? PYTHON_WIN
  : fs.existsSync(PYTHON_NIX)
    ? PYTHON_NIX
    : 'python3';
const LOG_DIR = path.join(ROOT, 'logs');
const LOCAL_DISABLED = fs.existsSync(path.join(ROOT, 'data', 'LOCAL_DISABLED'));

module.exports = {
  apps: LOCAL_DISABLED ? [] : [
    {
      name: 'trading-web-crypto',
      cwd: path.join(ROOT, 'web'),
      script: 'server.js',
      interpreter: 'node',
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: '10s',
      restart_delay: 3000,
      exp_backoff_restart_delay: 1000,
      env: {
        NODE_ENV: 'production',
        HOST: '0.0.0.0',
        PORT: '3001',
        ENV_FILE: '.env.crypto',
        PM2_APP_NAME: 'trading-bot-crypto',
        DATA_DIR: 'data-crypto',
      },
      error_file: path.join(LOG_DIR, 'pm2-web-crypto-error.log'),
      out_file: path.join(LOG_DIR, 'pm2-web-crypto-out.log'),
      merge_logs: true,
      time: true,
    },
    {
      name: 'trading-bot-crypto',
      cwd: ROOT,
      script: 'run_bot.py',
      interpreter: PYTHON,
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: '15s',
      restart_delay: 8000,
      exp_backoff_restart_delay: 2000,
      kill_timeout: 20000,
      env: {
        PYTHONUNBUFFERED: '1',
        BOT_STARTUP_SOURCE: 'pm2',
        ENV_FILE: '.env.crypto',
      },
      error_file: path.join(LOG_DIR, 'pm2-bot-crypto-error.log'),
      out_file: path.join(LOG_DIR, 'pm2-bot-crypto-out.log'),
      merge_logs: true,
      time: true,
    },
  ],
};
