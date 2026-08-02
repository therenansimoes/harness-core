// Minimal single-session WhatsApp connector (Baileys). Ported from
// 02-crm/connector-whatsapp/src/lib/baileys.ts — stripped down to one
// session, file-based auth, and a tiny HTTP bridge on 127.0.0.1 only.
//
// This process never decides anything: it only relays inbound text into
// WA_INBOX (append-only JSONL) and sends whatever /send tells it to send.
// Allowlist is enforced on both directions; groups are always ignored.
import makeWASocket, {
    useMultiFileAuthState,
    makeCacheableSignalKeyStore,
    fetchLatestBaileysVersion,
    DisconnectReason,
    Browsers,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import http from 'http';
import fs from 'fs';
import path from 'path';

const PORT = parseInt(process.env.WA_PORT || '8787', 10);
const AUTH_DIR = process.env.WA_AUTH_DIR || './auth';
const INBOX_PATH = process.env.WA_INBOX || './inbox.jsonl';
const ALLOWLIST = new Set(
    (process.env.WA_ALLOWLIST || '')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
);

const baileysLogger = pino({ level: process.env.BAILEYS_LOG_LEVEL || 'warn' });

// ─── In-memory state (single session, no multi-tenant) ─────────────
const state = {
    connected: false,
    jid: null,
    qr: null,
    lastError: null,
};

let socket = null;
let hasExistingSession = false;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 10;
let reconnectTimer = null;
let pairingInProgress = false;

// ─── Stuck-disconnected watchdog (simplified single-session version) ──
// Baileys gives up reconnecting after MAX_RECONNECT_ATTEMPTS (~a few min).
// This watchdog periodically checks for that and retries connect() with
// its own slow backoff, so the session doesn't stay dead forever without
// a process restart.
let disconnectedSince = null;
const STUCK_WATCHDOG_INTERVAL_MS = 60_000;
const STUCK_THRESHOLD_MS = 5 * 60_000;
const RECOVERY_BACKOFFS_MS = [60_000, 5 * 60_000, 15 * 60_000, 60 * 60_000];
let recoveryBackoffIndex = 0;
let lastRecoveryAttemptAt = 0;

function scheduleReconnect(delayMs) {
    clearReconnect();
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect().catch((err) => {
            state.lastError = String(err?.message || err);
            console.error('[wa] reconnect failed:', err);
        });
    }, delayMs);
}

function clearReconnect() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
}

function clearAuthFiles() {
    try {
        if (fs.existsSync(AUTH_DIR)) {
            fs.rmSync(AUTH_DIR, { recursive: true, force: true });
        }
    } catch (err) {
        console.error('[wa] failed to clear auth dir:', err);
    }
}

function checkStuckAndRecover() {
    if (!hasExistingSession) return;
    if (state.connected) return;
    if (!disconnectedSince) return;

    const now = Date.now();
    const dwellMs = now - disconnectedSince;
    if (dwellMs < STUCK_THRESHOLD_MS) return;

    const backoffIdx = Math.min(recoveryBackoffIndex, RECOVERY_BACKOFFS_MS.length - 1);
    const requiredGap = RECOVERY_BACKOFFS_MS[backoffIdx];
    if (lastRecoveryAttemptAt > 0 && now - lastRecoveryAttemptAt < requiredGap) return;
    if (reconnectTimer) return;

    const dwellSec = Math.round(dwellMs / 1000);
    console.warn(`[wa] watchdog: stuck disconnected ${dwellSec}s — attempting recovery (backoff idx=${backoffIdx})`);

    lastRecoveryAttemptAt = now;
    recoveryBackoffIndex = Math.min(recoveryBackoffIndex + 1, RECOVERY_BACKOFFS_MS.length - 1);
    reconnectAttempts = 0;

    connect().catch((err) => {
        console.error('[wa] watchdog recovery connect() failed:', err);
    });
}

setInterval(() => {
    try {
        checkStuckAndRecover();
    } catch (err) {
        console.error('[wa] watchdog tick error:', err);
    }
}, STUCK_WATCHDOG_INTERVAL_MS);

// ─── Connect ─────────────────────────────────────────
async function connect() {
    clearReconnect();

    const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

    let waVersion;
    try {
        const { version, isLatest } = await fetchLatestBaileysVersion();
        waVersion = version;
        console.log(`[wa] WA version: ${version.join('.')} (isLatest: ${isLatest})`);
    } catch (err) {
        console.warn('[wa] failed to fetch latest WA version, using built-in default');
    }

    socket = makeWASocket({
        ...(waVersion && { version: waVersion }),
        auth: {
            creds: authState.creds,
            keys: makeCacheableSignalKeyStore(authState.keys, baileysLogger),
        },
        logger: baileysLogger,
        printQRInTerminal: false,
        qrTimeout: 60_000,
        browser: Browsers.windows('Chrome'),
        // Don't set presence as online on connect — kills phone notifications otherwise.
        markOnlineOnConnect: false,
        // No `agent`/`fetchAgent` — this service never runs behind a proxy, and
        // Baileys' CDN upload uses Node's native fetch (undici), which is
        // incompatible with http.Agent-style proxy agents anyway.
    });

    socket.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            state.qr = qr;
            state.connected = false;
            console.log('[wa] QR code generated, waiting for scan...');
        }

        if (connection === 'open') {
            state.qr = null;
            state.connected = true;
            state.lastError = null;
            state.jid = socket?.user?.id?.replace(/:.*@/, '@') || null;
            hasExistingSession = true;
            reconnectAttempts = 0;
            disconnectedSince = null;
            recoveryBackoffIndex = 0;
            console.log(`[wa] connected as ${state.jid}`);
        }

        if (connection === 'close') {
            const statusCode = (lastDisconnect?.error instanceof Boom)
                ? lastDisconnect.error.output?.statusCode
                : undefined;

            state.connected = false;
            state.qr = null;
            socket = null;
            disconnectedSince = Date.now();

            console.log(`[wa] connection closed (code: ${statusCode})`);

            // 515 = restart required (e.g. right after pairing) — always reconnect immediately.
            if (statusCode === DisconnectReason.restartRequired) {
                console.log('[wa] restart required — reconnecting...');
                scheduleReconnect(2000);
                return;
            }

            // 405 right after pairing is expected — reconnect to complete pairing.
            if (statusCode === 405 && pairingInProgress) {
                console.log('[wa] 405 after pairing — reconnecting to complete...');
                scheduleReconnect(3000);
                return;
            }

            // 405 (outside pairing) or loggedOut = session is dead. Clear auth and stop.
            if (statusCode === 405 || statusCode === DisconnectReason.loggedOut) {
                clearAuthFiles();
                hasExistingSession = false;
                reconnectAttempts = 0;
                pairingInProgress = false;
                state.lastError = `session invalidated (${statusCode}); scan a new QR`;
                console.log(`[wa] session dead (${statusCode}). auth cleared. waiting for new QR.`);
                return;
            }

            // Transient errors — reconnect with exponential backoff if we have a session.
            if (hasExistingSession && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                reconnectAttempts++;
                const delay = Math.min(2000 * Math.pow(1.5, reconnectAttempts - 1), 120_000);
                console.log(`[wa] transient error (${statusCode}). reconnecting in ${(delay / 1000).toFixed(1)}s (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`);
                scheduleReconnect(delay);
            } else {
                console.log('[wa] not reconnecting (no session or max retries).');
                reconnectAttempts = 0;
                state.lastError = `reconnection exhausted after ${MAX_RECONNECT_ATTEMPTS} attempts (status ${statusCode})`;
            }
        }
    });

    socket.ev.on('creds.update', () => {
        saveCreds().catch((err) => console.error('[wa] CRITICAL: failed to save creds:', err));
    });

    socket.ev.on('messages.upsert', ({ messages }) => {
        for (const msg of messages) {
            try {
                handleIncoming(msg);
            } catch (err) {
                console.error('[wa] failed to handle incoming message:', err);
            }
        }
    });
}

// ─── Inbound handling ────────────────────────────────
function extractText(message) {
    if (!message) return null;
    if (message.conversation) return message.conversation;
    if (message.extendedTextMessage?.text) return message.extendedTextMessage.text;
    return null;
}

function handleIncoming(msg) {
    if (!msg.message) return;
    if (msg.key.fromMe) return;

    const remoteJid = msg.key.remoteJid || '';
    if (remoteJid === 'status@broadcast') return;

    const isGroup = remoteJid.endsWith('@g.us');
    // Groups are always ignored, no exceptions, even if sender is allowlisted.
    if (isGroup) return;

    // Fail closed: empty allowlist means nothing gets written to the inbox.
    if (ALLOWLIST.size === 0) return;
    if (!ALLOWLIST.has(remoteJid)) return;

    const text = extractText(msg.message);
    if (text === null) return; // not a plain text message — nothing to relay

    const line = JSON.stringify({
        ts: new Date().toISOString(),
        from: remoteJid,
        body: text,
        is_group: false,
        message_id: msg.key.id || '',
    });

    fs.appendFile(INBOX_PATH, line + '\n', (err) => {
        if (err) console.error('[wa] failed to append to inbox:', err);
    });
}

// ─── Outbound sending ────────────────────────────────
async function sendMessage(to, body) {
    if (!socket || !state.connected) {
        return { error: 'not connected', status: 503 };
    }

    try {
        const result = await socket.sendMessage(to, { text: body });
        return { id: result?.key?.id || '' };
    } catch (err) {
        console.error('[wa] failed to send message:', err);
        return { error: String(err?.message || err), status: 500 };
    }
}

// ─── HTTP server (127.0.0.1 only) ────────────────────
function readBody(req) {
    return new Promise((resolve, reject) => {
        let data = '';
        req.on('data', (chunk) => {
            data += chunk;
            if (data.length > 1_000_000) {
                reject(new Error('body too large'));
                req.destroy();
            }
        });
        req.on('end', () => resolve(data));
        req.on('error', reject);
    });
}

function sendJson(res, statusCode, body) {
    const payload = JSON.stringify(body);
    res.writeHead(statusCode, { 'Content-Type': 'application/json' });
    res.end(payload);
}

const server = http.createServer(async (req, res) => {
    if (req.method === 'GET' && req.url === '/status') {
        sendJson(res, 200, {
            connected: state.connected,
            jid: state.jid,
            qr: state.qr,
            last_error: state.lastError,
        });
        return;
    }

    if (req.method === 'POST' && req.url === '/send') {
        let body;
        try {
            const raw = await readBody(req);
            body = JSON.parse(raw);
        } catch (err) {
            sendJson(res, 400, { error: 'invalid JSON body' });
            return;
        }

        const { to, body: text } = body || {};
        if (typeof to !== 'string' || !to || typeof text !== 'string' || !text) {
            sendJson(res, 400, { error: 'missing or invalid "to"/"body"' });
            return;
        }

        // Fail closed: empty allowlist refuses every send.
        if (ALLOWLIST.size === 0 || !ALLOWLIST.has(to)) {
            sendJson(res, 403, { error: 'destino fora da allowlist' });
            return;
        }

        const result = await sendMessage(to, text);
        if (result.error) {
            sendJson(res, result.status || 500, { error: result.error });
            return;
        }

        sendJson(res, 200, { message_id: result.id });
        return;
    }

    sendJson(res, 404, { error: 'not found' });
});

server.listen(PORT, '127.0.0.1', () => {
    console.log(`[wa] listening on 127.0.0.1:${PORT}`);
});

connect().catch((err) => {
    state.lastError = String(err?.message || err);
    console.error('[wa] initial connect failed:', err);
});

process.on('SIGINT', () => {
    console.log('[wa] shutting down...');
    server.close(() => process.exit(0));
});
process.on('SIGTERM', () => {
    server.close(() => process.exit(0));
});
