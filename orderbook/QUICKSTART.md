# 대표·팀원용 실시간 수집 안내

활성 Polymarket Yes/No 질문의 호가와 체결 알림을 수집합니다. 실행 중에는 원본이 계속 저장되고, 종료하면 팀 양식 CSV와 탐색 분석을 생성합니다. 자동으로 주문을 내거나 매수 추천을 하지 않습니다.

## 처음 한 번

Python 3.10 이상과 Node.js 22.15 이상이 필요합니다. 추가 패키지·API 키는 사용하지 않습니다.

```sh
git clone --branch seungwoo-orderbook https://github.com/emanepe/elections.git
cd elections/orderbook
python3 run.py --markets-file markets.example.txt --check
```

마지막 명령은 예시 미국 대선 질문 두 개로 15초 실제 수신·CSV 생성 시험을 합니다. 예시는 연결 확인용 2028 마켓입니다. 과거 2026 선거 분석 대상을 바꾸어 해석하는 용도가 아닙니다. 시간이 지나 예시가 종료되면 활성 링크로 교체해야 합니다. 기존 저장소를 쓰는 경우 해당 저장소에서 `git fetch origin` 후 `git switch --track origin/seungwoo-orderbook`으로 이동합니다. 이미 같은 이름의 로컬 브랜치가 있으면 `git switch seungwoo-orderbook`을 사용합니다.

## 실제 연구 대상 수집

대표가 정한 활성 질문 링크 또는 slug를 `markets.txt`에 한 줄씩 적습니다. 예시를 복사해 수정할 수 있습니다.

```sh
cp markets.example.txt markets.txt
```

파일을 실제 연구 대상 목록으로 수정한 다음 실행합니다.

```sh
python3 run.py --markets-file markets.txt --minutes 80
```

질문 하나를 직접 지정할 수도 있습니다.

```sh
python3 run.py --market "실제 활성 질문 링크" --minutes 80
```

위 링크 자리표시자는 실제 주소로 바꿉니다. `--market`은 여러 번 쓸 수 있습니다. 이벤트 링크는 그 안의 활성 Yes/No 질문 전체로 확장하며, 최대 20질문까지 지원합니다. 20개를 넘는 이벤트는 개별 질문을 지정합니다. 지정한 마켓 조회에 실패하면 조용히 빼지 않고 오류로 중단합니다. 이벤트 안에서 이미 종료된 질문은 수집하지 않습니다.

2026년 11월 4일 선거가 종료된 질문은 지금 실시간으로 다시 수집할 수 없습니다. 과거 데이터 분석에는 기존 로그를 사용하고, 새 실시간 연구 대상은 별도로 정해야 합니다.

## 수집 중 확인할 것

콘솔에서 연결 상태·수신 건수·초기 호가 수신 수가 보입니다. 질문 두 개라면 Yes/No 총 네 토큰에 대해 `초기 호가 4/4`를 확인합니다. 표시되는 5초는 화면 갱신 주기이며 데이터 수집 간격이 아닙니다. 서버가 보내는 WebSocket 메시지는 도착할 때마다 기록합니다. 거래가 없는 시장은 새 메시지가 적을 수 있습니다.

`status.json`에 마지막 수신 시각·오류·재연결 횟수가 갱신됩니다. 연결이 끊기면 다시 연결하고, 끊긴 구간을 분석에서 이어 붙이지 않습니다. 일부 초기 호가를 못 받거나 유효 메시지가 없으면 성공으로 표시하지 않습니다.

## 끝난 뒤 열 파일

콘솔에 출력되는 `ws_data/<세션>/` 폴더를 엽니다.

| 파일 | 확인 목적 |
|---|---|
| `csv/polymarket_election_quotes_날짜.csv` | 가격·매수/매도 대기 수량. 잔량 연구에는 이 파일 사용 |
| `csv/polymarket_election_trades_날짜.csv` | 공개 체결 알림. 체결이 없으면 헤더만 있을 수 있음 |
| `analysis/summary.csv` | 우세 신호별 이후 방향과 모의 손익 집계 |
| `analysis/failed_signals.csv` | 예상과 반대이거나 보합인 사례, 중간 경로·잔량 변화 |
| `status.json`, `run_summary.json` | 수집·CSV·분석 상태를 각각 확인 |
| `metadata.json`, `events.jsonl` | 수집 대상과 원본. 재분석을 위해 보관 |

팀 양식의 `up`은 Yes, `down`은 No입니다. 민주당·공화당이라는 뜻이 아닙니다. 각 호가 행에는 그 이벤트에서 갱신된 outcome만 넣으므로 반대쪽 열이 비어 있을 수 있습니다. 빈 칸을 0이나 같은 시각의 반대 호가로 해석하지 않습니다. CSV는 종료 후 생성되며 수집 중 보려면 콘솔과 status.json을 확인합니다. 중단은 Ctrl+C 한 번입니다. 받은 원본을 보관하고 CSV 생성이 끝날 때까지 기다립니다.

수집은 정상이어도 우세 신호나 유효 비교 구간이 부족하면 분석이 `no_usable_windows`일 수 있습니다. 분석 프로그램 자체가 실패하면 `failed`와 0이 아닌 종료 코드로 알립니다. 파일이 있다고 전략이 검증됐다는 뜻은 아닙니다. 왕복 비용과 진입 지연은 기본값 0이며, 별도 가정으로 조절하는 방법은 [README](README.md)를 참고합니다.

## 계속 켜둘 때

프로그램을 실행한 컴퓨터와 인터넷 연결이 유지되어야 합니다. Mac 절전 방지가 필요하면 아래처럼 실행합니다.

```sh
caffeinate -i python3 run.py --markets-file markets.txt --minutes 80
```

터미널을 닫거나 컴퓨터가 꺼지면 상시 수집이 유지되지 않습니다. 서버 자동 실행·24시간 운영은 별도 설정이 필요합니다. 원본은 실시간으로 저장되므로 강제 종료된 세션도 재분석할 수 있지만 마지막 미완성 줄은 제외됩니다.

마켓 조회 HTTP 오류, `empty`, `partial`, 저장 오류가 나면 정상 완료로 취급하지 말고 오류 출력과 상태 파일을 확인합니다. 실제 접속 시험 범위는 [LIVE_CHECK.md](LIVE_CHECK.md)에 있습니다.
