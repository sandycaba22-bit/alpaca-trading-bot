/** Estado del bot: PM2 si está disponible; si no, spawn local (Windows-friendly). */

const fs = require('fs');
const path = require('path');
const { spawn, execSync, execFileSync } = require('child_process');

const PM2_APP = 'trading-bot';

function loadProjectEnv(root) {
    const env = { ...process.env };
    const envPath = path.join(root, '.env');
    try {
        const raw = fs.readFileSync(envPath, 'utf8');
        for (const line of raw.split(/\r?\n/)) {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith('#')) {
                continue;
            }
            const eq = trimmed.indexOf('=');
            if (eq <= 0) {
                continue;
            }
            const key = trimmed.slice(0, eq).trim();
            let value = trimmed.slice(eq + 1).trim();
            if (
                (value.startsWith('"') && value.endsWith('"'))
                || (value.startsWith("'") && value.endsWith("'"))
            ) {
                value = value.slice(1, -1);
            }
            env[key] = value;
        }
    } catch {
        /* .env opcional */
    }
    return env;
}

function createBotState(root, pythonBin) {
    const controlPath = path.join(root, 'data', 'control.json');
    const pidPath = path.join(root, 'data', 'bot.pid');
    const processLogPath = path.join(root, 'logs', 'bot_process.log');
    const projectEnv = () => loadProjectEnv(root);

    function isPidAlive(pid) {
        if (!pid || pid <= 0) {
            return false;
        }
        try {
            process.kill(pid, 0);
            return true;
        } catch (err) {
            return err.code === 'EPERM';
        }
    }

    function readPaused() {
        try {
            const raw = fs.readFileSync(controlPath, 'utf8');
            const data = JSON.parse(raw);
            return !!data.paused;
        } catch {
            return false;
        }
    }

    function writePaused(paused) {
        let data = { paused: false };
        try {
            data = JSON.parse(fs.readFileSync(controlPath, 'utf8'));
        } catch {
            /* nuevo archivo */
        }
        data.paused = !!paused;
        fs.mkdirSync(path.dirname(controlPath), { recursive: true });
        fs.writeFileSync(controlPath, JSON.stringify(data, null, 2), 'utf8');
    }

    function readPid() {
        try {
            const text = fs.readFileSync(pidPath, 'utf8').trim();
            if (!/^\d+$/.test(text)) {
                return null;
            }
            return parseInt(text, 10);
        } catch {
            return null;
        }
    }

    function clearPid() {
        try {
            if (fs.existsSync(pidPath)) {
                fs.unlinkSync(pidPath);
            }
        } catch {
            /* ignorar */
        }
    }

    function pruneStalePid() {
        const pid = readPid();
        if (pid && !isPidAlive(pid)) {
            clearPid();
            return null;
        }
        return pid;
    }

    /** Devuelve el proceso PM2 trading-bot o null si no hay PM2 / no está registrado. */
    function pm2Describe() {
        try {
            const raw = execFileSync('pm2', ['jlist'], {
                encoding: 'utf8',
                windowsHide: true,
                timeout: 5000,
            });
            const list = JSON.parse(raw || '[]');
            if (!Array.isArray(list)) {
                return null;
            }
            return list.find((app) => app && app.name === PM2_APP) || null;
        } catch {
            return null;
        }
    }

    function pm2Managed() {
        return pm2Describe() !== null;
    }

    function pm2Running() {
        const app = pm2Describe();
        if (!app || !app.pm2_env) {
            return false;
        }
        return app.pm2_env.status === 'online';
    }

    function pm2Cmd(args) {
        execFileSync('pm2', args, {
            cwd: root,
            windowsHide: true,
            stdio: 'ignore',
            timeout: 20000,
        });
    }

    function readState() {
        const paused = readPaused();
        if (pm2Managed()) {
            const running = pm2Running();
            const app = pm2Describe();
            const pid = running && app && app.pid ? Number(app.pid) : null;
            return {
                paused,
                running,
                active: running && !paused,
                pid,
                manager: 'pm2',
            };
        }
        const pid = pruneStalePid();
        const running = pid !== null && isPidAlive(pid);
        return {
            paused,
            running,
            active: running && !paused,
            pid: running ? pid : null,
            manager: 'local',
        };
    }

    function killProcess(pid) {
        if (!pid || !isPidAlive(pid)) {
            return false;
        }
        try {
            if (process.platform === 'win32') {
                execSync(`taskkill /PID ${pid} /T /F`, { windowsHide: true, stdio: 'ignore' });
            } else {
                process.kill(pid, 'SIGTERM');
            }
            return true;
        } catch {
            return false;
        }
    }

    function startBotProcess() {
        fs.mkdirSync(path.dirname(processLogPath), { recursive: true });
        const logFd = fs.openSync(processLogPath, 'a');
        const stamp = new Date().toISOString();
        fs.writeSync(logFd, `\n--- bot start ${stamp} ---\n`);
        const child = spawn(pythonBin(), [path.join(root, 'main.py')], {
            cwd: root,
            env: {
                ...projectEnv(),
                BOT_STARTUP_SOURCE: 'panel',
                PYTHONUNBUFFERED: '1',
            },
            detached: true,
            stdio: ['ignore', logFd, logFd],
            windowsHide: true,
        });
        child.unref();
        try {
            fs.closeSync(logFd);
        } catch {
            /* ignorar */
        }
        return child.pid;
    }

    function waitForActive(timeoutMs = 20000) {
        const deadline = Date.now() + timeoutMs;
        return new Promise((resolve) => {
            const tick = () => {
                const state = readState();
                if (state.active) {
                    resolve(state);
                    return;
                }
                if (Date.now() >= deadline) {
                    resolve(readState());
                    return;
                }
                setTimeout(tick, 400);
            };
            tick();
        });
    }

    async function start() {
        if (fs.existsSync(path.join(root, 'data', 'LOCAL_DISABLED'))) {
            throw new Error('Bot local deshabilitado de forma permanente (data/LOCAL_DISABLED)');
        }
        writePaused(false);
        if (pm2Managed()) {
            try {
                if (pm2Running()) {
                    pm2Cmd(['restart', PM2_APP, '--update-env']);
                } else {
                    pm2Cmd(['start', PM2_APP]);
                }
            } catch {
                /* fallback abajo */
                startBotProcess();
            }
            return waitForActive();
        }
        let state = readState();
        if (!state.running) {
            startBotProcess();
            state = await waitForActive();
        } else if (state.paused) {
            writePaused(false);
            state = readState();
        }
        return state;
    }

    async function stop() {
        if (pm2Managed()) {
            try {
                // Detener vía PM2 primero (evita que autorestart levante el bot otra vez)
                pm2Cmd(['stop', PM2_APP]);
            } catch {
                /* ignore */
            }
            writePaused(true);
            clearPid();
            return readState();
        }
        writePaused(true);
        const pid = readPid();
        if (pid) {
            const deadline = Date.now() + 8000;
            while (isPidAlive(pid) && Date.now() < deadline) {
                await new Promise((resolve) => setTimeout(resolve, 400));
            }
            if (isPidAlive(pid)) {
                killProcess(pid);
            }
        }
        clearPid();
        return readState();
    }

    return {
        readState,
        start,
        stop,
    };
}

module.exports = { createBotState };
