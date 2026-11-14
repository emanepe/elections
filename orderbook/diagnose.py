#!/usr/bin/env python3
"""Compare order-book imbalance, later midpoint, and hypothetical quoted fills."""
import argparse
import csv
import json
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from study import THRESHOLD, quote_state, replay

HORIZONS = (100, 500, 1000, 5000, 30000)
SPREAD_LIMIT = Decimal("0.01")
ONE_CONTRACT = Decimal(1)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class QuoteSeries:
    def __init__(self, points):
        self.times = []
        self.states = []
        for ts, state in points:
            if self.times and ts < self.times[-1]:
                raise ValueError("Capture clock moved backwards; split or repair the capture first")
            if self.times and ts == self.times[-1]:
                self.states[-1] = state
            else:
                self.times.append(ts)
                self.states.append(state)
        self.invalid = [0]
        for state in self.states:
            self.invalid.append(self.invalid[-1] + int(state is None))

    def window(self, start, end):
        if end < start:
            raise ValueError("Window end precedes start")
        a = bisect_right(self.times, start) - 1
        b = bisect_right(self.times, end) - 1
        if a < 0 or self.invalid[b + 1] != self.invalid[a]:
            return None
        return self.states[a], self.states[b]

    def signal_starts(self, threshold):
        previous = 0
        for ts, state in zip(self.times, self.states):
            current = signal_for(state[1], threshold) if state is not None else 0
            if current and current != previous:
                yield ts
            previous = current

    def path_metrics(self, start, end, signal, threshold):
        now, _ = self.window(start, end)
        a, b = bisect_right(self.times, start), bisect_right(self.times, end)
        changes = [Decimal(0)]
        first_expected = first_opposite = weakened = ""
        for ts, state in zip(self.times[a:b], self.states[a:b]):
            move = signal * (state[0] - now[0]) * 100
            changes.append(move)
            if move > 0 and first_expected == "":
                first_expected = ts - start
            if move < 0 and first_opposite == "":
                first_opposite = ts - start
            if state[1] * signal <= threshold and weakened == "":
                weakened = ts - start
        return dict(first_expected_ms=first_expected, first_opposite_ms=first_opposite,
                    imbalance_weakened_ms=weakened, max_expected_move_cents=max(changes),
                    max_adverse_move_cents=-min(changes))


def signal_for(imbalance, threshold):
    if imbalance > threshold:
        return 1
    if imbalance < -threshold:
        return -1
    return 0


def classify(signal, movement):
    if signal == 0:
        return "neutral"
    return "flat" if movement == 0 else "expected" if signal * movement > 0 else "opposite"


def observe(folder, cutoff_ms, *, sampling="grid", threshold=THRESHOLD,
            horizons=HORIZONS, latency_ms=0, round_trip_fee_cents=Decimal(0)):
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    tokens = {t["id"]: market for market in metadata["markets"] for t in market["tokens"]}
    trades = defaultdict(list)

    def on_trade(asset, conn, ts):
        trades[asset, conn].append(ts)

    series, counts, ends = replay(folder / "events.jsonl", tokens, quote_fn=quote_state,
                                  on_trade=on_trade, verify_quotes=True)
    indexed = {key: QuoteSeries(points) for key, points in series.items()}
    rows = []
    for market in metadata["markets"]:
        outcomes = {t["outcome"].lower(): t["id"] for t in market["tokens"]}
        yes, no = outcomes["yes"], outcomes["no"]
        for conn, end in ends.items():
            ys, ns = indexed.get((yes, conn)), indexed.get((no, conn))
            if ys is None:
                continue
            prints = trades[yes, conn]
            starts = list(ys.signal_starts(threshold)) if sampling == "signal" else None
            for h in horizons:
                start = (ys.times[0] + h - 1) // h * h
                samples = starts if starts is not None else range(start, end - h, h)
                for t in samples:
                    u = t + h
                    if u >= end:
                        continue
                    if cutoff_ms is not None and t < cutoff_ms <= u:
                        continue
                    observed = ys.window(t, u)
                    if observed is None:
                        continue
                    now, later = observed
                    signal = signal_for(now[1], threshold)
                    delta = 100 * (later[0] - now[0])
                    result = classify(signal, delta)
                    selected_series = ys if signal == 1 else ns if signal == -1 else None
                    selected = (selected_series.window(t + latency_ms, u)
                                if selected_series and latency_ms < h else None)
                    gross = net = entry_spread = ""
                    if selected:
                        entry_spread = 100 * (selected[0][4] - selected[0][3])
                    if selected and selected[0][6] >= ONE_CONTRACT and selected[1][5] >= ONE_CONTRACT:
                        gross = 100 * (selected[1][3] - selected[0][4])
                        net = gross - round_trip_fee_cents
                    note = []
                    if result in ("opposite", "flat"):
                        if now[1] * signal > threshold >= later[1] * signal:
                            note.append("imbalance_weakened")
                        if later[3] != now[3]:
                            note.append("best_bid_moved")
                        if later[4] != now[4]:
                            note.append("best_ask_moved")
                        if later[3] == now[3] and later[5] < now[5]:
                            note.append("same_best_bid_size_fell")
                        if later[4] == now[4] and later[6] < now[6]:
                            note.append("same_best_ask_size_fell")
                    trade_count = bisect_right(prints, u) - bisect_right(prints, t)
                    if trade_count:
                        note.append("yes_trade_reported")
                    path = dict(first_expected_ms="", first_opposite_ms="",
                                imbalance_weakened_ms="", max_expected_move_cents="",
                                max_adverse_move_cents="")
                    if signal:
                        path = ys.path_metrics(t, u, signal, threshold)
                    rows.append(dict(
                        session=folder.name, market=market["slug"], connection=conn,
                        observed_utc=datetime.fromtimestamp(t / 1000, timezone.utc).isoformat(timespec="milliseconds"),
                        received_ms=t, horizon_ms=h,
                        sampling=sampling, latency_ms=latency_ms,
                        period="all" if cutoff_ms is None else "early" if u < cutoff_ms else "later",
                        signal="buy_pressure" if signal == 1 else "sell_pressure" if signal == -1 else "neutral",
                        result=result, imbalance=now[1], future_imbalance=later[1],
                        bid=now[3], bid_size=now[5], ask=now[4], ask_size=now[6],
                        future_bid=later[3], future_bid_size=later[5],
                        future_ask=later[4], future_ask_size=later[6],
                        spread_cents=100 * (now[4] - now[3]),
                        midpoint_change_cents=delta, yes_trade_notifications=trade_count,
                        simulated_outcome="Yes" if signal == 1 else "No" if signal == -1 else "",
                        simulated_entry_spread_cents=entry_spread,
                        simulated_gross_cents=gross, simulated_net_cents=net,
                        round_trip_fee_cents=round_trip_fee_cents,
                        **path, observed_changes="|".join(note)))
    return rows, dict(counts)


def summarize(rows):
    buckets = defaultdict(list)
    for row in rows:
        key = tuple(row[k] for k in ("market", "horizon_ms", "period", "signal"))
        buckets[(*key, "all")].append(row)
        if row["spread_cents"] <= SPREAD_LIMIT * 100:
            buckets[(*key, "at_most_1_cent")].append(row)
    summary = []
    for (market, h, period, signal, spread), values in sorted(buckets.items()):
        n = len(values)
        moves = [r["midpoint_change_cents"] for r in values]
        fills = [r["simulated_gross_cents"] for r in values if r["simulated_gross_cents"] != ""]
        net = [r["simulated_net_cents"] for r in values if r["simulated_net_cents"] != ""]
        summary.append(dict(market=market, horizon_ms=h, period=period, signal=signal,
                            spread_group=spread, observations=n, up=sum(d>0 for d in moves),
                            flat=moves.count(0), down=sum(d<0 for d in moves),
                            expected=sum(r["result"]=="expected" for r in values),
                            opposite=sum(r["result"]=="opposite" for r in values),
                            mean_mid_change_cents=round(sum(moves)/n, 6), quote_fill_pairs=len(fills),
                            gross_positive=sum(x>0 for x in fills),
                            mean_gross_cents=round(sum(fills)/len(fills), 6) if fills else "",
                            net_positive=sum(x>0 for x in net),
                            mean_net_cents=round(sum(net)/len(net), 6) if net else ""))
    return summary


def write_csv(path, rows, fieldnames=None):
    fields = fieldnames or (list(rows[0]) if rows else [])
    with path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=Path, nargs="+")
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument("--cutoff-utc", help="Optional ISO-8601 time with timezone to split observations")
    parser.add_argument("--sampling", choices=("grid", "signal"), default="grid",
                        help="grid uses non-overlapping clock windows; signal uses first pressure transitions")
    parser.add_argument("--horizons-ms", nargs="+", type=int, default=list(HORIZONS))
    parser.add_argument("--threshold", type=Decimal, default=THRESHOLD)
    parser.add_argument("--latency-ms", type=int, default=0)
    parser.add_argument("--round-trip-fee-cents", type=Decimal, default=Decimal(0),
                        help="User-specified total fee per contract for both trades; not a platform fee quote")
    args = parser.parse_args()
    if (not args.threshold.is_finite() or not 0 < args.threshold < 1
            or args.latency_ms < 0 or any(h <= args.latency_ms for h in args.horizons_ms)
            or not args.round_trip_fee_cents.is_finite() or args.round_trip_fee_cents < 0):
        parser.error("Require 0<threshold<1, nonnegative latency/fee, and every horizon>latency")
    cutoff = None
    if args.cutoff_utc:
        dt = datetime.fromisoformat(args.cutoff_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            parser.error("cutoff must include a timezone")
        cutoff = int((dt - EPOCH).total_seconds() * 1000)
    rows = []
    audits = {}
    for folder in dict.fromkeys(p.resolve() for p in args.sessions):
        findings, audits[folder.name] = observe(
            folder, cutoff, sampling=args.sampling, threshold=args.threshold,
            horizons=sorted(set(args.horizons_ms)), latency_ms=args.latency_ms,
            round_trip_fee_cents=args.round_trip_fee_cents)
        rows.extend(findings)
    if not rows:
        parser.exit(1, "No usable quote windows found.\n")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "observations.csv", rows)
    write_csv(args.output_dir / "summary.csv", summarize(rows))
    write_csv(args.output_dir / "failed_signals.csv",
              [r for r in rows if r["result"] in ("flat", "opposite")], list(rows[0]))
    settings = dict(sampling=args.sampling, horizons_ms=sorted(set(args.horizons_ms)),
                    threshold=str(args.threshold), latency_ms=args.latency_ms,
                    round_trip_fee_cents=str(args.round_trip_fee_cents), cutoff_utc=args.cutoff_utc,
                    sessions=[str(p.resolve()) for p in args.sessions])
    (args.output_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    (args.output_dir / "replay_audit.json").write_text(
        json.dumps(audits, indent=2) + "\n", encoding="utf-8")
    print(f"{len(rows):,} observations → {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
