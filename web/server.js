const crypto = require('crypto');
const fs = require('fs');
const { spawn } = require('child_process');
const express = require('express');
const session = require('express-session');
const path = require('path');
const { createBotState } = require('./bot_state');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

function normalizeEnvValue(raw) {
    let value = String(raw || '').trim();
    if (
        (value.startsWith('"') && value.endsWith('"'))
        || (value.startsWith("'") && value.endsWith("'"))
    ) {
        value = value.slice(1, -1);
    }
    return value;
}

const app = express();
const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || '0.0.0.0';
const ROOT = path.join(__dirname, '..');
if (fs.existsSync(path.join(ROOT, 'data', 'LOCAL_DISABLED'))) {
    console.error('Panel local deshabilitado de forma permanente (data/LOCAL_DISABLED)');
    process.exit(1);
}
const PUBLIC_DIR = path.join(__dirname, 'public');
const PRIVATE_DIR = path.join(__dirname, 'private');

const SESSION_SECRET = process.env.SESSION_SECRET;
const ADMIN_USER = normalizeEnvValue(process.env.ADMIN_USER);
const ADMIN_PASS = normalizeEnvValue(process.env.ADMIN_PASS);

if (!SESSION_SECRET || SESSION_SECRET === 'change_me_to_a_long_random_string') {
    console.error('Falta SESSION_SECRET en .env');
    process.exit(1);
}
if (!ADMIN_USER || !ADMIN_PASS || ADMIN_PASS === 'change_me') {
    console.error('Configura ADMIN_USER y ADMIN_PASS en .env');
    process.exit(1);
}

app.use(express.urlencoded({ extended: false }));
app.use(express.json({ limit: '16kb' }));

// Panel local en http://127.0.0.1 — NO usar cookie Secure solo por NODE_ENV=production (PM2),
// o el navegador descarta la sesión y el login parece fallar (formulario limpio + rechazo).
const cookieSecure = process.env.COOKIE_SECURE === 'true';

app.use(session({
    name: 'bot.sid',
    secret: SESSION_SECRET,
    resave: false,
    saveUninitialized: false,
    cookie: {
        httpOnly: true,
        sameSite: 'lax',
        secure: cookieSecure,
        maxAge: 1000 * 60 * 60 * 2
    }
}));

const loginAttempts = new Map();

function clientIp(req) {
    return String(req.ip || req.socket.remoteAddress || 'unknown').slice(0, 64);
}

function isRateLimited(ip) {
    const now = Date.now();
    const windowMs = 15 * 60 * 1000;
    const row = loginAttempts.get(ip);
    if (!row) {
        return false;
    }
    if (now - row.start > windowMs) {
        loginAttempts.delete(ip);
        return false;
    }
    return row.count > 8;
}

function recordFailedLogin(ip) {
    const now = Date.now();
    const windowMs = 15 * 60 * 1000;
    const row = loginAttempts.get(ip) || { count: 0, start: now };
    if (now - row.start > windowMs) {
        loginAttempts.set(ip, { count: 1, start: now });
        return;
    }
    row.count += 1;
    loginAttempts.set(ip, row);
}

function clearLoginAttempts(ip) {
    loginAttempts.delete(ip);
}

function safeEqual(left, right) {
    const a = Buffer.from(String(left || ''), 'utf8');
    const b = Buffer.from(String(right || ''), 'utf8');
    if (a.length !== b.length) {
        crypto.timingSafeEqual(a, Buffer.alloc(a.length));
        return false;
    }
    return crypto.timingSafeEqual(a, b);
}

function requireLogin(req, res, next) {
    if (req.session && req.session.isAdmin) {
        return next();
    }
    if (req.path.startsWith('/api/')) {
        return res.status(401).json({ error: 'No autorizado' });
    }
    return res.redirect('/login.html');
}

app.post('/api/login', (req, res) => {
    const ip = clientIp(req);
    if (isRateLimited(ip)) {
        return res.status(429).redirect('/login.html?error=1');
    }

    const username = String(req.body.username || '').trim();
    const password = String(req.body.password || '');
    const adminUser = String(ADMIN_USER || '').trim();
    const adminPass = String(ADMIN_PASS || '');
    const userOk = safeEqual(username, adminUser);
    const passOk = safeEqual(password, adminPass);

    if (userOk && passOk) {
        clearLoginAttempts(ip);
        req.session.isAdmin = true;
        // Persistir sesión antes del redirect (evita carrera: dashboard sin cookie)
        return req.session.save((err) => {
            if (err) {
                return res.redirect('/login.html?error=1');
            }
            return res.redirect('/dashboard.html');
        });
    }

    recordFailedLogin(ip);
    return res.redirect('/login.html?error=1');
});

app.get('/api/logout', (req, res) => {
    req.session.destroy(() => {
        res.clearCookie('bot.sid');
        res.redirect('/login.html');
    });
});

function pythonBin() {
    if (process.env.PYTHON_BIN) {
        return process.env.PYTHON_BIN;
    }
    const win = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
    const nix = path.join(ROOT, '.venv', 'bin', 'python');
    if (fs.existsSync(win)) {
        return win;
    }
    if (fs.existsSync(nix)) {
        return nix;
    }
    return 'python';
}

function runPython(scriptArgs, timeoutMs, callback) {
    const child = spawn(pythonBin(), scriptArgs, {
        cwd: ROOT,
        env: process.env,
        windowsHide: true
    });
    let stdout = '';
    let settled = false;
    const timer = setTimeout(() => {
        if (!settled) {
            settled = true;
            child.kill();
            callback(new Error('timeout'), null);
        }
    }, timeoutMs);
    child.stdout.on('data', (chunk) => {
        stdout += chunk.toString('utf8');
        if (stdout.length > 200000) {
            child.kill();
        }
    });
    child.stderr.on('data', () => {});
    child.on('close', (code) => {
        if (settled) {
            return;
        }
        settled = true;
        clearTimeout(timer);
        callback(code === 0 ? null : new Error('python_exit'), stdout);
    });
}

const botState = createBotState(ROOT, pythonBin);

app.get('/api/bot/status', requireLogin, (req, res) => {
    try {
        return res.json(botState.readState());
    } catch {
        return res.status(500).json({ error: 'No se pudo leer el estado del bot' });
    }
});

app.get('/api/status', requireLogin, (req, res) => {
    const child = spawn(pythonBin(), [path.join(__dirname, 'status.py')], {
        cwd: ROOT,
        env: process.env,
        windowsHide: true
    });

    let stdout = '';
    let settled = false;
    const timer = setTimeout(() => {
        if (!settled) {
            settled = true;
            child.kill();
            res.status(504).json({ error: 'Tiempo de espera agotado' });
        }
    }, 20000);

    child.stdout.on('data', (chunk) => {
        stdout += chunk.toString('utf8');
        if (stdout.length > 200000) {
            child.kill();
        }
    });
    child.stderr.on('data', () => {});
    child.on('close', (code) => {
        if (settled) {
            return;
        }
        settled = true;
        clearTimeout(timer);
        try {
            const data = JSON.parse(stdout || '{}');
            if (code !== 0 || data.error) {
                return res.status(502).json({ error: 'No se pudo leer el estado' });
            }
            return res.json(data);
        } catch {
            return res.status(502).json({ error: 'Respuesta invalida del bot' });
        }
    });
});

app.get('/api/chart', requireLogin, (req, res) => {
    const symbol = String(req.query.symbol || '').toUpperCase();
    const timeframe = String(req.query.timeframe || '15Min');
    if (!/^([A-Z][A-Z0-9.]{0,9}|[A-Z]{2,10}\/USD)$/.test(symbol)) {
        return res.status(400).json({ error: 'Simbolo invalido' });
    }
    if (!['1Min', '3Min', '5Min', '6Min', '9Min', '15Min', '30Min', '1Hour', '1Day'].includes(timeframe)) {
        return res.status(400).json({ error: 'Timeframe invalido' });
    }
    runPython(
        [path.join(__dirname, 'bars.py'), '--symbol', symbol, '--timeframe', timeframe],
        25000,
        (err, stdout) => {
            if (err) {
                return res.status(502).json({ error: 'No se pudieron obtener las velas' });
            }
            try {
                const data = JSON.parse(stdout || '{}');
                if (data.error) {
                    return res.status(502).json({ error: 'No se pudieron obtener las velas' });
                }
                return res.json(data);
            } catch {
                return res.status(502).json({ error: 'Respuesta invalida del grafico' });
            }
        }
    );
});

app.post('/api/bot', requireLogin, async (req, res) => {
    const action = String((req.body && req.body.action) || '');
    if (!['pause', 'resume', 'start', 'stop'].includes(action)) {
        return res.status(400).json({ error: 'Accion no permitida' });
    }
    try {
        let state;
        if (action === 'start' || action === 'resume') {
            state = await botState.start();
        } else {
            state = await botState.stop();
        }
        return res.json({ ok: true, bot: state });
    } catch (err) {
        console.error('POST /api/bot error:', err.message);
        return res.status(500).json({ error: 'No se pudo actualizar el estado del bot' });
    }
});

app.get('/dashboard.html', requireLogin, (req, res) => {
    res.sendFile(path.join(PRIVATE_DIR, 'dashboard.html'));
});

app.get('/', (req, res) => {
    if (req.session && req.session.isAdmin) {
        return res.redirect('/dashboard.html');
    }
    return res.redirect('/login.html');
});

app.use(express.static(PUBLIC_DIR, {
    index: 'login.html',
    extensions: ['html']
}));

app.listen(PORT, HOST, () => {
    const shown = HOST === '0.0.0.0' ? '127.0.0.1' : HOST;
    console.log(`Servidor del panel en http://${shown}:${PORT} (bind ${HOST})`);
});
