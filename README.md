# Election market research: SIG and Polymarket

The repository includes SIG/Polymarket snapshot comparison scripts and continuous Polymarket election order-book research.

## Polymarket order-book research (Seungwoo)

The [orderbook/](orderbook/) module records public Yes/No market-stream messages, exports quote and trade CSVs, and tests order-book imbalance against later quote-midpoint changes. It includes quote reconstruction checks, signal-transition sampling, failed-signal diagnostics, and configurable entry-delay/cost scenarios.

- [Setup and commands](orderbook/README.md)
- [November 4 findings and limitations](orderbook/FINDINGS.md)
- [Market/signal summary CSVs](orderbook/reports/20261104/)

Run its 21 tests from the module directory:

```sh
cd orderbook
python3 -m unittest -v
node --check capture.mjs
```

The order-book module does not place orders. Raw capture logs and generated observation files are excluded from Git.

## SIG and Polymarket snapshot comparison

The original comparison scripts were last modified locally on October 13, 2026 and are preserved with their supporting module.

## What it extracts

- SIG open markets, last prices, and best bid/ask quotes.
- Related Polymarket markets discovered from SIG titles, including 2026 House, Senate, and governor races.
- Polymarket YES order-book midpoints and Gamma outcome prices.
- Matched price differences, unmatched SIG markets, and a position-sizing preview.

The comparison script is a snapshot tool; use [orderbook/](orderbook/) for continuous Polymarket quote logging. Market discovery is driven by currently open SIG markets; it is not restricted exclusively to elections. The helper module reads Polymarket bid/ask books to calculate midpoint references but does not export those bid/ask levels as a dataset.

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

Both comparison scripts were checked for Python syntax and the comparison CLI's `--help` was run locally. No API keys, account snapshots, trading journals, or generated comparison datasets are included. The order-book module includes aggregate research summaries; its 21 tests and Node.js syntax check pass.
