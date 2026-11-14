"""Regression checks for time alignment, signal events, and quote reconstruction."""
import csv
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from diagnose import QuoteSeries, observe, write_csv
from study import quote_state, replay


def state(mid="0.5", pressure="0.4"):
    m = D(mid)
    return m, D(pressure), D(pressure), m-D("0.01"), m+D("0.01"), D(10), D(10)


def book(asset, bid="0.49", ask="0.51", bid_size="10", ask_size="10"):
    return dict(event_type="book", asset_id=asset, market="condition",
                bids=[dict(price=bid, size=bid_size)],
                asks=[dict(price=ask, size=ask_size)])


def delta(asset, size, best_bid="0.49", best_ask="0.51"):
    return dict(event_type="price_change", market="condition", price_changes=[
        dict(asset_id=asset, price="0.49", size=size, side="BUY",
             best_bid=best_bid, best_ask=best_ask)])


class DiagnosticTests(unittest.TestCase):
    def test_invalid_window_cannot_bridge_recovery(self):
        qs = QuoteSeries([(0, state()), (40, None), (80, state("0.6"))])
        self.assertIsNone(qs.window(0, 100))
        self.assertEqual(qs.window(80, 100)[0][0], D("0.6"))

    def test_signal_counts_transitions_not_every_update(self):
        qs = QuoteSeries([(0, state(pressure="0")), (10, state()),
                          (20, state(pressure="0.8")), (30, state(pressure="0.2")),
                          (40, state()), (50, state(pressure="-0.4")),
                          (60, None), (70, state(pressure="-0.4"))])
        self.assertEqual(list(qs.signal_starts(D("0.2"))), [10, 40, 50, 70])

    def test_excursion_captures_reversal_before_flat_endpoint(self):
        qs = QuoteSeries([(0, state()), (20, state("0.52")),
                          (40, state("0.49", "0.1")), (60, state())])
        result = qs.path_metrics(0, 100, 1, D("0.2"))
        self.assertEqual(result["first_expected_ms"], 20)
        self.assertEqual(result["first_opposite_ms"], 40)
        self.assertEqual(result["imbalance_weakened_ms"], 40)
        self.assertEqual(result["max_expected_move_cents"], D(2))
        self.assertEqual(result["max_adverse_move_cents"], D(1))

    def test_millisecond_ties_use_last_received_state(self):
        qs = QuoteSeries([(0, state()), (0, state("0.6")), (1, state("0.7"))])
        self.assertEqual(qs.window(0, 0)[0][0], D("0.6"))
        with self.assertRaises(ValueError):
            QuoteSeries([(2, state()), (1, state())])

    def create_session(self, folder, events, end=200):
        market = dict(slug="synthetic", condition_id="condition", tokens=[
            dict(id="yes", outcome="Yes"), dict(id="no", outcome="No")])
        (folder / "metadata.json").write_text(json.dumps(dict(markets=[market])))
        rows = [dict(kind="connect", received_ms=0, connection=1)]
        rows.extend(dict(kind="message", received_ms=t, connection=1, raw=json.dumps(e))
                    for t, e in events)
        rows.extend([dict(kind="disconnect", received_ms=end, connection=1),
                     dict(kind="end", received_ms=end, connection=1)])
        (folder / "events.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
        return {"yes": market, "no": market}

    def test_mismatch_stays_invalid_until_full_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tokens = self.create_session(folder, [
                (0, book("yes")), (10, delta("yes", "20", best_bid="0.48")),
                (20, delta("yes", "30")), (30, book("yes", bid_size="30"))])
            series, counts, _ = replay(folder / "events.jsonl", tokens,
                                       quote_fn=quote_state, verify_quotes=True)
            qs = QuoteSeries(series["yes", 1])
            self.assertIsNone(qs.window(20, 20))
            self.assertIsNone(qs.window(0, 30))
            self.assertIsNotNone(qs.window(30, 35))
            self.assertEqual(counts["bbo_mismatch"], 1)
            self.assertEqual(counts["snapshot_recovered"], 1)

    def test_delayed_entry_fee_and_no_lookahead(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.create_session(folder, [(0, [book("yes", bid_size="30"), book("no")]),
                (40, book("yes", bid="0.59", ask="0.61", bid_size="30")),
                (100, book("yes", bid="0.69", ask="0.71", bid_size="30"))])
            immediate, _ = observe(folder, None, sampling="signal", horizons=(100,))
            delayed, _ = observe(folder, None, sampling="signal", horizons=(100,),
                                  latency_ms=50, round_trip_fee_cents=D("0.4"))
            self.assertEqual(len(immediate), 1)
            self.assertEqual(immediate[0]["simulated_gross_cents"], D(18))
            self.assertEqual(delayed[0]["simulated_gross_cents"], D(8))
            self.assertEqual(delayed[0]["simulated_net_cents"], D("7.6"))
            # At 50 ms entry uses the 40 ms quote, not the 100 ms exit quote.

    def test_sell_pressure_buys_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.create_session(folder, [(0, [book("yes", ask_size="30"), book("no")]),
                (100, book("no", bid="0.59", ask="0.61"))])
            rows, _ = observe(folder, None, sampling="signal", horizons=(100,))
            self.assertEqual(rows[0]["simulated_outcome"], "No")
            self.assertEqual(rows[0]["simulated_gross_cents"], D(8))

    def test_empty_failure_output_replaces_old_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "failed.csv"
            path.write_text("obsolete rows")
            write_csv(path, [], ["result", "signal"])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, ["result", "signal"])
                self.assertEqual(list(reader), [])

    def test_csv_export_also_quarantines_until_snapshot_recovery(self):
        from collections import Counter
        from io import StringIO
        from export_orderbook_csv import export_session, FIELDS
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.create_session(folder, [(0, [book("yes"), book("no")]),
                (10, delta("yes", "20", best_bid="0.48")),
                (20, delta("yes", "30")), (30, book("yes", bid_size="30"))])
            quotes, trades = StringIO(), StringIO()
            counts = Counter()
            export_session(folder, csv.writer(trades), csv.writer(quotes), counts)
            rows = [dict(zip(FIELDS, row)) for row in csv.reader(StringIO(quotes.getvalue()))]
            self.assertEqual([r["timestamp_ms"] for r in rows if r["up_mid"]], ["0", "30"])
            self.assertEqual(counts["bbo_mismatch"], 1)
            self.assertEqual(counts["snapshot_recovered"], 1)


if __name__ == "__main__":
    unittest.main()
