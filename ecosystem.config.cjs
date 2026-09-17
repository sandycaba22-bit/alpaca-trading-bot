/**
 * PM2 — procesos permanentes del bot Alpaca (panel + motor).
 *
 * Uso (desde la raíz del repo):
 *   pm2 start ecosystem.config.cjs
 *   pm2 save
 *   pm2 status
 *
 * Windows (arranque al encender el PC):
 *   npm install -g pm2-windows-startup
 *   pm2-startup install
 *   pm2 save
 *
 * Si existe data/LOCAL_DISABLED, PM2 no arranca nada en esta máquina.
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
      name: 'trading-web',
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
        PORT: '3000',
      },
      error_file: path.join(LOG_DIR, 'pm2-web-error.log'),
      out_file: path.join(LOG_DIR, 'pm2-web-out.log'),
      merge_logs: true,
      time: true,
    },
    {
      name: 'trading-bot',
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
      // El bot escribe su propio PID; PM2 lo reinicia si se cae
      kill_timeout: 20000,
      env: {
        PYTHONUNBUFFERED: '1',
        BOT_STARTUP_SOURCE: 'pm2',
      },
      error_file: path.join(LOG_DIR, 'pm2-bot-error.log'),
      out_file: path.join(LOG_DIR, 'pm2-bot-out.log'),
      merge_logs: true,
      time: true,
    },
  ],
};
