import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { marketMetadata, parseOptions, targetReference, resolveMarkets, runCapture } from './capture.mjs';

const metadata = { active: true, closed: false, acceptingOrders: true, enableOrderBook: true,
  slug: 'test-market', question: 'Test?', conditionId: 'condition',
  clobTokenIds: '["123","456"]', outcomes: '["Yes","No"]' };
const fakeFetch = async () => new Response(JSON.stringify(metadata));
const snapshots = ['123','456'].map(asset_id => ({event_type:'book', asset_id, market:'condition',
  bids:[{price:'0.49',size:'30'}], asks:[{price:'0.51',size:'10'}]}));

function socketClass({empty=false, partial=false, reconnect=false, noPong=false, neverOpen=false,
                      emptyAfterReconnect=false} = {}) {
  let count = 0;
  return class extends EventTarget {
    constructor() {
      super();
      this.readyState = 0;
      this.index = ++count;
      if (!neverOpen) queueMicrotask(() => {
        this.readyState = 1;
        this.dispatchEvent(new Event('open'));
      });
    }
    send(data) {
      if (data === 'PING') {
        if (!noPong) queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', {data:'PONG'})));
        return;
      }
      const subscription = JSON.parse(data);
      assert.deepEqual(subscription.assets_ids, ['123','456']);
      if (!empty && !(emptyAfterReconnect && this.index > 1)) queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', {
        data: JSON.stringify(partial ? snapshots.slice(0,1) : snapshots)})));
      if (reconnect && this.index === 1) setTimeout(() => this.close(), 5);
    }
    close() {
      if (this.readyState === 3) return;
      this.readyState = 3;
      this.dispatchEvent(Object.assign(new Event('close'), {code:1000}));
    }
  };
}

async function capture(t, socketOptions = {}, extra = {}) {
  const folder = mkdtempSync(join(tmpdir(), 'orderbook-capture-test-'));
  t.after(() => rmSync(folder, {recursive:true,force:true}));
  const output = join(folder, 'session');
  const result = await runCapture({targets:['test-market'], minutes:0.002, output, ...extra}, {
    WebSocketImpl:socketClass(socketOptions), fetchImpl:fakeFetch, log:()=>{},
    heartbeatMs:10, heartbeatTimeoutMs:25, openTimeoutMs:30,
    snapshotTimeoutMs:40, reconnectDelayMs:5, progressMs:1000,
  });
  return {result, output};
}

test('CLI requires an explicit target and rejects invalid durations', () => {
  assert.throws(() => parseOptions([]), /--market/);
  assert.throws(() => parseOptions(['--market','test','--minutes','NaN']), /1440/);
  assert.equal(parseOptions(['80','test-market']).minutes,80);
  assert.equal(parseOptions(['--market','https://polymarket.com/event/test']).targets.length,1);
});

test('URL resolution distinguishes an event from an individual question', () => {
  assert.deepEqual(targetReference('https://polymarket.com/event/election/question?tid=1'),
    {slug:'question',event:false});
  assert.deepEqual(targetReference('https://polymarket.com/event/election'), {slug:'election',event:true});
  assert.throws(() => targetReference('https://example.com/event/test'), /polymarket.com/);
  assert.throws(() => marketMetadata({...metadata, closed:true}), /활성/);
  assert.throws(() => marketMetadata({...metadata, outcomes:'["Yes","Yes"]'}), /매핑/);
});

test('requesting an unavailable market fails instead of silently skipping it', async () => {
  let calls=0;
  await assert.rejects(resolveMarkets(['first','second'], async () =>
    new Response(JSON.stringify(++calls===1 ? metadata : {...metadata, closed:true}))), /활성/);
});

test('event links expand to active binary questions', async () => {
  const markets = await resolveMarkets(['https://polymarket.com/event/election'], async url => {
    assert.match(url, /events\/slug\/election$/);
    return new Response(JSON.stringify({markets:[metadata,{...metadata,closed:true}]}));
  });
  assert.equal(markets.length,1);
});

test('actual market messages are persisted before success with receipt clocks', async t => {
  const {result,output} = await capture(t);
  assert.equal(result.state,'complete');
  assert.equal(result.snapshot_tokens,2);
  assert.equal(result.event_counts.book,2);
  const rows = readFileSync(join(output,'events.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(rows.at(-1).kind,'end');
  assert.equal(rows.filter(r=>r.kind==='message').length,1);
  assert.match(rows[0].received_monotonic_ns,/^\d+$/);
  assert.equal(JSON.parse(readFileSync(join(output,'status.json'),'utf8')).state,'complete');
});

test('no payload and missing one outcome cannot be reported complete', async t => {
  assert.equal((await capture(t,{empty:true})).result.state,'empty');
  assert.equal((await capture(t,{partial:true})).result.state,'partial');
});

test('a disconnected stream reconnects and records separate connection boundaries', async t => {
  const {result,output} = await capture(t,{reconnect:true});
  assert.ok(result.connections>=2);
  const rows = readFileSync(join(output,'events.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
  assert.ok(rows.some(r=>r.kind==='disconnect' && r.connection===1));
  assert.ok(rows.some(r=>r.kind==='connect' && r.connection===2));
});

test('a hung connection exits at the deadline', async t => {
  assert.equal((await capture(t,{neverOpen:true})).result.state,'empty');
});

test('previous snapshots cannot hide an incomplete final connection', async t => {
  const {result} = await capture(t,{reconnect:true,emptyAfterReconnect:true});
  assert.equal(result.ever_snapshot_tokens,2);
  assert.equal(result.state,'partial');
});

test('missed heartbeat reconnects and external interruption flushes the log', async t => {
  assert.ok((await capture(t,{noPong:true})).result.connections>=2);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(),50);
  t.after(()=>clearTimeout(timer));
  const {result,output} = await capture(t,{}, {signal:controller.signal});
  assert.equal(result.state,'stopped');
  assert.equal(result.stop_reason,'interrupted');
  assert.equal(JSON.parse(readFileSync(join(output,'events.jsonl'),'utf8').trim().split('\n').at(-1)).kind,'end');
});
