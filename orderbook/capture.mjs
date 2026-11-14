import { createWriteStream, mkdirSync, writeFileSync } from 'node:fs';
import { once } from 'node:events';
import { resolve } from 'node:path';

const candidates = [
  'will-mary-peltola-win-the-alaska-senate-race-in-2026',
  'will-the-democratic-party-candidate-win-the-2026-michigan-senate-election-by-3-6',
  'will-the-republican-party-candidate-win-the-2026-florida-senate-election-by-0-3',
  'will-the-democratic-party-candidate-win-the-2026-iowa-senate-election-by-3-6',
];
const [minutesArg = '80', ...overrides] = process.argv.slice(2);
const minutes = Number(minutesArg);
if (!Number.isFinite(minutes) || minutes <= 0) {
  console.error('사용법: node capture.mjs 80 [마켓_slug ...]');
  process.exit(2);
}
const slugs = [...new Set(overrides.length ? overrides : candidates)];
const targets = await Promise.allSettled(slugs.map(async slug => {
  if (!/^[a-z0-9-]+$/i.test(slug)) throw Error('마켓 slug 형식 오류');
  const response = await fetch(
    'https://gamma-api.polymarket.com/markets/slug/' + encodeURIComponent(slug),
    { signal: AbortSignal.timeout(12000) },
  );
  if (!response.ok) throw Error('HTTP ' + response.status);
  const m = await response.json();
  if (!m.active || m.closed || m.acceptingOrders === false || !m.enableOrderBook) {
    throw Error('호가 수집이 가능한 활성 마켓이 아닙니다');
  }
  const array = v => typeof v === 'string' ? JSON.parse(v) : v;
  const ids = array(m.clobTokenIds);
  const names = array(m.outcomes);
  if (!Array.isArray(ids) || !Array.isArray(names) ||
      ids.length !== 2 || names.length !== 2 || !names.some(n => n.toLowerCase() === 'yes') ||
      !names.some(n => n.toLowerCase() === 'no') || !ids.every(id => /^\d+$/.test(id)) ||
      new Set(ids).size !== 2) {
    throw Error('Yes/No 토큰 매핑 오류');
  }
  return { slug, question: m.question, condition_id: m.conditionId,
    tokens: ids.map((id, i) => ({ id: String(id), outcome: names[i] })) };
}));
const markets = [];
targets.forEach((r, i) => {
  if (r.status === 'fulfilled') markets.push(r.value);
  else console.error('제외:', slugs[i], r.reason.message, r.reason.cause?.code ?? '');
});
if (!markets.length) process.exit(1);
if (markets.length < 2) console.error('현재 1개만 수집 가능. 여러 마켓 비교에는 추가 slug가 필요합니다.');
const assets = markets.flatMap(m => m.tokens.map(t => t.id));
const start = new Date();
const dir = resolve('ws_data', start.toISOString().replace(/[:.]/g, '-'));
mkdirSync(dir, { recursive: true });
writeFileSync(resolve(dir, 'metadata.json'), JSON.stringify({
  started_at: start.toISOString(), requested_minutes: minutes, markets,
}, null, 2));
console.log('저장 위치:', dir);
for (const m of markets) console.log('관찰:', m.question);

const output = createWriteStream(resolve(dir, 'events.jsonl'), { flags: 'wx' });
let sequence = 0;
let connection = 0;
let messages = 0;
let ws;
let stop = false;
const record = (kind, extra = {}) => output.write(JSON.stringify({
  sequence: ++sequence, connection, kind, received_ms: Date.now(), ...extra,
}) + '\n');
function connect() {
  return new Promise(done => {
    connection++;
    ws = new WebSocket('wss://ws-subscriptions-clob.polymarket.com/ws/market');
    let lastPong = Date.now();
    let heartbeat;
    let complete = false;
    const openTimeout = setTimeout(() => finish('open_timeout'), 12000);
    function finish(reason) {
      if (complete) return;
      complete = true;
      clearTimeout(openTimeout);
      clearInterval(heartbeat);
      record('disconnect', { reason });
      if (ws.readyState < WebSocket.CLOSING) ws.close();
      done();
    }
    ws.addEventListener('open', () => {
      if (complete) return;
      clearTimeout(openTimeout);
      record('connect');
      ws.send(JSON.stringify({ assets_ids: assets, type: 'market', custom_feature_enabled: true }));
      lastPong = Date.now();
      heartbeat = setInterval(() => {
        if (Date.now() - lastPong > 25000) return finish('heartbeat_timeout');
        if (ws.readyState === WebSocket.OPEN) ws.send('PING');
      }, 10000);
      console.log('연결:', connection, '토큰:', assets.length);
    });
    ws.addEventListener('message', event => {
      if (complete) return;
      const received = Date.now();
      if (event.data === 'PONG') {
        lastPong = received;
        return;
      }
      messages++;
      record('message', { received_ms: received, raw: event.data });
      if (messages === 1) console.log('첫 메시지 저장 완료');
      if (output.writableLength > 8 * 1024 * 1024) finish('disk_backlog');
    });
    ws.addEventListener('error', e => {
      const reason = e.message || e.error?.message || 'socket_error';
      console.error('연결 오류:', reason);
      finish(reason);
    });
    ws.addEventListener('close', e => finish('close_' + e.code));
  });
}
function shutdown() {
  stop = true;
  if (ws?.readyState < WebSocket.CLOSING) ws.close();
}
process.once('SIGINT', shutdown);
process.once('SIGTERM', shutdown);
const deadline = setTimeout(shutdown, minutes * 60_000);
const progress = setInterval(() => console.log('수신 메시지:', messages), 30000);
while (!stop) {
  await connect();
  if (!stop) await new Promise(r => setTimeout(r, 2000));
}
clearTimeout(deadline);
clearInterval(progress);
record('end', { messages });
output.end();
await once(output, 'finish');
console.log('완료:', messages, '메시지 /', dir);
