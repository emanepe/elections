# Election market comparison: SIG and Polymarket

Local scripts originally last modified on October 13, 2026, preserved here with their supporting module.

## What it extracts

- SIG open markets, last prices, and best bid/ask quotes.
- Related Polymarket markets discovered from SIG titles, including 2026 House, Senate, and governor races.
- Polymarket YES order-book midpoints and Gamma outcome prices.
- Matched price differences, unmatched SIG markets, and a position-sizing preview.

This is a snapshot comparison tool, not a continuous Polymarket best-bid/ask logger. Market discovery is driven by currently open SIG markets; it is not restricted exclusively to elections. The helper module reads Polymarket bid/ask books to calculate midpoint references but does not export those bid/ask levels as a dataset.

## Setup

Python 3.10+; only the standard library is used.

Copy `.env.market_compare.example` to `.env.market_compare` in this repository root and replace the placeholder with your SIG API key. Use the plain value without quotation marks. The real configuration and generated data are gitignored. Polymarket market-data requests do not use trading credentials.

Run from the repository root:

```sh
python scripts/compare_sig_polymarket.py --help
python scripts/compare_sig_polymarket.py --poly-probe --query "Texas Senate"
python scripts/compare_sig_polymarket.py
```

Export SIG only:

```sh
python scripts/compare_sig_polymarket.py --sig-only
```

Results go to `market_compare_output/` by default; use `--output PATH` to change the comparison output directory. The optional entry helper uses the default directory regardless of `--output`.

## Outputs

- `sig_open_markets.csv`: SIG prices and best bid/ask.
- `polymarket_open_markets.csv`: matched Polymarket outcome prices from Gamma.
- `matched_price_differences.csv`: SIG quotes, Polymarket YES midpoint, and comparison fields.
- `unmatched_sig_markets.csv`: SIG exchanges without a matched comparison.
- `position_sizing_preview.csv`: calculated allocation preview; the default snapshot does not submit orders.

Matching can use normalized election race names or exact titles. Check contract resolution rules before treating matched markets as equivalent. Midpoints and price differences are reference values, not executable prices.

## Original optional SIG features

`scripts/sig_entry_execution.py` is included because the original CLI imports it for entry planning, review, execution, reconciliation, and return summaries. The `--execute-entry-plan` option can submit and cancel SIG tournament orders using the configured account. It is not needed for data extraction. Neither script has been run against an account as part of this upload.

The original code is preserved. API availability and compatibility have not been checked live. If Polymarket returns HTTP 451, use the tool only from a location where access is permitted.

## Validation

Both scripts were checked for Python syntax and the comparison CLI's `--help` was run locally. No API keys, account snapshots, trading journals, or generated datasets are included.
