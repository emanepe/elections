"""Replay Polymarket market-stream updates and compare future midpoints."""
import argparse
import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from collect import imbalance, levels

HORIZONS = (1, 5, 30)
STEP_MS = 1000
THRESHOLD = Decimal("0.2")


def number(value):
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("non-finite number")
    return parsed


def quote_state(book):
    bids, asks = book["BUY"], book["SELL"]
    if not bids or not asks:
        return None
    bid, ask = max(bids), min(asks)
    if bid >= ask:
        return None
    buy5 = sum(bids[p] for p in sorted(bids, reverse=True)[:5])
    sell5 = sum(asks[p] for p in sorted(asks)[:5])
    return ((bid + ask) / 2, imbalance(bids[bid], asks[ask]),
            imbalance(buy5, sell5), bid, ask, bids[bid], asks[ask])


def quote(book):
    state = quote_state(book)
    return state[:3] if state is not None else None


def replay(path, tokens, *, quote_fn=quote, on_trade=None, verify_quotes=False):
    books = {}
    series = defaultdict(list)
    counts = Counter()
    last_ms = {}
    desynchronized = set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            row = json.loads(line)
            ts, conn = row["received_ms"], row["connection"]
            if row["kind"] != "end":
                last_ms[conn] = ts
            if row["kind"] != "message":
                books.clear()
                desynchronized.clear()
                counts[row["kind"]] += 1
                continue
            try:
                payload = json.loads(row["raw"])
            except (TypeError, json.JSONDecodeError):
                counts["unparsed_messages"] += 1
                continue
            counts["messages"] += 1
            for event in payload if isinstance(payload, list) else [payload]:
                if not isinstance(event, dict):
                    continue
                kind = event.get("event_type")
                counts[kind or "other_event"] += 1
                touched = set()
                reported = {}
                try:
                    if kind == "book":
                        asset = event.get("asset_id")
                        if asset in tokens:
                            touched.add(asset)
                            if event.get("market") != tokens[asset]["condition_id"]:
                                raise ValueError("market mismatch")
                            bids, bad_bid = levels(event["bids"], True)
                            asks, bad_ask = levels(event["asks"], False)
                            if bad_bid or bad_ask:
                                raise ValueError("invalid snapshot")
                            books[asset] = {"BUY": dict(bids), "SELL": dict(asks)}
                            if asset in desynchronized:
                                counts["snapshot_recovered"] += 1
                            desynchronized.discard(asset)
                    elif kind == "price_change":
                        for change in event["price_changes"]:
                            asset = change.get("asset_id")
                            if asset not in tokens or asset not in books:
                                continue
                            touched.add(asset)
                            if event.get("market") != tokens[asset]["condition_id"]:
                                raise ValueError("market mismatch")
                            price = number(change["price"])
                            size = number(change["size"])
                            if not 0 < price < 1 or size < 0:
                                raise ValueError("invalid delta")
                            if verify_quotes:
                                reported[asset] = (change.get("best_bid"), change.get("best_ask"))
                            side = books[asset][change["side"]]
                            if size:
                                side[price] = size
                            else:
                                side.pop(price, None)
                    elif kind == "last_trade_price" and on_trade is not None:
                        asset = event.get("asset_id")
                        if asset in tokens and event.get("market") == tokens[asset]["condition_id"]:
                            on_trade(asset, conn, ts)
                    elif kind == "market_resolved":
                        for asset in event.get("assets_ids", []):
                            if asset in tokens:
                                touched.add(asset)
                                books.pop(asset, None)
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    counts["invalid_book_events"] += 1
                    for asset in touched:
                        books.pop(asset, None)
                for asset in touched:
                    value = quote_fn(books[asset]) if asset in books else None
                    if asset in reported and value is not None:
                        bids, asks = books[asset]["BUY"], books[asset]["SELL"]
                        if any(bbo is not None and number(bbo) != top for bbo, top in
                               zip(reported[asset], (max(bids), min(asks)))):
                            counts["bbo_mismatch"] += 1
                            desynchronized.add(asset)
                    if asset in desynchronized:
                        value = None
                    points = series[asset, conn]
                    if not points or value != points[-1][1]:
                        points.append((ts, value))
    return series, counts, last_ms


def grid(points, end_ms):
    if not points:
        return {}
    result = {}
    i = 0
    first = (points[0][0] + STEP_MS - 1) // STEP_MS * STEP_MS
    for t in range(first, end_ms, STEP_MS):
        while i + 1 < len(points) and points[i + 1][0] <= t:
            i += 1
        result[t] = points[i][1]
    return result


def summarize(series, last_ms, tokens):
    stats = defaultdict(Counter)
    coverage = defaultdict(Counter)
    for (asset, connection), points in series.items():
        slug = tokens[asset]["slug"]
        observed = grid(points, last_ms[connection])
        prev = None
        for ts, current in observed.items():
            if current is None:
                prev = None
                continue
            mid, l1, l5 = current
            coverage[slug]["valid_seconds"] += 1
            if prev is not None and prev != mid:
                coverage[slug]["mid_changes_1s"] += 1
            prev = mid
            for seconds in HORIZONS:
                path = [observed.get(ts + k * STEP_MS) for k in range(1, seconds + 1)]
                if not all(path):
                    continue
                delta = path[-1][0] - mid
                direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
                for label, value in (("L1", l1), ("L5", l5)):
                    group = ("buy_heavy" if value > THRESHOLD else
                             "sell_heavy" if value < -THRESHOLD else "balanced")
                    for bucket in ("all", group):
                        counter = stats[slug, seconds, label, bucket]
                        counter[direction] += 1
                        counter["sum_cents"] += delta * 100
    rows = []
    for (slug, seconds, levels_label, bucket), count in sorted(stats.items()):
        n = sum(count[k] for k in ("up", "down", "flat"))
        rows.append(dict(market=slug, horizon_s=seconds, levels=levels_label, group=bucket,
                         n=n, up=count["up"], down=count["down"], flat=count["flat"],
                         up_pct=round(count["up"] / n * 100, 3),
                         avg_change_cents=round(count["sum_cents"] / n, 6)))
    return rows, coverage


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    args = p.parse_args()
    info = json.loads((args.folder / "metadata.json").read_text(encoding="utf-8"))
    tokens = {t["id"]: m for m in info["markets"] for t in m["tokens"]
              if t["outcome"].lower() == "yes"}
    series, counts, last_ms = replay(args.folder / "events.jsonl", tokens)
    rows, coverage = summarize(series, last_ms, tokens)
    for m in info["markets"]:
        slug = m["slug"]
        print(f"{m['question']} | 유효 {coverage[slug]['valid_seconds']}초 | "
              f"1초 표본에서 중간가격 변화 {coverage[slug]['mid_changes_1s']}회")
    print("이벤트 수:", dict(counts))
    for row in rows:
        if row["horizon_s"] == 5 and row["levels"] == "L1":
            print(f"{row['market']} / {row['group']}: "
                  f"상승 {row['up']} / 하락 {row['down']} / 보합 {row['flat']}")
    if rows:
        path = args.folder / "comparison.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print("비교표:", path)
    else:
        print("비교에 쓸 수 있는 연속 양방향 호가가 아직 부족합니다.")


if __name__ == "__main__":
    main()
