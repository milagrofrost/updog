// Run with: node --test
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { hourBucket, isStale, nextState, outagesChanged, reporterId } from '../src/index.js';

const T0 = '2026-11-04T12:00:00.000Z';
const T1 = '2026-11-04T12:01:00.000Z';

test('first check starts the clock', () => {
    assert.deepEqual(nextState(null, true, T0), { up: true, since: T0, checked: T0 });
});

test('no change keeps the original since', () => {
    assert.deepEqual(nextState({ up: true, since: T0, checked: T0 }, true, T1), { up: true, since: T0, checked: T1 });
});

test('a change resets since', () => {
    assert.deepEqual(nextState({ up: true, since: T0, checked: T0 }, false, T1), { up: false, since: T1, checked: T1 });
});

test('state goes stale when checks stop', () => {
    const now = Date.parse(T0);
    assert.equal(isStale(null, now), true);
    assert.equal(isStale({ checked: T0 }, now + 4 * 60000), false);
    assert.equal(isStale({ checked: T0 }, now + 6 * 60000), true);
});

test('history is only rewritten when outages change', () => {
    const rows = [{ start: 'a', end: 'b' }];
    assert.equal(outagesChanged(null, { outages: rows }), true);
    assert.equal(outagesChanged({ updated: '1', outages: rows }, { updated: '2', outages: [...rows] }), false);
    assert.equal(outagesChanged({ outages: rows }, { outages: [...rows, { start: 'c', end: null }] }), true);
});

test('reports fall into hourly buckets', () => {
    const now = Date.parse(T0);
    assert.equal(hourBucket(now), hourBucket(now + 59 * 60000));
    assert.equal(hourBucket(now) + 1, hourBucket(now + 60 * 60000));
});

test('reporter id is stable within a day and hides the address', async () => {
    const now = Date.parse(T0);
    const id = await reporterId('203.0.113.9', 'salt', now);
    assert.equal(id, await reporterId('203.0.113.9', 'salt', now + 3600000));
    assert.equal(id.includes('203.0.113.9'), false);
    assert.equal(id.length, 64);
});

test('reporter id changes by day, address and salt', async () => {
    const now = Date.parse(T0);
    const id = await reporterId('203.0.113.9', 'salt', now);
    assert.notEqual(id, await reporterId('203.0.113.9', 'salt', now + 86400000));
    assert.notEqual(id, await reporterId('203.0.113.10', 'salt', now));
    assert.notEqual(id, await reporterId('203.0.113.9', 'other', now));
});
