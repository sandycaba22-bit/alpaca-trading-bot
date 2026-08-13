const crypto = require('crypto');
const { spawn } = require('child_process');
const express = require('express');
const session = require('express-session');
const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

const app = express();
const PORT = process.env.PORT || 3000;
const ROOT = path.join(__dirname, '..');
const PUBLIC_DIR = path.join(__dirname, 'public');
const PRIVATE_DIR = path.join(__dirname, 'private');

const SESSION_SECRET = process.env.SESSION_SECRET;
const ADMIN_USER = process.env.ADMIN_USER;
const ADMIN_PASS = process.env.ADMIN_PASS;

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

app.use(session({
    secret: SESSION_SECRET,
    resave: false,
    saveUninitialized: false,
    cookie: {
        httpOnly: true,
        sameSite: 'lax',
        secure: process.env.NODE_ENV === 'production',
        maxAge: 1000 * 60 * 60 * 2
    }
}));

const loginAttempts = new Map();

function clientIp(req) {
    return String(req.ip || req.socket.remoteAddress || 'unknown').slice(0, 64);
}

function tooManyLogins(ip) {
    const now = Date.now();
    const windowMs = 15 * 60 * 1000;
    const row = loginAttempts.get(ip) || { count: 0, start: now };
    if (now - row.start > windowMs) {
        loginAttempts.set(ip, { count: 1, start: now });
        return false;
    }
    row.count += 1;
    loginAttempts.set(ip, row);
    return row.count > 8;
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
    if (tooManyLogins(ip)) {
        return res.status(429).redirect('/login.html?error=1');
    }

    const username = String(req.body.username || '');
    const password = String(req.body.password || '');
    const userOk = safeEqual(username, ADMIN_USER);
    const passOk = safeEqual(password, ADMIN_PASS);

    if (userOk && passOk) {
        req.session.isAdmin = true;
        return res.redirect('/dashboard.html');
    }
    return res.redirect('/login.html?error=1');
});

app.get('/api/logout', (req, res) => {
    req.session.destroy(() => {
        res.redirect('/login.html');
    });
});

app.get('/api/status', requireLogin, (req, res) => {
    const python = process.env.PYTHON_BIN
        || path.join(ROOT, '.venv', 'Scripts', 'python.exe');
    const child = spawn(python, [path.join(__dirname, 'status.py')], {
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

app.listen(PORT, () => {
    console.log(`Servidor del panel en http://127.0.0.1:${PORT}`);
});
