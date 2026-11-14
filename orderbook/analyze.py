#!/usr/bin/env python3
"""Label collected snapshots using later observations; no trading or forecasting."""

import argparse
import bisect
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def label_rows(rows, horizon, tolerance):
    groups = defaultdict(list)
    for row in rows:
        ts, mid, obi = (number(row.get(k)) for k in
                        ("received_at_unix", "mid", "imbalance_l1"))
        if row.get("valid_for_analysis") not in ("1", 1) or None in (ts, mid, obi):
            continue
        if not 0 < mid < 1 or not -1 <= obi <= 1:
            continue
        groups[(row.get("session_id"), row.get("token_id"))].append((ts, mid, obi, row))
    result = []
    for entries in groups.values():
        entries.sort(key=lambda x: x[0])
        times = [x[0] for x in entries]
        for ts, mid, obi, row in entries:
            # Use a real elapsed time, not "10 rows later". Never use a quote before t+h.
            idx = bisect.bisect_left(times, ts + horizon)
            if idx == len(entries) or times[idx] - (ts + horizon) > tolerance:
                continue
            future_ts, future_mid, _, _ = entries[idx]
            delta = future_mid - mid
            result.append({**row, "horizon_seconds": horizon,
                           "future_received_at_utc": datetime.fromtimestamp(
                               future_ts, timezone.utc).isoformat(timespec="milliseconds"),
                           "actual_horizon_seconds": round(future_ts - ts, 6),
                           "future_mid": future_mid, "mid_change": round(delta, 12),
                           "direction": "up" if delta > 1e-12 else "down" if delta < -1e-12 else "flat"})
    return result


def summarize(rows, threshold):
    buckets = defaultdict(list)
    for row in rows:
        # YES and NO remain separate; they are not independent extra experiments.
        base = (row["session_id"], row["token_id"], row["market_slug"], row["outcome"])
        obi = float(row["imbalance_l1"])
        buckets[(*base, "all")].append(row)
        bucket = "buy_heavy" if obi > threshold else "sell_heavy" if obi < -threshold else "balanced"
        buckets[(*base, bucket)].append(row)
    output = []
    for (session, token, slug, outcome, bucket), items in sorted(buckets.items()):
        n = len(items)
        up = sum(r["direction"] == "up" for r in items)
        down = sum(r["direction"] == "down" for r in items)
        flat = n - up - down
        output.append({"session_id": session, "token_id": token, "market_slug": slug,
                       "outcome": outcome, "bucket": bucket, "n": n,
                       "up": up, "down": down, "flat": flat,
                       "up_fraction": up / n,
                       "mean_mid_change": sum(r["mid_change"] for r in items) / n})
    return output


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 수를 넣어 주세요.")
    return result


def main():
    p = argparse.ArgumentParser(description="수집된 CSV에서 일정 시간 뒤 가격 변화 확인")
    p.add_argument("csv_file", type=Path)
    p.add_argument("--horizon", type=positive, default=10, help="몇 초 뒤를 볼지 (기본 10초)")
    p.add_argument("--tolerance", type=positive, default=3, help="목표 시간 이후 허용 지연 (기본 3초)")
    p.add_argument("--threshold", type=float, default=0.2, help="호가 불균형 분류 기준 (기본 0.2)")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if not math.isfinite(args.threshold) or not 0 <= args.threshold < 1:
        p.error("threshold는 0 이상 1 미만이어야 합니다.")
    with args.csv_file.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        needed = {"received_at_unix", "mid", "imbalance_l1", "valid_for_analysis",
                  "session_id", "token_id", "market_slug", "outcome"}
        if not needed.issubset(reader.fieldnames or []):
            p.error("collect.py가 만든 snapshots.csv를 넣어 주세요.")
        rows = list(reader)
    labeled = label_rows(rows, args.horizon, args.tolerance)
    report = {"horizon_seconds": args.horizon, "tolerance_seconds": args.tolerance,
              "imbalance_threshold": args.threshold, "input_rows": len(rows),
              "labeled_rows": len(labeled), "groups": summarize(labeled, args.threshold),
              "caution": "Descriptive only. Overlapping observations are dependent; no profitability or causal claim."}
    out = args.output or args.csv_file.parent / ("analysis_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    out.mkdir(parents=True, exist_ok=False)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if labeled:
        with (out / "labeled.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(labeled[0]))
            w.writeheader()
            w.writerows(labeled)
    print(f"입력 {len(rows)}행 → 비교 가능한 {len(labeled)}행. 결과: {out.resolve()}")
    for g in report["groups"]:
        print(f"{g['market_slug']} / {g['outcome']} / {g['bucket']}: "
              f"{g['n']}회, 상승 {g['up']} / 하락 {g['down']} / 그대로 {g['flat']}")
    if not labeled:
        print("비교할 미래 기록이 부족합니다. 더 오래 수집하거나 horizon/tolerance를 확인해 주세요.")
    print("단순 관찰 결과입니다. 상승 비율만으로 예측력이나 수익성을 결론내리지 마세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
