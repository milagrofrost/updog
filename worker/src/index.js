// Runs on Cloudflare, so it keeps working when the house is offline. It checks
// the router every minute, keeps a copy of the outage history, and counts
// "mine's down" reports from neighbors.

const STATUS_TEXT = 'okayokayokay';
const CHECK_TIMEOUT_MS = 10000;
const HISTORY_EVERY_MINUTES = 5;
const REPORT_WINDOW_HOURS = 1;
const KEEP_REPORTS_DAYS = 7;
const STATE_STALE_MS = 5 * 60 * 1000;
const HOUR_MS = 3600000;

export function nextState(previous, up, now) {
    const changed = !previous || previous.up !== up;
    return { up, since: changed ? now : previous.since, checked: now };
}

export function isStale(state, nowMs) {
    return !state || nowMs - Date.parse(state.checked) > STATE_STALE_MS;
}

export function outagesChanged(saved, fresh) {
    return !saved || JSON.stringify(saved.outages) !== JSON.stringify(fresh.outages);
}

export function hourBucket(nowMs) {
    return Math.floor(nowMs / HOUR_MS);
}

// The address is only used with a secret that changes daily, so reports can be
// de-duplicated without keeping anything that identifies a person.
export async function reporterId(ip, salt, nowMs) {
    const day = new Date(nowMs).toISOString().slice(0, 10);
    const bytes = new TextEncoder().encode(`${salt}|${day}|${ip}`);
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

async function getValue(env, key) {
    const row = await env.DB.prepare('SELECT value FROM state WHERE key = ?').bind(key).first();
    return row ? JSON.parse(row.value) : null;
}

async function setValue(env, key, value) {
    await env.DB.prepare('INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
        .bind(key, JSON.stringify(value)).run();
}

async function reportCount(env, nowMs) {
    const row = await env.DB.prepare('SELECT COUNT(DISTINCT reporter) AS count FROM reports WHERE hour >= ?')
        .bind(hourBucket(nowMs - REPORT_WINDOW_HOURS * HOUR_MS)).first();
    return row.count;
}

async function routerResponds(env) {
    try {
        const response = await fetch(env.STATUS_URL, { signal: AbortSignal.timeout(CHECK_TIMEOUT_MS) });
        return (await response.text()).includes(STATUS_TEXT);
    } catch {
        return false;
    }
}

async function saveHistory(env) {
    try {
        const response = await fetch(env.HISTORY_URL, { signal: AbortSignal.timeout(CHECK_TIMEOUT_MS) });
        if (!response.ok) return;
        const fresh = await response.json();
        if (!Array.isArray(fresh.outages)) return;
        if (outagesChanged(await getValue(env, 'history'), fresh)) {
            await setValue(env, 'history', fresh);
        }
    } catch {
        // The router is unreachable, so keep the copy we already have.
    }
}

export async function runChecks(env, nowMs, minute) {
    const now = new Date(nowMs).toISOString();
    const up = await routerResponds(env) || await routerResponds(env);
    await setValue(env, 'status', nextState(await getValue(env, 'status'), up, now));

    if (up && minute % HISTORY_EVERY_MINUTES === 0) {
        await saveHistory(env);
    }
    if (minute === 0) {
        await env.DB.prepare('DELETE FROM reports WHERE hour < ?')
            .bind(hourBucket(nowMs - KEEP_REPORTS_DAYS * 24 * HOUR_MS)).run();
    }
}

function json(env, body, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: {
            'Content-Type': 'application/json',
            'Cache-Control': 'no-store',
            'Access-Control-Allow-Origin': env.ALLOWED_ORIGIN,
            'Vary': 'Origin',
        },
    });
}

async function handleStatus(env, nowMs) {
    const state = await getValue(env, 'status');
    return json(env, {
        status: isStale(state, nowMs) ? null : state,
        reports: await reportCount(env, nowMs),
        history: await getValue(env, 'history'),
    });
}

async function handleReport(request, env, nowMs) {
    if (request.headers.get('Origin') !== env.ALLOWED_ORIGIN) {
        return json(env, { error: 'forbidden' }, 403);
    }
    const ip = request.headers.get('CF-Connecting-IP');
    if (ip && env.REPORT_SALT) {
        const reporter = await reporterId(ip, env.REPORT_SALT, nowMs);
        await env.DB.prepare('INSERT OR IGNORE INTO reports (reporter, hour) VALUES (?, ?)')
            .bind(reporter, hourBucket(nowMs)).run();
    }
    return json(env, { reports: await reportCount(env, nowMs) });
}

export default {
    async fetch(request, env) {
        const { pathname } = new URL(request.url);
        const nowMs = Date.now();
        if (request.method === 'OPTIONS') {
            return new Response(null, {
                status: 204,
                headers: {
                    'Access-Control-Allow-Origin': env.ALLOWED_ORIGIN,
                    'Access-Control-Allow-Methods': 'GET, POST',
                    'Access-Control-Max-Age': '86400',
                    'Vary': 'Origin',
                },
            });
        }
        if (pathname === '/api/status' && request.method === 'GET') return handleStatus(env, nowMs);
        if (pathname === '/api/report' && request.method === 'POST') return handleReport(request, env, nowMs);
        return json(env, { error: 'not found' }, 404);
    },

    async scheduled(event, env) {
        await runChecks(env, event.scheduledTime, new Date(event.scheduledTime).getUTCMinutes());
    },
};
