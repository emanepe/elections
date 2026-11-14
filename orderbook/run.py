#!/usr/bin/env python3
"""One-command live capture, CSV export, and exploratory signal analysis."""
import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from diagnose import HORIZONS

HERE = Path(__file__).resolve().parent


def parser():
    p = argparse.ArgumentParser(description='활성 Polymarket 링크 → 실시간 수집 → CSV → 탐색 분석')
    p.add_argument('--market', action='append', default=[], help='개별 질문/이벤트 링크 또는 slug. 반복 가능')
    p.add_argument('--markets-file', type=Path, help='한 줄에 링크 또는 slug 하나. #으로 시작하는 줄은 무시')
    p.add_argument('--minutes', type=float, default=80, help='수집 시간(분), 기본 80')
    p.add_argument('--check', action='store_true', help='15초 실제 연결·수신·CSV 시험. 전략 검증용 표본이 아닙니다')
    p.add_argument('--output', type=Path, default=HERE/'ws_data', help='새 세션들을 저장할 상위 폴더')
    p.add_argument('--no-analysis', action='store_true', help='수집 후 CSV만 생성')
    p.add_argument('--latency-ms', type=int, default=0, help='분석에 가정할 진입 지연, 기본 0')
    p.add_argument('--round-trip-fee-cents', type=Decimal, default=Decimal(0), help='계약당 가정 왕복 비용(센트)')
    p.add_argument('--horizons-ms', type=int, nargs='+', default=list(HORIZONS))
    return p


def validate(args, p):
    targets = list(args.market)
    if args.markets_file:
        try:
            targets.extend(line.strip() for line in args.markets_file.read_text(encoding='utf-8-sig').splitlines()
                           if line.strip() and not line.lstrip().startswith('#'))
        except OSError as exc:
            p.error(f'마켓 목록을 읽을 수 없습니다: {exc}')
    if not targets:
        p.error('--market으로 활성 링크/slug를 지정하거나 --markets-file로 목록을 주세요')
    if not math.isfinite(args.minutes) or not 0 < args.minutes <= 1440:
        p.error('--minutes는 0보다 크고 1440 이하여야 합니다')
    if args.latency_ms < 0 or any(h <= args.latency_ms for h in args.horizons_ms):
        p.error('각 비교 기간은 0 이상인 --latency-ms보다 커야 합니다')
    if not args.round_trip_fee_cents.is_finite() or args.round_trip_fee_cents < 0:
        p.error('비용은 0 이상의 유한한 값이어야 합니다')
    return list(dict.fromkeys(targets))


def require_node():
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js가 없습니다. Node.js 22.15 이상을 설치한 뒤 다시 실행해 주세요.')
    version = subprocess.run([node, '--version'], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    numbers = tuple(int(n) for n in version.lstrip('v').split('.')[:2])
    if numbers < (22, 15):
        raise RuntimeError(f'Node.js 22.15 이상이 필요합니다. 현재 {version}')
    return node, version


def capture_wait(command):
    child = subprocess.Popen(command, cwd=HERE, start_new_session=(os.name == 'posix'))
    try:
        return child.wait()
    except KeyboardInterrupt:
        print('\n수집을 중단하고 받은 데이터를 저장합니다. CSV 생성이 끝날 때까지 기다려 주세요.', flush=True)
        try:
            child.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return child.wait()
        try:
            return child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                return child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                return 1


def has_data_row(path):
    with path.open(encoding='utf-8-sig') as stream:
        next(stream, None)
        return next(stream, None) is not None


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    targets = validate(args, p)
    try:
        node, version = require_node()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'시작 실패: {exc}', file=sys.stderr)
        return 1
    minutes = 0.25 if args.check else args.minutes
    session = args.output.resolve() / (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')+'-'+uuid4().hex[:8])
    command = [node, str(HERE/'capture.mjs'), '--minutes', str(minutes), '--output', str(session)]
    for target in targets:
        command.extend(['--market', target])
    print(f'실행 환경: Python {sys.version.split()[0]} / Node {version}', flush=True)
    if args.check:
        print('15초 실제 수신 시험입니다. 대상 시장은 자동으로 바꾸지 않습니다.', flush=True)
    code = capture_wait(command)
    status_file = session/'status.json'
    if not status_file.exists():
        print('수집을 시작하지 못했습니다. 위의 활성 마켓/네트워크 오류를 확인해 주세요.', file=sys.stderr)
        return code or 1
    status = json.loads(status_file.read_text(encoding='utf-8'))
    result = dict(capture=status, node_version=version, check_mode=args.check,
                  csv_status='not_started', analysis_status='not_requested', successful=False)
    csv_dir = session/'csv'
    try:
        exported = subprocess.run([sys.executable, str(HERE/'export_orderbook_csv.py'),
                                   str(session), '--output-dir', str(csv_dir)], cwd=HERE)
        if exported.returncode:
            result['csv_status'] = 'failed'
            code = code or exported.returncode
        else:
            quotes = next(csv_dir.glob('*_quotes_*.csv'))
            result['csv_status'] = 'complete' if has_data_row(quotes) else 'no_usable_quotes'
            if result['csv_status'] != 'complete':
                print('양방향 호가 CSV 행이 없습니다. 정상 수집으로 처리하지 않습니다.', file=sys.stderr)
                code = code or 1
            elif not args.no_analysis:
                analysis = subprocess.run([sys.executable, str(HERE/'diagnose.py'), str(session),
                    '--sampling', 'signal', '--output-dir', str(session/'analysis'),
                    '--latency-ms', str(args.latency_ms),
                    '--round-trip-fee-cents', str(args.round_trip_fee_cents),
                    '--horizons-ms', *(str(h) for h in args.horizons_ms)], cwd=HERE,
                    capture_output=True, text=True)
                if analysis.stdout:
                    print(analysis.stdout, end='')
                if analysis.stderr:
                    print(analysis.stderr, end='', file=sys.stderr)
                if analysis.returncode == 0:
                    result['analysis_status'] = 'complete'
                elif analysis.stderr.strip() == 'No usable quote windows found.':
                    result['analysis_status'] = 'no_usable_windows'
                    print('수집은 됐지만 분석할 우세 신호·비교 구간이 부족합니다.', file=sys.stderr)
                else:
                    result['analysis_status'] = 'failed'
                    code = code or analysis.returncode
    except (OSError, StopIteration, ValueError) as exc:
        result['processing_error'] = str(exc)
        code = code or 1
    result['successful'] = (code == 0 and status['state'] in ('complete', 'stopped')
                            and result['csv_status'] == 'complete')
    (session/'run_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('결과 폴더:', session)
    print('CSV:', csv_dir)
    print('수집 상태:', status['state'], '/ CSV:', result['csv_status'], '/ 분석:', result['analysis_status'])
    if not result['successful']:
        print('완료 조건을 충족하지 못했습니다. status.json과 run_summary.json을 확인해 주세요.', file=sys.stderr)
        return code or 1
    print('수신·저장·CSV 생성 확인 완료. 매매 추천이나 주문 실행은 하지 않습니다.')
    return 0


if __name__ == '__main__':
    if os.name == 'posix':
        def request_stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, request_stop)
    raise SystemExit(main())
