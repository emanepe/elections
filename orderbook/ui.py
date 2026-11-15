#!/usr/bin/env python3
"""Local browser controls for the existing capture → CSV → analysis runner."""
import argparse
import csv
import errno
import hmac
import json
import math
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from collect import GAMMA, active_market, array_field, market_slug

HERE = Path(__file__).resolve().parent
STATIC = HERE / 'ui'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temp.replace(path)


def capture_config(data):
    if not isinstance(data, dict):
        raise ValueError('수집 설정이 올바르지 않습니다.')
    targets = data.get('targets')
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise ValueError('활성 마켓 링크 또는 slug를 1~20개 넣어 주세요.')
    for target in targets:
        if not isinstance(target, str) or not target.strip() or len(target) > 600:
            raise ValueError('마켓 링크를 확인해 주세요.')
        market_slug(target.strip())
    minutes = data.get('minutes', 80)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or not 0 < minutes <= 1440:
        raise ValueError('수집 시간은 0보다 크고 1440분 이하여야 합니다.')
    latency = data.get('latency_ms', 0)
    if isinstance(latency, bool) or not isinstance(latency, int) or not 0 <= latency < 100:
        raise ValueError('가정 진입 지연은 0~99ms로 설정해 주세요.')
    try:
        fee = Decimal(str(data.get('fee_cents', '0')))
    except InvalidOperation:
        raise ValueError('가정 왕복 비용을 숫자로 넣어 주세요.') from None
    if not fee.is_finite() or not 0 <= fee <= 100:
        raise ValueError('가정 왕복 비용은 0~100센트로 설정해 주세요.')
    return dict(targets=list(dict.fromkeys(t.strip() for t in targets)), minutes=minutes,
                latency_ms=latency, fee_cents=str(fee))


def search_markets(query):
    query = query.strip()
    if not query or len(query) > 150:
        raise ValueError('영어 검색어를 150자 이내로 입력해 주세요.')
    params = dict(q=query, events_status='active', limit_per_type=5, page=1,
                  search_profiles='false', search_tags='false')
    req = Request(GAMMA+'/public-search?'+urlencode(params),
                  headers={'User-Agent':'OrderbookResearchUI/1.0','Accept':'application/json'})
    with urlopen(req, timeout=12) as response:
        result = json.load(response)
    found = {}
    for event in result.get('events') or []:
        for market in event.get('markets') or []:
            if not active_market(market) or not market.get('slug'):
                continue
            try:
                outcomes = array_field(market.get('outcomes'), 'outcomes')
                if {str(x).lower() for x in outcomes} != {'yes','no'} or len(outcomes) != 2:
                    continue
            except ValueError:
                continue
            slug = market['slug']
            found[slug] = dict(slug=slug, question=market.get('question', slug),
                               event_title=event.get('title',''))
    return list(found.values())[:40]


@dataclass
class Job:
    id: str
    root: Path
    config: dict
    started_at: str
    process: object = None
    finished_at: str = None
    returncode: int = None
    stop_requested: bool = False
    logs: deque = field(default_factory=lambda: deque(maxlen=160))


class JobManager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.active_id = None
        self.closing = False
        for path in sorted(self.root.glob('ui-*/job.json'))[-100:]:
            record = read_json(path)
            if not record or record.get('id') != path.parent.name:
                continue
            job = Job(id=record['id'], root=path.parent, config=record.get('config',{}),
                      started_at=record.get('started_at',''), finished_at=record.get('finished_at'),
                      returncode=record.get('returncode'), stop_requested=record.get('stop_requested',False))
            if not job.finished_at:
                job.finished_at = now()
                job.returncode = -1  # Never assume an interrupted server session completed.
            log_path = job.root/'console.log'
            if log_path.exists():
                with log_path.open(encoding='utf-8', errors='replace') as log:
                    job.logs.extend(deque(log, maxlen=160))
            self.jobs[job.id] = job

    def _persist(self, job):
        write_json(job.root/'job.json', dict(id=job.id, config=job.config,
                   started_at=job.started_at, finished_at=job.finished_at,
                   returncode=job.returncode, stop_requested=job.stop_requested))

    def start(self, config):
        config = capture_config(config)
        with self.lock:
            if self.closing:
                raise RuntimeError('화면 서버를 종료하는 중입니다.')
            if self.active_id:
                raise RuntimeError('현재 수집 또는 파일 정리가 끝난 뒤 시작해 주세요.')
            job_id = 'ui-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8]
            root = self.root/job_id
            root.mkdir()
            job = Job(job_id, root, config, now())
            command = [sys.executable,'-u',str(HERE/'run.py'),'--output',str(root),
                       '--minutes',str(config['minutes']),'--latency-ms',str(config['latency_ms']),
                       '--round-trip-fee-cents',config['fee_cents']]
            for target in config['targets']:
                command.extend(['--market',target])
            self._persist(job)
            try:
                job.process = subprocess.Popen(command, cwd=HERE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                    start_new_session=(os.name == 'posix'))
            except OSError as exc:
                job.finished_at, job.returncode = now(), 1
                job.logs.append(str(exc))
                self.jobs[job_id] = job
                self._persist(job)
                raise RuntimeError('수집 프로그램을 시작하지 못했습니다: '+str(exc)) from exc
            self.jobs[job_id] = job
            self.active_id = job_id
            threading.Thread(target=self._watch, args=(job,), daemon=True).start()
            return job_id

    def _watch(self, job):
        try:
            with (job.root/'console.log').open('w', encoding='utf-8', buffering=1) as log:
                for line in job.process.stdout:
                    log.write(line)
                    with self.lock:
                        job.logs.append(line.rstrip())
        finally:
            code = job.process.wait()
            job.process.stdout.close()
            with self.lock:
                job.returncode, job.finished_at = code, now()
                self._persist(job)
                if self.active_id == job.id:
                    self.active_id = None

    def stop(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError('수집 기록을 찾지 못했습니다.')
            job = self.jobs[job_id]
            if job.finished_at or job.stop_requested:
                return
            session = self.session(job)
            capture = read_json(session/'status.json') if session else {}
            if capture.get('state') in ('complete','stopped','partial','empty','failed'):
                raise RuntimeError('이미 수집을 마치고 CSV·분석을 생성하는 중입니다.')
            if not job.process or job.process.poll() is not None:
                return
            job.stop_requested = True
            self._persist(job)
            try:
                job.process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    @staticmethod
    def session(job):
        for path in sorted(job.root.glob('*/metadata.json')):
            if path.parent.resolve().is_relative_to(job.root.resolve()):
                return path.parent
        return None

    def files(self, job):
        result = {}
        session = self.session(job)
        if session:
            candidates = [session/name for name in ('metadata.json','status.json','run_summary.json','events.jsonl')]
            candidates += list((session/'csv').glob('*.csv'))
            candidates += list((session/'analysis').glob('*.csv'))
            candidates += list((session/'analysis').glob('*.json'))
            for path in candidates:
                if path.is_file() and path.resolve().is_relative_to(session.resolve()):
                    result[str(path.relative_to(session))] = path
        log = job.root/'console.log'
        if log.is_file() and log.resolve().is_relative_to(job.root.resolve()):
            result['console.log'] = log
        return result

    def file(self, job_id, name):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError('수집 기록을 찾지 못했습니다.')
            path = self.files(self.jobs[job_id]).get(name)
            if not path:
                raise KeyError('해당 결과 파일을 찾지 못했습니다.')
            return path

    def view(self, job):
        session = self.session(job)
        capture = read_json(session/'status.json') if session else {}
        summary = read_json(session/'run_summary.json') if session else {}
        metadata = read_json(session/'metadata.json') if session else {}
        if job.finished_at:
            state = ('stopped' if job.stop_requested else 'complete') if summary.get('successful') else 'failed'
        elif job.stop_requested:
            state = 'stopping'
        elif capture.get('state') in ('complete','stopped','empty','partial','failed'):
            state = 'processing'
        else:
            state = capture.get('state','starting')
        preview = []
        if session and (session/'analysis'/'summary.csv').is_file():
            with (session/'analysis'/'summary.csv').open(encoding='utf-8-sig', newline='') as stream:
                for index, row in enumerate(csv.DictReader(stream)):
                    if index >= 500:
                        break
                    if row.get('period') == 'all' and row.get('spread_group') == 'all':
                        preview.append(row)
        files = [dict(name=name, size=path.stat().st_size) for name,path in self.files(job).items()]
        return dict(id=job.id, config=job.config, started_at=job.started_at,
                    finished_at=job.finished_at, returncode=job.returncode,
                    state=state, capture=capture, summary=summary, metadata=metadata,
                    files=files, preview=preview, logs=list(job.logs))

    def snapshot(self, selected=None):
        with self.lock:
            ordered = sorted(self.jobs.values(),key=lambda j:j.id,reverse=True)
            job_id = selected or self.active_id or (ordered[0].id if ordered else None)
            if job_id and job_id not in self.jobs:
                raise KeyError('수집 기록을 찾지 못했습니다.')
            job = self.jobs.get(job_id)
            return dict(active_job_id=self.active_id, jobs=[dict(id=j.id, started_at=j.started_at,
                        finished=bool(j.finished_at)) for j in ordered], current=self.view(job) if job else None)

    def shutdown(self):
        with self.lock:
            self.closing = True
            job_id = self.active_id
            process = self.jobs[job_id].process if job_id else None
        if job_id:
            try:
                self.stop(job_id)
            except RuntimeError:
                pass
        if process:
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.terminate()


def handler_for(manager, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def valid_host(self):
            port = self.server.server_address[1]
            return self.headers.get('Host') in (f'127.0.0.1:{port}',f'localhost:{port}')

        def headers_for(self, code, mime, size=None):
            self.send_response(code)
            self.send_header('Content-Type',mime)
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if size is not None:
                self.send_header('Content-Length',str(size))

        def json(self, code, value):
            body = json.dumps(value,ensure_ascii=False).encode('utf-8')
            self.headers_for(code,'application/json; charset=utf-8',len(body))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if not self.valid_host():
                    return self.json(403,dict(error='이 컴퓨터의 화면 주소로 접속해 주세요.'))
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == '/api/health':
                    return self.json(200,dict(app='orderbook-ui',version=1))
                if parsed.path == '/api/state':
                    return self.json(200,manager.snapshot(query.get('job',[None])[0]))
                if parsed.path == '/api/search':
                    return self.json(200,dict(markets=search_markets(query.get('q',[''])[0])))
                if parsed.path == '/api/download':
                    path = manager.file(query.get('job',[''])[0],query.get('file',[''])[0])
                    mime = 'text/csv; charset=utf-8' if path.suffix == '.csv' else 'application/octet-stream'
                    with path.open('rb') as stream:
                        remaining = os.fstat(stream.fileno()).st_size
                        self.headers_for(200,mime,remaining)
                        self.send_header('Content-Disposition',f'attachment; filename="{path.name}"')
                        self.end_headers()
                        while remaining and (chunk := stream.read(min(65536,remaining))):
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
                static = {'/':('index.html','text/html; charset=utf-8'),
                          '/app.js':('app.js','text/javascript; charset=utf-8'),
                          '/app.css':('app.css','text/css; charset=utf-8')}
                if parsed.path in static:
                    name,mime = static[parsed.path]
                    body = (STATIC/name).read_text(encoding='utf-8')
                    if name == 'index.html':
                        body = body.replace('__LOCAL_TOKEN__',token)
                    encoded = body.encode('utf-8')
                    self.headers_for(200,mime,len(encoded))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                self.json(404,dict(error='페이지를 찾지 못했습니다.'))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except KeyError as exc:
                self.json(404,dict(error=str(exc.args[0])))
            except ValueError as exc:
                self.json(400,dict(error=str(exc)))
            except (HTTPError, URLError, TimeoutError) as exc:
                self.json(502,dict(error='마켓 검색에 연결하지 못했습니다. 링크를 직접 넣거나 잠시 뒤 다시 시도해 주세요.',detail=str(exc)))
            except OSError as exc:
                self.json(500,dict(error='파일을 읽지 못했습니다.',detail=str(exc)))

        def do_POST(self):
            try:
                if not self.valid_host() or not hmac.compare_digest(self.headers.get('X-Local-Token',''),token):
                    return self.json(403,dict(error='화면을 새로고침한 뒤 다시 시도해 주세요.'))
                port = self.server.server_address[1]
                if self.headers.get('Origin') not in (None,f'http://127.0.0.1:{port}',f'http://localhost:{port}'):
                    return self.json(403,dict(error='이 컴퓨터의 화면에서 실행해 주세요.'))
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 16384:
                    return self.json(400,dict(error='요청 크기를 확인해 주세요.'))
                data = json.loads(self.rfile.read(length))
                if not isinstance(data,dict):
                    raise ValueError('설정을 확인해 주세요.')
                if self.path == '/api/start':
                    return self.json(202,dict(job_id=manager.start(data)))
                if self.path == '/api/stop':
                    manager.stop(data.get('job_id',''))
                    return self.json(202,dict(stopping=True))
                self.json(404,dict(error='요청을 찾지 못했습니다.'))
            except KeyError as exc:
                self.json(404,dict(error=str(exc.args[0])))
            except (ValueError, TypeError) as exc:
                self.json(400,dict(error=str(exc)))
            except RuntimeError as exc:
                self.json(409,dict(error=str(exc)))
            except OSError as exc:
                self.json(500,dict(error='수집 프로그램을 시작하지 못했습니다.',detail=str(exc)))
    return Handler


def main():
    parser = argparse.ArgumentParser(description='버튼으로 사용하는 로컬 Polymarket 수집 화면')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',type=Path,default=HERE/'ws_data')
    parser.add_argument('--no-browser',action='store_true')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('포트는 0~65535 사이여야 합니다.')
    manager = JobManager(args.data_dir)
    handler = handler_for(manager,secrets.token_hex(24))
    try:
        server = ThreadingHTTPServer(('127.0.0.1',args.port),handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        existing_url = f'http://127.0.0.1:{args.port}'
        try:
            with urlopen(existing_url+'/api/health',timeout=2) as response:
                existing = json.load(response)
        except (OSError,ValueError):
            existing = {}
        if existing.get('app') == 'orderbook-ui':
            print('이미 실행 중인 수집 화면:',existing_url,flush=True)
            if not args.no_browser:
                webbrowser.open(existing_url)
            return
        server = ThreadingHTTPServer(('127.0.0.1',0),handler)
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_address[1]}'
    print('호가 수집 화면:',url,flush=True)
    print('브라우저에서 시작·중지·결과 다운로드를 사용하세요. 서버 종료는 Ctrl+C입니다.',flush=True)
    if not args.no_browser:
        threading.Timer(0.4,lambda:webbrowser.open(url)).start()
    def request_stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,request_stop)
    if hasattr(signal,'SIGHUP'):
        signal.signal(signal.SIGHUP,request_stop)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print('\n진행 중인 수집을 저장하고 종료합니다.',flush=True)
    finally:
        server.server_close()
        manager.shutdown()


if __name__ == '__main__':
    main()
