# Polymarket Election Order Book Study

선택한 Polymarket 활성 Yes/No 마켓에서 공개 호가를 실시간 수집하고, 대기 물량의 차이와 이후 호가 중간값 변화를 비교합니다. 대표·팀원용 안내는 [QUICKSTART.md](QUICKSTART.md)에 있습니다. 아래 명령은 저장소의 `orderbook/` 폴더에서 실행합니다.

| 파일 | 용도 |
|---|---|
| `run.py` | 실시간 수집 → 팀 양식 CSV → 탐색 분석을 한 번에 실행 |
| `capture.mjs` | 지정한 활성 질문/이벤트의 Yes·No 메시지 기록, 상태 표시, 재연결 |
| `export_orderbook_csv.py` | 원본 로그를 팀 양식의 체결 CSV와 호가 CSV로 변환 |
| `diagnose.py` | 예상 방향·보합·반대 방향을 분류하고 시작·종료 호가와 잔량 비교 |
| `study.py` | 호가 복원과 기존 1·5·30초 중간값 집계 |
| `collect.py`, `analyze.py` | 기존 REST 수집 및 스냅샷 분석 |
| `FINDINGS.md` | 11월 4일 수집분의 탐색 결과와 재현 기준 |

## 실행

Node.js 22.15+와 Python 3.10+를 사용합니다. 추가 패키지와 API 키는 필요 없습니다. 실시간 수집에는 활성 상태인 마켓과 Polymarket 공개 데이터에 연결 가능한 네트워크가 필요합니다. 종료된 마켓은 이미 수집한 로그로 재현합니다.

```sh
python3 run.py --market "활성 마켓 링크 또는 slug" --minutes 80
```

80은 수집 시간(분)입니다. 링크/slug는 실제 값으로 바꿉니다. `--check`로 15초 연결·CSV 생성 시험을 먼저 할 수 있습니다. 여러 질문은 `--market`을 반복하거나 `--markets-file markets.txt`를 사용합니다. 종료된 2026 선거를 기본값으로 자동 선택하지 않습니다.

한 세션은 `ws_data/<session>/metadata.json`, `events.jsonl`, `status.json`, `run_summary.json`, `csv/`, `analysis/`로 구성됩니다. 수집 중 원본 로그와 상태 파일이 갱신되며, CSV와 분석은 수집 종료 또는 Ctrl+C 뒤에 생성됩니다. 5초 콘솔 표시는 진행 상태 갱신 주기입니다. 데이터는 WebSocket으로 도착할 때마다 기록합니다. 기존 로그를 사용하려면 아래 `SESSIONS`에 경로를 넣습니다. 수집 폴더와 생성 결과는 Git에서 제외됩니다.

```bash
SESSIONS=( ws_data/* )
python3 export_orderbook_csv.py "${SESSIONS[@]}" --output-dir exports
python3 diagnose.py "${SESSIONS[@]}" --sampling grid --output-dir results/grid_checked --cutoff-utc 2026-11-04T11:34:09Z
python3 diagnose.py "${SESSIONS[@]}" --sampling signal --output-dir results/signal_checked --cutoff-utc 2026-11-04T11:34:09Z
python3 -m unittest -v
node --test test_capture.mjs
```

- `exports/`에 팀 양식과 동일한 18열의 체결·호가 CSV를 별도로 출력합니다. 팀 예시의 체결 행은 양쪽 호가 이름의 열에 동일한 체결 가격·수량을 넣습니다. 잔량 분석에는 호가 CSV를 사용합니다. Yes는 `up`, No는 `down`으로 매핑합니다.
- `results/observations.csv`는 개별 관측입니다. 시장, 시작 UTC, 예상 방향, 실제 방향, 양쪽의 전후 호가·잔량과 확인 가능한 체결 알림을 포함합니다. `observed_changes`는 관찰된 변화입니다. 잔량 감소가 취소나 체결로 발생했는지는 이 정보만으로 단정하지 않습니다.
- `results/summary.csv`는 시장·시간 간격·신호·스프레드별 집계입니다. `results/replay_audit.json`은 메시지와 호가 불일치 집계입니다.

## 비교 방법

100ms, 500ms, 1초, 5초, 30초 뒤를 비교합니다. 이 시간은 수집 간격이 아니라 신호 관측 뒤의 비교 기간입니다. 시점별로 그때까지 받은 마지막 호가를 사용합니다. 동일 밀리초의 메시지는 마지막 상태를 사용하므로 밀리초 미만의 움직임은 분석하지 않습니다. 신호는 Yes의 최우선 매수·매도 잔량을 비교합니다. 매수 잔량이 매도 잔량의 1.5배를 넘으면 상승, 매도 잔량이 매수 잔량의 1.5배를 넘으면 하락을 예상합니다. 기본 불균형 임계값은 ±0.2이며 --threshold로 조절합니다.

- --sampling grid: 기간별로 겹치지 않는 고정 구간을 만듭니다. 신호가 없으면 중립으로 분류합니다.
- --sampling signal: 중립에서 우세로 바뀌거나 우세 방향이 뒤집힌 순간을 관측합니다. 우세가 지속되는 동안 새 신호로 세지 않습니다. 무효 구간 뒤 새로 확인된 우세도 시작점으로 취급합니다. 신호가 빠르게 반복되면 비교 구간은 겹칠 수 있으므로 독립 시행으로 해석하지 않습니다.

수신 시각과 연결별로 분리합니다. 구간 안에 무효 호가가 있으면 제외합니다. 복원 가격과 거래소가 보고한 최우선 가격이 다르면 새 전체 스냅샷을 받을 때까지 제외합니다. 과거 버전의 결과는 이 검사로 다시 생성해야 합니다.

모의 손익은 상승 신호에는 Yes, 하락 신호에는 No를 매도호가에 1계약 사고 종료 시점 매수호가에 판 것으로 계산합니다. 해당 가격대에 1계약 이상이 보일 때만 계산합니다. 기본 지연과 수수료는 0이며 실제 주문은 내지 않습니다. 최소 주문 크기, 대기열, 체결 확률, 슬리피지는 반영하지 않습니다. 방향이 맞아도 매수·매도 가격 차이 때문에 손실이 날 수 있습니다.

## 추가 진단 및 비용 시나리오

각 결과 폴더에 observations.csv, summary.csv, replay_audit.json 외에 아래 파일을 저장합니다.

- failed_signals.csv: 보합·반대 사례만 모은 파일입니다. first_expected_ms·first_opposite_ms는 처음 해당 방향으로 움직인 시간, imbalance_weakened_ms는 우세가 임계값 이하로 약해진 시간입니다. 빈 값은 해당 변화가 관측되지 않았다는 뜻입니다. max_expected_move_cents·max_adverse_move_cents는 시작 중간값 대비 최대 이동 폭입니다. 끝값이 보합이어도 중간에 움직였다 돌아왔는지 확인할 수 있습니다.
- settings.json: 임계값·지연·가정 비용·분석 기간·원본 경로를 보관합니다.
- replay_audit.json의 snapshot_recovered: 가격 불일치 뒤 전체 호가를 다시 받아 회복한 횟수입니다.

아래는 진입을 70ms 지연시키고 왕복 비용을 계약당 0.2센트로 가정하는 민감도 분석입니다. 실제 거래소 수수료나 주문 지연 측정값이 아닙니다. 종료는 원래 신호 시점에서 지정 기간 뒤이며, 진입·종료 모두 해당 시점까지 받은 호가를 씁니다.

```bash
python3 diagnose.py "${SESSIONS[@]}" --sampling signal --latency-ms 70 --round-trip-fee-cents 0.2 --horizons-ms 100 500 1000 5000 30000 --output-dir results/cost_scenario
```

simulated_gross_cents는 비용 전, simulated_net_cents는 지정 비용 차감 후 계약당 손익입니다. summary.csv의 스프레드 그룹은 신호 시점 Yes 기준입니다. 실제 진입 대상 Yes 또는 No의 호가 간격은 simulated_entry_spread_cents에 별도로 남깁니다. 데이터는 단일 날짜의 탐색 결과이며 별도 날짜와 시장에서 재검증해야 합니다.

## 실시간 운영 상태

`status.json`은 수신 메시지, 구독 대상의 유효 메시지, 현재 연결의 초기 호가 수신 토큰 수, 연결·재연결 횟수, 마지막 수신·오류, 저장 바이트를 기록합니다. 일정 시간마다 원자적으로 교체합니다. 전체 초기 호가를 받지 못했거나 유효 메시지가 0개면 종료 코드는 0이 아니며 `partial` 또는 `empty`로 남깁니다. 종료 시 연결이 복구되지 않은 경우도 `partial`입니다. `run_summary.json`에서 수집·CSV·분석의 성공 여부를 따로 확인합니다. 신호가 없으면 정상 수집이어도 분석 파일이 없을 수 있으며 `no_usable_windows`로 기록합니다. 분석 프로그램 오류는 별도로 `failed`와 0이 아닌 종료 코드로 알립니다.

서버 메시지 원문을 먼저 보관하고, CSV에서도 복원 호가 불일치 후 전체 스냅샷 회복 전까지 제외합니다. 타임스탬프는 수신 시각(밀리초)과 로컬 단조 시계(ns)를 함께 기록합니다. ns는 거래소 발생 시각이나 지연 0을 의미하지 않습니다.

공식 [Market Stream 문서](https://docs.polymarket.com/market-data/realtime-data)의 공개 WebSocket 구독 및 10초 PING 절차를 사용합니다. 실제 API·WebSocket 시험과 자동 검증 범위는 [LIVE_CHECK.md](LIVE_CHECK.md)에 기록합니다.

## 저장소에 포함된 분석 자료

[11월 4일 보고서](FINDINGS.md)와 [시장·신호별 집계 CSV](reports/20261104/)를 포함합니다. 원본 로그와 개별 관측 CSV는 용량이 커서 Git에 포함하지 않습니다. 기존 로컬 데이터는 SESSIONS에 실제 경로를 지정해 사용합니다. 과거 네 마켓은 11월 4일 자료의 대상입니다. 새 수집은 활성 마켓 slug를 직접 지정합니다. 예: `node capture.mjs 80 MARKET_SLUG_1 MARKET_SLUG_2` (대문자 자리표시자는 실제 slug로 바꿉니다).
