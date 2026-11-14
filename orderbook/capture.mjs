import { createWriteStream, mkdirSync, writeFileSync, renameSync } from 'node:fs';
import { finished } from 'node:stream/promises';
import { setTimeout as delay } from 'node:timers/promises';
import { resolve, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

const GAMMA = 'https://gamma-api.polymarket.com';
const STREAM = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';
const HELP = `공개 Yes/No 실시간 호가 수집기 (Node.js 22.15 이상)
사용법: node capture.mjs --market SLUG_OR_URL [--market SLUG_OR_URL] --minutes 80
        node capture.mjs 80 MARKET_SLUG [MARKET_SLUG ...]
옵션: --output 새_세션_폴더  --help
활성 이벤트 링크는 그 안의 활성 Yes/No 질문을 함께 수집합니다 (최대 20개).
CSV와 분석 자동 생성: python3 run.py --market SLUG_OR_URL --minutes 80`;

export function parseOptions(argv) {
  const options = { minutes: 80, targets: [] };
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') return { help: true };
    if (['--minutes', '--market', '--output'].includes(arg)) {
      const value = argv[++i];
      if (!value || value.startsWith('--')) throw Error(`${arg} 값이 필요합니다`);
      if (arg === '--market') options.targets.push(value);
      else if (arg === '--minutes') options.minutes = Number(value);
      else options.output = value;
    } else if (arg.startsWith('-')) throw Error(`알 수 없는 옵션: ${arg}`);
    else positional.push(arg);
  }
  if (positional.length && /^\d+(\.\d+)?$/.test(positional[0])) {
    options.minutes = Number(positional.shift());
  }
  options.targets.push(...positional);
  if (!Number.isFinite(options.minutes) || options.minutes <= 0 || options.minutes > 1440) {
    throw Error('수집 시간은 0보다 크고 1440분 이하여야 합니다');
  }
  if (!options.targets.length) throw Error('활성 마켓의 링크나 slug를 --market으로 지정해 주세요');
  return options;
}

export function targetReference(value) {
  let slug = value, event = false;
  if (value.includes('://')) {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !['polymarket.com', 'www.polymarket.com'].includes(url.hostname)) {
      throw Error('https://polymarket.com 마켓/이벤트 링크를 사용해 주세요');
    }
    const parts = url.pathname.split('/').filter(Boolean);
    if (!['event', 'market'].includes(parts[0]) || parts.length < 2 || parts.length > 3) {
      throw Error('개별 마켓 또는 이벤트 링크가 필요합니다');
    }
    slug = parts.at(-1);
    event = parts[0] === 'event' && parts.length === 2;
  }
  if (!/^[a-z0-9-]+$/i.test(slug)) throw Error('마켓 slug 형식 오류');
  return { slug, event };
}

export function marketMetadata(m) {
  if (!m.active || m.closed || m.acceptingOrders === false || !m.enableOrderBook) {
    throw Error(`${m.slug ?? '마켓'}: 호가 수집이 가능한 활성 마켓이 아닙니다`);
  }
  const array = v => typeof v === 'string' ? JSON.parse(v) : v;
  const ids = array(m.clobTokenIds), names = array(m.outcomes);
  if (!Array.isArray(ids) || !Array.isArray(names) || ids.length !== 2 || names.length !== 2 ||
      !names.every(n => typeof n === 'string') ||
      new Set(names.map(n => n.toLowerCase())).size !== 2 ||
      !names.some(n => n.toLowerCase() === 'yes') || !names.some(n => n.toLowerCase() === 'no') ||
      !ids.every(id => typeof id === 'string' && /^\d+$/.test(id)) || new Set(ids).size !== 2 ||
      typeof m.conditionId !== 'string' || !m.conditionId || !m.slug) {
    throw Error(`${m.slug ?? '마켓'}: Yes/No 토큰 매핑 오류`);
  }
  return { slug: m.slug, question: m.question, condition_id: m.conditionId,
    tokens: ids.map((id, i) => ({ id, outcome: names[i] })) };
}

function errorText(e) {
  return `${e.message ?? e}${e.cause?.code ? ` (${e.cause.code})` : ''}`;
}

export async function resolveMarkets(targets, fetchImpl = fetch, onProgress = () => {}) {
  async function request(path, allowNotFound = false) {
    const response = await fetchImpl(GAMMA + path, { signal: AbortSignal.timeout(12000) });
    if (allowNotFound && response.status === 404) return null;
    if (!response.ok) throw Error(`마켓 조회 HTTP ${response.status}: ${path}`);
    return response.json();
  }
  const markets = new Map();
  // Fail rather than silently dropping a requested market.
  for (const value of new Set(targets)) {
    onProgress('활성 마켓 조회:', value);
    const ref = targetReference(value);
    const single = ref.event ? null : await request('/markets/slug/' + ref.slug, true);
    if (single) {
      const market = marketMetadata(single);
      markets.set(market.slug, market);
    } else {
      const event = await request('/events/slug/' + ref.slug);
      const active = (event.markets ?? []).filter(m => m.active && !m.closed &&
        m.acceptingOrders !== false && m.enableOrderBook);
      if (!active.length) throw Error(`${ref.slug}: 활성 호가 마켓이 없습니다`);
      for (const item of active) {
        const market = marketMetadata(item);
        markets.set(market.slug, market);
      }
    }
    if (markets.size > 20) throw Error('질문이 20개를 넘습니다. 개별 마켓 링크를 지정해 주세요');
  }
  const result = [...markets.values()];
  const assets = result.flatMap(m => m.tokens.map(t => t.id));
  if (!assets.length || new Set(assets).size !== assets.length) throw Error('토큰이 없거나 중복되었습니다');
  return result;
}

export async function runCapture(options, dependencies = {}) {
  const { WebSocketImpl = WebSocket, fetchImpl = fetch, log = console.log,
    heartbeatMs = 10000, heartbeatTimeoutMs = 25000, openTimeoutMs = 12000,
    snapshotTimeoutMs = 20000, reconnectDelayMs = 2000, progressMs = 5000 } = dependencies;
  const markets = await resolveMarkets(options.targets, fetchImpl, log);
  const assets = markets.flatMap(m => m.tokens.map(t => t.id));
  const tokenMarkets = new Map(markets.flatMap(m => m.tokens.map(t => [t.id, m.condition_id])));
  const started = new Date();
  const sessionDir = resolve(options.output ?? resolve('ws_data',
    started.toISOString().replace(/[:.]/g, '-') + '-' + process.pid));
  mkdirSync(dirname(sessionDir), { recursive: true });
  mkdirSync(sessionDir); // Never overwrite an existing capture.
  writeFileSync(resolve(sessionDir, 'metadata.json'), JSON.stringify({
    started_at: started.toISOString(), requested_minutes: options.minutes,
    requested_targets: options.targets, markets,
    timestamp_note: 'received_ms is local receipt time; received_monotonic_ns is local process clock, not exchange time',
  }, null, 2) + '\n');
  log('저장 위치:', sessionDir);
  for (const market of markets) log('관찰:', market.question);
  const controller = new AbortController();
  let stopReason = 'duration', phase = 'connecting', failure;
  let sequence = 0, connection = 0, messages = 0, usefulMessages = 0;
  let lastMessageMs = null, lastError = null, opened = false, currentFinish;
  let consecutiveFailures = 0;
  const snapshots = new Set();
  let currentSnapshots = new Set(), endedHealthy = false;
  const eventCounts = {};
  const output = createWriteStream(resolve(sessionDir, 'events.jsonl'), { flags: 'wx' });
  const outputDone = finished(output);
  outputDone.catch(() => {});
  function stop(reason) {
    stopReason = reason;
    controller.abort();
  }
  output.on('error', e => { failure = e; stop('disk_error'); });
  const record = (kind, extra = {}) => {
    if (failure) return;
    output.write(JSON.stringify({ sequence: ++sequence, connection, kind,
      received_ms: Date.now(), received_monotonic_ns: process.hrtime.bigint().toString(),
      ...extra }) + '\n');
  };
  function status(state = phase) {
    const value = { state, session_dir: sessionDir, started_at: started.toISOString(),
      updated_at: new Date().toISOString(), elapsed_seconds: Math.round((Date.now()-started.getTime())/1000),
      messages, useful_messages: usefulMessages, event_counts: eventCounts,
      connections: connection, reconnects: Math.max(0, connection-1),
      snapshot_tokens: currentSnapshots.size, ever_snapshot_tokens: snapshots.size,
      expected_tokens: assets.length,
      last_message_at: lastMessageMs === null ? null : new Date(lastMessageMs).toISOString(),
      last_error: lastError, stop_reason: controller.signal.aborted ? stopReason : null,
      bytes_written: output.bytesWritten };
    const temp = resolve(sessionDir, 'status.json.tmp');
    writeFileSync(temp, JSON.stringify(value, null, 2) + '\n');
    renameSync(temp, resolve(sessionDir, 'status.json'));
    return value;
  }
  function saveStatus() {
    try { return status(); } catch (e) { failure = e; stop('status_write_error'); }
  }
  const externalAbort = () => stop('interrupted');
  options.signal?.addEventListener('abort', externalAbort, { once: true });
  if (options.signal?.aborted) externalAbort();
  const deadline = setTimeout(() => stop('duration'), options.minutes * 60000);
  const progress = setInterval(() => {
    const age = lastMessageMs === null ? '미수신' : `${Math.round((Date.now()-lastMessageMs)/1000)}초 전`;
    log(`[${phase}] 메시지 ${messages} / 호가·체결 ${usefulMessages} / 초기 호가 ${currentSnapshots.size}/${assets.length} / 마지막 수신 ${age}`);
    saveStatus();
  }, progressMs);
  const health = setInterval(saveStatus, 1000);
  saveStatus();
  async function connect() {
    connection++;
    phase = 'connecting';
    currentSnapshots = new Set();
    await new Promise(done => {
      let ws, complete = false, heartbeat, snapshotTimer, hadUseful = false;
      let lastPong = Date.now();
      const connectionSnapshots = new Set();
      currentSnapshots = connectionSnapshots;
      const openTimer = setTimeout(() => finish('open_timeout'), openTimeoutMs);
      function finish(reason) {
        if (complete) return;
        complete = true;
        endedHealthy = reason === 'stopped' && ws?.readyState === 1 && hadUseful &&
          connectionSnapshots.size === assets.length;
        opened = false;
        clearTimeout(openTimer);
        clearTimeout(snapshotTimer);
        clearInterval(heartbeat);
        controller.signal.removeEventListener('abort', abort);
        if (reason !== 'stopped') lastError = reason;
        consecutiveFailures = hadUseful ? 0 : consecutiveFailures + 1;
        record('disconnect', { reason });
        phase = 'reconnecting';
        try { if (ws && ws.readyState < 2) ws.close(); } catch {}
        done();
      }
      currentFinish = finish;
      const abort = () => finish('stopped');
      controller.signal.addEventListener('abort', abort, { once: true });
      try { ws = new WebSocketImpl(STREAM); }
      catch (e) { finish(errorText(e)); return; }
      ws.addEventListener('open', () => {
        if (complete) { try { ws.close(); } catch {} return; }
        clearTimeout(openTimer);
        opened = true;
        phase = 'waiting_for_snapshot';
        record('connect');
        ws.send(JSON.stringify({ assets_ids: assets, type: 'market', custom_feature_enabled: true }));
        lastPong = Date.now();
        snapshotTimer = setTimeout(() => {
          if (connectionSnapshots.size < assets.length) finish('snapshot_timeout');
        }, snapshotTimeoutMs);
        heartbeat = setInterval(() => {
          if (Date.now()-lastPong > heartbeatTimeoutMs) return finish('heartbeat_timeout');
          if (ws.readyState === 1) ws.send('PING');
        }, heartbeatMs);
        log('실시간 연결:', connection, '/ Yes·No 토큰:', assets.length);
      });
      ws.addEventListener('message', event => {
        if (complete) return;
        const received = Date.now();
        if (event.data === 'PONG') { lastPong = received; return; }
        messages++;
        lastMessageMs = received;
        record('message', { received_ms: received, raw: event.data });
        try {
          const payload = JSON.parse(event.data);
          let useful = false;
          for (const item of Array.isArray(payload) ? payload : [payload]) {
            if (!item || typeof item !== 'object') continue;
            const kind = item.event_type;
            const known = id => tokenMarkets.get(id) === item.market && tokenMarkets.has(id);
            if (kind === 'book' && known(item.asset_id) && Array.isArray(item.bids) && Array.isArray(item.asks)) {
              snapshots.add(item.asset_id);
              connectionSnapshots.add(item.asset_id);
            }
            if ((['book','best_bid_ask','last_trade_price'].includes(kind) && known(item.asset_id)) ||
                (kind === 'price_change' && item.price_changes?.some(c => known(c.asset_id)))) {
              useful = true;
              eventCounts[kind] = (eventCounts[kind] ?? 0) + 1;
            }
            if (item.error) lastError = String(item.error);
          }
          if (useful) { usefulMessages++; hadUseful = true; phase = 'receiving'; }
        } catch { eventCounts.unparsed = (eventCounts.unparsed ?? 0) + 1; }
        if (messages === 1) log('첫 실시간 메시지 저장 완료');
        if (output.writableLength > 8 * 1024 * 1024) finish('disk_backlog');
      });
      ws.addEventListener('error', e => finish(e.message || e.error?.message || 'socket_error'));
      ws.addEventListener('close', e => finish('close_' + e.code));
      if (controller.signal.aborted) abort();
    });
  }
  try {
    while (!controller.signal.aborted) {
      await connect();
      if (!controller.signal.aborted) {
        saveStatus();
        try { await delay(Math.min(reconnectDelayMs * 2 ** Math.min(Math.max(consecutiveFailures-1, 0), 4), 30000),
          undefined, { signal: controller.signal }); } catch (e) { if (e.name !== 'AbortError') throw e; }
      }
    }
  } finally {
    clearTimeout(deadline);
    clearInterval(progress);
    clearInterval(health);
    options.signal?.removeEventListener('abort', externalAbort);
    if (opened) currentFinish?.('stopped');
    record('end', { messages, useful_messages: usefulMessages, stop_reason: stopReason });
    output.end();
    try { await outputDone; } catch (e) { failure = e; }
  }
  phase = failure ? 'failed' : !usefulMessages ? 'empty' : !endedHealthy ? 'partial'
    : stopReason === 'interrupted' ? 'stopped' : 'complete';
  const result = status(phase);
  log(`수집 ${phase}: ${messages} 메시지 / ${sessionDir}`);
  if (failure) throw Error('파일 저장 실패: ' + errorText(failure));
  return result;
}

async function main() {
  const options = parseOptions(process.argv.slice(2));
  if (options.help) { console.log(HELP); return; }
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 22 || (major === 22 && minor < 15) || typeof WebSocket !== 'function') {
    throw Error('Node.js 22.15 이상이 필요합니다');
  }
  const controller = new AbortController();
  const interrupt = () => controller.abort();
  process.once('SIGINT', interrupt);
  process.once('SIGTERM', interrupt);
  try {
    const result = await runCapture({ ...options, signal: controller.signal });
    if (['empty','partial','failed'].includes(result.state)) {
      console.error('수집을 완료로 처리하지 않습니다. status.json의 last_error와 초기 호가 수를 확인해 주세요.');
      process.exitCode = 1;
    }
  } finally {
    process.removeListener('SIGINT', interrupt);
    process.removeListener('SIGTERM', interrupt);
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(e => { console.error('수집 실패:', errorText(e)); process.exitCode = 1; });
}
