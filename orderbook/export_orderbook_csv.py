#!/usr/bin/env python3
"""Export Polymarket capture sessions into matching trade and quote CSVs.

The team template stores trade price and size in both quote-shaped columns.
The separate quotes CSV stores genuine resting bid and ask prices and sizes.
Election outcomes map Yes to up and No to down. Market timestamp is blank
because the template's BTC interval-start field has no source equivalent.
All output times are capture receipt times in UTC, with millisecond precision.
"""
import argparse
import csv
import json
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SIDES = ("up", "down")
FIELDS = ["timestamp", "timestamp_ms", "datetime", "market_timestamp",
          "market_slug", "message_type"]
FIELDS += [field for side in SIDES for field in (
    f"{side}_mid", f"{side}_bid", f"{side}_bid_size", f"{side}_ask",
    f"{side}_ask_size", f"spread_{side}")]
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def read_tokens(session):
    metadata = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
    tokens = {}
    for market in metadata["markets"]:
        for token in market["tokens"]:
            side = {"yes": "up", "no": "down"}[token["outcome"].lower()]
            token_id = token["id"]
            if token_id in tokens:
                raise ValueError(f"Duplicate token in {session}")
            tokens[token_id] = (market["slug"], side, market["condition_id"])
    return tokens


def row_for(ts_ms, market, kind, prices):
    seconds, milliseconds = divmod(ts_ms, 1000)
    return [f"{seconds}.{milliseconds:03d}", ts_ms,
            (EPOCH + timedelta(milliseconds=ts_ms)).isoformat(timespec="milliseconds"),
            "", market, kind, *prices.get("up", [""] * 6),
            *prices.get("down", [""] * 6)]


def get_levels(entries):
    levels = {}
    for entry in entries:
        price, size = Decimal(entry["price"]), Decimal(entry["size"])
        if size > 0:
            levels[price] = size
    return levels


def quote(book, reported):
    bids, asks = book
    if not bids or not asks:
        return None
    bid, ask = max(bids), min(asks)
    if bid >= ask:
        return None
    if any(value is not None and Decimal(value) != actual
           for value, actual in zip(reported, (bid, ask))):
        return None
    return ((bid + ask) / 2, bid, bids[bid], ask, asks[ask], ask - bid)


def export_session(session, trades, quotes, counts):
    tokens = read_tokens(session)
    books = {}
    connection = None
    with (session / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                counts["incomplete_tail"] += 1
                break
            record = json.loads(line)
            if record["connection"] != connection or record["kind"] != "message":
                books.clear()
                connection = record["connection"]
            if record["kind"] != "message":
                continue
            try:
                payload = json.loads(record["raw"])
            except (ValueError, TypeError):
                counts["unparsed_messages"] += 1
                continue
            ts_ms = record["received_ms"]
            for event in payload if isinstance(payload, list) else [payload]:
                if not isinstance(event, dict):
                    continue
                kind = event.get("event_type")
                asset = event.get("asset_id")
                condition = event.get("market")

                def matches(candidate):
                    return candidate in tokens and tokens[candidate][2] == condition

                if kind == "last_trade_price" and matches(asset):
                    slug, side, _ = tokens[asset]
                    price, size = Decimal(event["price"]), Decimal(event["size"])
                    trades.writerow(row_for(ts_ms, slug, kind, {
                        side: (price, price, size, price, size, "0.0"),
                    }))
                    counts["trade_rows"] += 1
                    continue

                updated = {}
                if kind == "book" and matches(asset):
                    books[asset] = (get_levels(event["bids"]), get_levels(event["asks"]))
                    updated[asset] = (None, None)
                elif kind == "price_change":
                    for change in event["price_changes"]:
                        candidate = change["asset_id"]
                        if not matches(candidate):
                            continue
                        if candidate not in books:
                            counts["delta_before_snapshot"] += 1
                            continue
                        levels = books[candidate][{"BUY": 0, "SELL": 1}[change["side"]]]
                        price, size = Decimal(change["price"]), Decimal(change["size"])
                        if size == 0:
                            levels.pop(price, None)
                        else:
                            levels[price] = size
                        updated[candidate] = (change.get("best_bid"), change.get("best_ask"))
                elif kind == "market_resolved":
                    for candidate in event.get("assets_ids", []):
                        books.pop(candidate, None)

                values = {}
                for candidate, reported in updated.items():
                    current = quote(books[candidate], reported)
                    if current is None:
                        counts["unusable_quote_sides"] += 1
                        continue
                    slug, side, _ = tokens[candidate]
                    values.setdefault(slug, {})[side] = current
                for slug, prices in values.items():
                    quotes.writerow(row_for(ts_ms, slug, kind, prices))
                    counts["quote_rows"] += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=Path, nargs="+",
                        help="Capture folders, each with metadata.json and events.jsonl")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sessions = sorted({p.resolve() for p in args.sessions},
                      key=lambda p: json.loads((p / "metadata.json").read_text())["started_at"])
    dates = [json.loads((p / "metadata.json").read_text())["started_at"][:10].replace("-", "")
             for p in sessions]
    suffix = dates[0] if dates[0] == dates[-1] else f"{dates[0]}_to_{dates[-1]}"
    filenames = (f"polymarket_election_trades_{suffix}.csv",
                 f"polymarket_election_quotes_{suffix}.csv")
    counts = Counter()
    with tempfile.TemporaryDirectory(dir=args.output_dir) as temp:
        trade_path, quote_path = (Path(temp) / name for name in filenames)
        with trade_path.open("w", encoding="utf-8", newline="") as trade_file, \
             quote_path.open("w", encoding="utf-8", newline="") as quote_file:
            trades, quotes = csv.writer(trade_file), csv.writer(quote_file)
            trades.writerow(FIELDS)
            quotes.writerow(FIELDS)
            for session in sessions:
                export_session(session, trades, quotes, counts)
        for path in (trade_path, quote_path):
            path.replace(args.output_dir / path.name)
    for key, value in sorted(counts.items()):
        print(f"{key}: {value:,}")


if __name__ == "__main__":
    main()
