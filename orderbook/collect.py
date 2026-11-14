#!/usr/bin/env python3
"""Public Polymarket order-book snapshots. Python 3.10+, standard library only."""

import argparse
import csv
import json
import math
import re
import sys
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
VERSION = "1.0.0"
FIELDS = [
    "session_id", "cycle_id", "received_at_utc", "received_at_unix",
    "request_started_at_utc", "request_latency_ms", "source_timestamp_raw",
    "source_time_utc", "book_age_seconds", "market_slug", "question",
    "condition_id", "token_id", "outcome", "book_hash", "tick_size",
    "best_bid", "bid_size", "best_ask", "ask_size", "mid", "spread",
    "top_levels", "bid_depth", "ask_depth", "bid_notional", "ask_notional",
    "imbalance_l1", "imbalance_depth", "bid_level_count", "ask_level_count",
    "invalid_level_count", "quality", "valid_for_analysis", "last_trade_price",
]


class DataError(ValueError):
    pass


def utc(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts,
                                  timezone.utc).isoformat(timespec="milliseconds")


def get_json(base, path, params=None):
    """Only GET to the two fixed public data services; never accepts credentials."""
    if base not in (GAMMA, CLOB):
        raise DataError("지원하지 않는 데이터 서버입니다.")
    url = base + path + (("?" + urlencode(params)) if params else "")
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "OrderbookResearch/" + VERSION,
                                        "Accept": "application/json"}, method="GET")
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            try:
                delay = float(exc.headers.get("Retry-After", 2 ** attempt))
            except (TypeError, ValueError):
                delay = 2 ** attempt
            time.sleep(min(10, max(1, delay)))
        except URLError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def market_slug(value):
    if "://" in value:
        url = urlsplit(value)
        if url.scheme != "https" or url.hostname not in ("polymarket.com", "www.polymarket.com"):
            raise DataError("Polymarket의 https 링크 또는 마켓 slug를 넣어 주세요.")
        parts = [unquote(x) for x in url.path.split("/") if x]
        if len(parts) not in (2, 3) or parts[0] not in ("event", "market"):
            raise DataError("개별 마켓 또는 이벤트 페이지 링크를 넣어 주세요.")
        value = parts[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise DataError("마켓 slug 형식이 올바르지 않습니다.")
    return value


def array_field(value, label):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DataError(label + "를 배열로 읽을 수 없습니다.") from exc
    if not isinstance(value, list):
        raise DataError(label + " 배열이 없습니다.")
    return value


def active_market(m):
    return (m.get("active") is True and m.get("closed") is not True
            and m.get("enableOrderBook") is True
            and m.get("acceptingOrders") is not False)


def tokens_for(m):
    if not active_market(m):
        raise DataError("종료되었거나 호가창을 사용할 수 없는 마켓: " + str(m.get("slug")))
    ids = array_field(m.get("clobTokenIds"), "clobTokenIds")
    names = array_field(m.get("outcomes"), "outcomes")
    if len(ids) != len(names) or not ids or len(set(map(str, ids))) != len(ids):
        raise DataError("토큰과 outcome 대응 관계를 확인할 수 없습니다.")
    if any(not re.fullmatch(r"[0-9]+", str(t)) for t in ids):
        raise DataError("토큰 ID 형식이 올바르지 않습니다.")
    return [{"market_slug": m["slug"], "question": m.get("question", ""),
             "condition_id": str(m.get("conditionId", "")),
             "token_id": str(t), "outcome": str(n)} for t, n in zip(ids, names)]


def resolve_markets(values):
    markets = {}
    for value in values:
        slug = market_slug(value)
        try:
            found = [get_json(GAMMA, "/markets/slug/" + quote(slug, safe=""))]
        except HTTPError as exc:
            if exc.code != 404:
                raise
            event = get_json(GAMMA, "/events/slug/" + quote(slug, safe=""))
            found = [m for m in event.get("markets", []) if active_market(m)]
        if not found:
            raise DataError("수집 가능한 마켓이 없습니다: " + slug)
        for m in found:
            # A directly selected closed market is an error, not a silent omission.
            for item in tokens_for(m):
                markets[item["token_id"]] = item
    if len(markets) > 20:
        raise DataError("첫 버전은 최대 20개 outcome입니다. 이벤트 전체 대신 개별 마켓 링크를 골라 주세요.")
    return list(markets.values())


def levels(values, reverse):
    if not isinstance(values, list):
        raise DataError("호가 응답의 bids/asks 배열이 없습니다.")
    result = {}
    invalid = 0
    for item in values:
        try:
            p, s = Decimal(str(item["price"])), Decimal(str(item["size"]))
            if not p.is_finite() or not s.is_finite() or not 0 < p < 1 or s < 0:
                raise ValueError("invalid level")
            if s > 0:
                # Aggregate duplicate price levels rather than relying on list order.
                result[p] = result.get(p, Decimal(0)) + s
        except (KeyError, TypeError, ValueError, InvalidOperation):
            invalid += 1
    return sorted(result.items(), reverse=reverse), invalid


def imbalance(buy, sell):
    return (buy - sell) / (buy + sell) if buy + sell > 0 else None


def fmt(value):
    return "" if value is None else format(value, "f")


def source_time(value):
    """Keep raw time too. Accept modern UNIX seconds or milliseconds only."""
    try:
        v = float(value)
        if 1e12 <= v < 1e14:
            v /= 1000
        elif not 1e9 <= v < 1e11:
            return None
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def summarize_book(book, token, top_n):
    if not isinstance(book, dict) or book.get("error"):
        raise DataError("정상적인 호가 응답이 아닙니다.")
    if str(book.get("asset_id")) != token["token_id"]:
        raise DataError("요청한 토큰과 응답의 토큰이 다릅니다.")
    if token["condition_id"] and str(book.get("market")) != token["condition_id"]:
        raise DataError("요청한 마켓과 응답의 마켓 ID가 다릅니다.")
    bids, bad_b = levels(book.get("bids"), True)
    asks, bad_a = levels(book.get("asks"), False)
    b, bs = bids[0] if bids else (None, None)
    a, az = asks[0] if asks else (None, None)
    flags = []
    if not bids or not asks:
        flags.append("empty_book" if not bids and not asks else "one_sided")
    if b is not None and a is not None and b >= a:
        flags.append("crossed_book" if b > a else "locked_book")
    if bad_b + bad_a:
        flags.append("invalid_levels")
    valid = not flags
    bid_depth = sum((s for _, s in bids[:top_n]), Decimal(0))
    ask_depth = sum((s for _, s in asks[:top_n]), Decimal(0))
    return {
        **token, "source_timestamp_raw": book.get("timestamp", ""),
        "book_hash": book.get("hash", ""), "tick_size": book.get("tick_size", ""),
        "best_bid": fmt(b), "bid_size": fmt(bs), "best_ask": fmt(a), "ask_size": fmt(az),
        # A trade price is never substituted for an absent quote.
        "mid": fmt((b + a) / 2 if valid else None),
        "spread": fmt(a - b if b is not None and a is not None else None),
        "top_levels": top_n, "bid_depth": fmt(bid_depth), "ask_depth": fmt(ask_depth),
        "bid_notional": fmt(sum((p * s for p, s in bids[:top_n]), Decimal(0))),
        "ask_notional": fmt(sum((p * s for p, s in asks[:top_n]), Decimal(0))),
        "imbalance_l1": fmt(imbalance(bs, az) if valid else None),
        "imbalance_depth": fmt(imbalance(bid_depth, ask_depth) if valid else None),
        "bid_level_count": len(bids), "ask_level_count": len(asks),
        "invalid_level_count": bad_b + bad_a, "quality": ";".join(flags) or "ok",
        "valid_for_analysis": int(valid), "last_trade_price": book.get("last_trade_price", ""),
    }


def find_markets(args):
    data = get_json(GAMMA, "/public-search", {"q": args.query, "events_status": "active",
                    "limit_per_type": args.limit, "page": args.page,
                    "search_profiles": "false", "search_tags": "false"})
    seen = set()
    count = 0
    for event in data.get("events") or []:
        for m in event.get("markets") or []:
            if not active_market(m) or m.get("slug") in seen:
                continue
            seen.add(m["slug"])
            count += 1
            print(f"{count}. {m.get('question', m['slug'])}\n   {m['slug']}")
    if not count:
        print("검색된 활성 마켓이 없습니다. 다른 영어 검색어 또는 개별 마켓 링크를 사용해 주세요.")
    if (data.get("pagination") or {}).get("hasMore"):
        print(f"다음 검색 결과: --page {args.page + 1}")
    return 0


def collect(args):
    tokens = resolve_markets(args.market)
    for t in tokens:
        print(f"수집 대상: {t['question']} / {t['outcome']}")
    session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    folder = Path(args.output).expanduser() / session
    folder.mkdir(parents=True, exist_ok=False)
    meta = {"version": VERSION, "platform": "polymarket", "method": "REST polling",
            "session_id": session, "started_at_utc": utc(), "interval_seconds": args.interval,
            "top_levels": args.top_levels, "duration_seconds": args.duration,
            "count_limit": args.count, "tokens": tokens,
            "note": "Independent per-token snapshots; not every order event. No trades submitted."}
    (folder / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("저장 폴더:", folder.resolve(), flush=True)
    start = time.monotonic()
    deadline = None if args.count else start + args.duration
    cycles = rows = errors = 0
    disabled = set()
    interrupted = False
    with ExitStack() as stack:
        raw = stack.enter_context((folder / "raw_books.jsonl").open("w", encoding="utf-8"))
        errlog = stack.enter_context((folder / "errors.jsonl").open("w", encoding="utf-8"))
        outf = stack.enter_context((folder / "snapshots.csv").open("w", encoding="utf-8-sig", newline=""))
        writer = csv.DictWriter(outf, fieldnames=FIELDS)
        writer.writeheader()
        try:
            while (args.count is None or cycles < args.count) and (deadline is None or time.monotonic() < deadline):
                cycle_start = time.monotonic()
                cycles += 1
                for token in tokens:
                    if token["token_id"] in disabled:
                        continue
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    sent = time.time()
                    tick = time.monotonic()
                    try:
                        book = get_json(CLOB, "/book", {"token_id": token["token_id"]})
                        received = time.time()
                        latency = (time.monotonic() - tick) * 1000
                        envelope = {"session_id": session, "cycle_id": cycles,
                                    "request_started_at_utc": utc(sent), "received_at_utc": utc(received),
                                    "received_at_unix": received, "request_latency_ms": round(latency, 3),
                                    "market_slug": token["market_slug"], "token_id": token["token_id"],
                                    "outcome": token["outcome"], "response": book}
                        raw.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                        raw.flush()
                        row = summarize_book(book, token, args.top_levels)
                        ts = source_time(book.get("timestamp"))
                        row.update({k: envelope[k] for k in (
                            "session_id", "cycle_id", "request_started_at_utc", "received_at_utc",
                            "received_at_unix", "request_latency_ms")})
                        row["source_time_utc"] = utc(ts) if ts is not None else ""
                        row["book_age_seconds"] = round(received - ts, 3) if ts is not None else ""
                        writer.writerow(row)
                        outf.flush()
                        rows += 1
                    except (HTTPError, URLError, TimeoutError, DataError, ValueError) as exc:
                        errors += 1
                        status = exc.code if isinstance(exc, HTTPError) else None
                        msg = f"HTTP {status}" if status else str(exc)
                        errlog.write(json.dumps({"time_utc": utc(), "cycle_id": cycles,
                            "token_id": token["token_id"], "outcome": token["outcome"],
                            "message": msg}, ensure_ascii=False) + "\n")
                        errlog.flush()
                        print("수집 오류:", token["market_slug"], token["outcome"], msg, file=sys.stderr)
                        if status in (400, 404, 410):
                            disabled.add(token["token_id"])
                        if status in (401, 403):
                            raise DataError("공개 API 접근이 거절되었습니다. 접속 환경을 확인해 주세요.") from exc
                print(f"{cycles}회차 | 누적 {rows}행 | 오류 {errors}건", flush=True)
                if len(disabled) == len(tokens):
                    print("모든 대상의 호가창을 사용할 수 없어 종료합니다.", file=sys.stderr)
                    break
                if args.count and cycles >= args.count:
                    break
                wait = max(0, args.interval - (time.monotonic() - cycle_start))
                if deadline is not None:
                    wait = min(wait, max(0, deadline - time.monotonic()))
                time.sleep(wait)
        except KeyboardInterrupt:
            interrupted = True
            print("\n수집을 중단했습니다. 이미 저장된 기록은 유지됩니다.")
        finally:
            summary = {"ended_at_utc": utc(), "rows": rows, "errors": errors,
                       "cycles": cycles, "interrupted": interrupted,
                       "elapsed_seconds": round(time.monotonic() - start, 3)}
            (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"완료: {rows}행. {folder / 'snapshots.csv'}")
    return 0 if rows else 1


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 유한한 수를 넣어 주세요.")
    return result


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("1 이상의 정수를 넣어 주세요.")
    return result


def parser():
    p = argparse.ArgumentParser(description="선거 마켓 연구용 공개 호가 수집기 (Python 3.10 이상)")
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("find", help="영어 검색어로 활성 마켓 찾기")
    f.add_argument("query")
    f.add_argument("--limit", type=positive_int, default=5)
    f.add_argument("--page", type=positive_int, default=1)
    c = sub.add_parser("collect", help="선택한 마켓의 모든 outcome 호가 수집")
    c.add_argument("--market", action="append", required=True, help="마켓/이벤트 링크 또는 slug (반복 가능)")
    c.add_argument("--interval", type=positive, default=2, help="회차 시작 간격(초), 기본 2초")
    duration = c.add_mutually_exclusive_group()
    duration.add_argument("--duration", type=positive, default=60, help="수집 시간(초), 기본 60초")
    duration.add_argument("--count", type=positive_int, help="수집 회차 수")
    c.add_argument("--top-levels", type=positive_int, default=5, help="깊이 지표에 쓸 각 방향 가격 구간 수")
    c.add_argument("--output", default="data", help="결과 저장 폴더")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "collect" and args.interval < 1:
        parser().error("이 첫 버전의 --interval은 1초 이상으로 설정해 주세요.")
    try:
        return find_markets(args) if args.command == "find" else collect(args)
    except (DataError, HTTPError, URLError, OSError, ValueError) as exc:
        print("실행 실패:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
