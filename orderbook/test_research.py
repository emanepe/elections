import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

import collect
import analyze


TOKEN = {"market_slug": "example", "question": "Example?", "condition_id": "condition",
         "token_id": "123", "outcome": "Yes"}


def book(bids=None, asks=None):
    return {"asset_id": "123", "market": "condition", "timestamp": "1793619373317",
            "bids": bids if bids is not None else [{"price": "0.4", "size": "80"}],
            "asks": asks if asks is not None else [{"price": "0.6", "size": "20"}],
            "last_trade_price": "0.9"}


def observed(t, mid, token="123", session="a", valid="1"):
    return {"received_at_unix": str(t), "mid": str(mid), "imbalance_l1": "0.6",
            "valid_for_analysis": valid, "token_id": token, "session_id": session,
            "market_slug": "example", "outcome": "Yes"}


class BookTests(unittest.TestCase):
    def test_unsorted_levels_and_duplicates(self):
        b = book(bids=[{"price": "0.1", "size": "9"}, {"price": "0.4", "size": "30"},
                       {"price": "0.4", "size": "50"}],
                 asks=[{"price": "0.9", "size": "3"}, {"price": "0.6", "size": "20"}])
        r = collect.summarize_book(b, TOKEN, 1)
        self.assertEqual((r["best_bid"], r["best_ask"]), ("0.4", "0.6"))
        self.assertEqual(Decimal(r["imbalance_l1"]), Decimal("0.6"))
        self.assertEqual(Decimal(r["bid_depth"]), Decimal(80))
        self.assertEqual(r["mid"], "0.5")

    def test_empty_side_never_uses_trade_price(self):
        r = collect.summarize_book(book(bids=[]), TOKEN, 5)
        self.assertEqual(r["quality"], "one_sided")
        self.assertEqual(r["mid"], "")
        self.assertEqual(r["imbalance_l1"], "")

    def test_crossed_and_invalid_levels(self):
        b = book(bids=[{"price": "0.8", "size": "80"}, {"price": "NaN", "size": "1"}])
        r = collect.summarize_book(b, TOKEN, 5)
        self.assertEqual(r["valid_for_analysis"], 0)
        self.assertIn("crossed_book", r["quality"])
        self.assertEqual(r["invalid_level_count"], 1)

    def test_bad_identity_fails(self):
        b = book()
        b["asset_id"] = "999"
        with self.assertRaises(collect.DataError):
            collect.summarize_book(b, TOKEN, 5)

    def test_zero_size_is_not_a_waiting_order(self):
        r = collect.summarize_book(book(bids=[{"price": "0.5", "size": "0"}]), TOKEN, 5)
        self.assertEqual(r["best_bid"], "")

    def test_url_and_slug_validation(self):
        self.assertEqual(collect.market_slug("https://polymarket.com/event/event-slug/market-slug?x=1"), "market-slug")
        with self.assertRaises(collect.DataError):
            collect.market_slug("https://other.example/event/foo")

    def test_outcomes_do_not_assume_yes_is_first(self):
        m = {"slug": "example", "active": True, "enableOrderBook": True,
             "outcomes": '["No", "Yes"]', "clobTokenIds": '["123", "456"]'}
        self.assertEqual(collect.tokens_for(m)[0]["outcome"], "No")

    def test_source_timestamp_seconds_and_milliseconds(self):
        self.assertEqual(collect.source_time("1793619373317"), 1793619373.317)
        self.assertEqual(collect.source_time("1793619373"), 1793619373)
        self.assertIsNone(collect.source_time("bad"))

    def test_event_fallback_and_duplicate_urls(self):
        m = {"slug": "child", "active": True, "enableOrderBook": True,
             "outcomes": '["Yes", "No"]', "clobTokenIds": '["123", "456"]'}
        def fake(base, path, params=None):
            if path == "/markets/slug/parent":
                raise HTTPError("https://example", 404, "missing", {}, None)
            return {"markets": [m]}
        with patch.object(collect, "get_json", side_effect=fake):
            self.assertEqual(len(collect.resolve_markets(["parent", "parent"])), 2)


class LabelTests(unittest.TestCase):
    def test_future_is_first_at_or_after_horizon(self):
        r = analyze.label_rows([observed(0, .5), observed(9, .7), observed(11, .6)], 10, 2)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["future_mid"], .6)
        self.assertEqual(r[0]["actual_horizon_seconds"], 11)

    def test_never_join_tokens_sessions_or_wide_gaps(self):
        for future in [observed(10, .7, token="456"), observed(10, .7, session="b"),
                       observed(20, .7), observed(10, .7, valid="0")]:
            self.assertEqual(analyze.label_rows([observed(0, .5), future], 10, 2), [])

    def test_last_rows_not_filled_from_past(self):
        self.assertEqual(analyze.label_rows([observed(0, .5), observed(5, .6)], 10, 2), [])

    def test_flat_changes_stay_flat(self):
        r = analyze.label_rows([observed(0, .5), observed(10, .5)], 10, 2)
        self.assertEqual(r[0]["direction"], "flat")
        report = analyze.summarize(r, .2)
        self.assertEqual(report[0]["flat"], 1)


if __name__ == "__main__":
    unittest.main()
