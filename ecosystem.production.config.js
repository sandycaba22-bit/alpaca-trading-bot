/**
 * PM2 — producción VPS: bot élite (:3000) + Top 50 paper (:3001)
 *
 *   pm2 start ecosystem.production.config.js
 *   pm2 save
 *
 * Requiere: .env.stocks (live) y .env.stocks_top50 (paper)
 */
const stocks = require('./ecosystem.stocks.config.js');
const top50 = require('./ecosystem.stocks_top50.config.js');

module.exports = {
  apps: [...(stocks.apps || []), ...(top50.apps || [])],
};
