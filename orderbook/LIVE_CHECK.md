# 실제 실시간 접속 확인

## 2026-11-14 실행 기록

- 시작 UTC: 2026-11-14T04:08:30.549Z
- 실행 환경: Python 3.13.3 / Node.js v24.18.0
- 수집 타이머: 30초. 마켓 조회는 별도이며 WebSocket 연결 시간은 타이머에 포함됩니다.
- 상태: complete. 토큰 초기 호가 4/4 수신.
- 원본 메시지: 261개. 지정한 마켓의 호가·체결 관련 메시지: 248개.
- CSV 호가: 199행. 분석 관측: 50행.
- 수집·CSV·분석 단계가 모두 실행됐습니다. 이는 접속과 파일 생성 검사이며 수익성 검증이 아닙니다.

연결 시험 대상은 JD Vance 및 Gavin Newsom의 2028 미국 대선 당선 여부 두 질문입니다. 기존 2026 선거 분석을 대체하는 연구 표본이 아닙니다. 연구 대상 시장은 사용자가 명시적으로 지정합니다.

## 자동 검증

Python 24개, Node.js 10개 테스트와 Node.js 문법 검사를 실행합니다. 검증 내용은 호가 불일치 격리·스냅샷 회복, 시각 정렬과 진입 지연, 신호 전환·가격 경로, CSV 오류 구간 제외, 마켓 조회 실패 처리, 초기 호가 누락, 재연결, heartbeat 누락, 열린 연결의 시간 초과, 중단 시 원본 flush입니다.

실제 활성 JD Vance 질문에서 실행 중 SIGINT를 보내 수집 중단, 원본 flush, CSV 생성, 종료 코드 0도 확인했습니다. 단절·heartbeat·누락은 모의 소켓으로 재현한 자동 테스트입니다. 실제 접속 시험은 위 두 질문에서 수행했고 네트워크 단절을 실제로 유발하지 않았습니다. 장시간 서버 운영과 서로 다른 네트워크에서의 동작까지 검증한 것은 아닙니다.

## 재현

```sh
python3 -m unittest -v
node --test test_capture.mjs
node --check capture.mjs
python3 run.py --markets-file markets.example.txt --minutes 0.5
```

접속 시점에 예시 질문이 종료되었거나 네트워크 접근이 제한되면 활성 링크/연결 환경을 확인합니다. 실패 시 수집 대상이 자동으로 다른 시장으로 바뀌지 않습니다. 실시간 원본과 생성 결과는 Git에 포함하지 않습니다.

프로토콜 기준: [Polymarket 공식 Real-Time Data 문서](https://docs.polymarket.com/market-data/realtime-data)의 Market Stream / API 항목. 공개 market 소켓의 assets_ids 구독, 10초 PING/PONG, book 및 price_change 메시지를 사용합니다.
