/**
 * PM2 — Crypto Night Fortress (caja separada; NO mezclar con stocks/Top50)
 * pm2 start ecosystem.crypto_night.config.js
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
      name: 'crypto-night',
      cwd: ROOT,
      script: 'run_crypto_night.py',
      interpreter: PYTHON,
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: '15s',
      restart_delay: 8000,
      kill_timeout: 20000,
      env: {
        PYTHONUNBUFFERED: '1',
        BOT_STARTUP_SOURCE: 'pm2',
        ENV_FILE: '.env.crypto_night',
        BOT_PROFILE: 'crypto_night',
      },
      error_file: path.join(LOG_DIR, 'pm2-crypto-night-error.log'),
      out_file: path.join(LOG_DIR, 'pm2-crypto-night-out.log'),
      merge_logs: true,
      time: true,
    },
  ],
};
