"""Snapshot open SIG and Polymarket markets and compare likely identical YES outcomes.

Read only. Prices are reference quotes, not executable arbitrage calculations.
"""

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIG = "https://sig.thesuper.market/api/v1"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def request_json(url, *, key=None, params=None, data=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "MoneyLaundering-market-compare/1.0"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 502, 503, 504) or attempt == 4:
                raise RuntimeError(f"HTTP {exc.code} from {url.split('?')[0]}") from exc
        except (OSError, urllib.error.URLError) as exc:
            if attempt == 4:
                raise RuntimeError(f"Request failed: {url.split('?')[0]}: {exc}") from exc
        time.sleep(min(2 ** attempt, 10))


def load_key():
    env = ROOT / ".env.market_compare"
    values = {}
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values[name] = value.strip()
    return values["SIG_API_KEY"]


def sig_markets(key):
    rows, cursor = [], None
    while True:
        params = {"status": "open", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        page = request_json(SIG + "/markets", key=key, params=params)
        rows.extend(page["data"])
        pagination = page["pagination"]
        if not pagination["hasMore"]:
            return rows
        next_cursor = pagination.get("nextCursor")
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("SIG market pagination stopped without a new cursor")
        cursor = next_cursor


def sig_prices(key, markets):
    ids = sorted({str(ex["id"]) for market in markets for ex in market.get("exchanges", [])})
    prices = {}
    for start in range(0, len(ids), 100):
        page = request_json(SIG + "/exchanges/prices", key=key, params={"ids": ",".join(ids[start:start + 100])})
        prices.update({str(row["exchangeId"]): row for row in page["data"]})
        if page.get("missingIds"):
            print(f"SIG missing {len(page['missingIds'])} exchange prices", file=sys.stderr)
    return prices


def search_query(title):
    title = re.sub(r"^Will\s+(?:the\s+)?(?:Democratic|Republican|Independent)\s+Party\s+win\s+(?:the\s+)?", "", title, flags=re.I)
    title = title.rstrip("?").replace("U.S.", "US")
    return re.sub(r"\s+race$", "", title, flags=re.I).strip()


def poly_markets(sig):
    queries = sorted({search_query(s["title"]) for s in sig})
    rows = {}
    def search(q):
        markets = []
        page = request_json(GAMMA + "/public-search", params={"q": q, "events_status": "active", "limit_per_type": 10, "search_tags": "false", "search_profiles": "false"})
        for event in page.get("events", []):
            for market in event.get("markets", []):
                if market.get("active", True) and not market.get("closed", False):
                    markets.append({**market, "_event_title": event.get("title"), "_search_query": q})
        return markets
    print(f"Searching Polymarket for {len(queries)} SIG topics only...", flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(search, q): q for q in queries}
        for count, future in enumerate(as_completed(futures), 1):
            for market in future.result():
                rows[str(market["id"])] = market
            if count % 10 == 0 or count == len(queries):
                print(f"SIG topics searched: {count}/{len(queries)}; {len(rows)} related Polymarket candidates", flush=True)
    return list(rows.values())


def as_list(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def normalize(title):
    title = title.lower().replace("u.s.", "us").replace("united states", "us")
    title = re.sub(r"\bdemocratic party\b", "democrat", title)
    title = re.sub(r"\brepublican party\b", "republican", title)
    title = re.sub(r"\bdemocrats\b", "democrat", title)
    title = re.sub(r"\brepublicans\b", "republican", title)
    title = re.sub(r"\bindependent (?:party|candidate)\b", "independent", title)
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    return " ".join(x for x in title.split() if x not in {"will", "the", "a", "an", "be"})


def yes_token(market):
    outcomes = [str(x).lower() for x in as_list(market.get("outcomes"))]
    tokens = as_list(market.get("clobTokenIds"))
    for i, outcome in enumerate(outcomes):
        if outcome == "yes" and i < len(tokens):
            return str(tokens[i])
    return None


def race_key(title):
    text = normalize(title)
    party = re.match(r"(democrat|republican|independent) (?:win|control)\b", text)
    if not party or any(word in text.split() for word in ("primary", "and", "combo", "endorse", "before")):
        return None
    years = re.findall(r"\b20\d{2}\b", text)
    if any(year != "2026" for year in years):
        return None
    body = text[party.end():].strip()
    body = re.sub(r"\b(?:race|seat|election|elections|in|after|2026|midterm)\b", "", body)
    body = " ".join(body.split())
    national = re.fullmatch(r"(?:us )?(house|senate)", body)
    if national:
        return party[1], national[1], "us"
    regional = re.fullmatch(r"(.+?) (house|senate|governor)", body)
    if regional:
        return party[1], regional[2], regional[1]
    district = re.fullmatch(r"([a-z]{2} \d+)", body)
    if district:
        return party[1], "house", district[1]
    return None


def candidates(sig, poly):
    by_race, by_title = defaultdict(list), defaultdict(list)
    for market in poly:
        if not yes_token(market):
            continue
        key = race_key(market.get('question') or '')
        if key:
            by_race[key].append(market)
        by_title[normalize(market.get('question') or '')].append(market)
    direct = []
    for market in sig:
        key = race_key(market['title'])
        matching = by_race.get(key, []) if key else by_title.get(normalize(market['title']), [])
        for other in matching:
            direct.append({'sig_id': market['id'], 'sig_title': market['title'], 'poly_id': other['id'],
                           'poly_question': other.get('question'), 'poly_slug': other.get('slug'),
                           'score': 1.0, 'match_status': 'same_race' if key else 'exact_title'})
    return direct

def fuzzy_candidates_for_review(sig, poly):
    index = defaultdict(list)
    for p in poly:
        for word in set(normalize(p.get("question") or "").split()):
            if len(word) > 3:
                index[word].append(p)
    output = []
    for s in sig:
        sn = normalize(s["title"])
        pool = {}
        for word in set(sn.split()):
            if len(word) > 3:
                for p in index.get(word, []):
                    pool[str(p["id"])] = p
        scored = []
        for p in pool.values():
            pn = normalize(p.get("question") or "")
            if not pn or not yes_token(p):
                continue
            sk, pk = race_key(s["title"]), race_key(p.get("question") or "")
            if sk:
                if sk != pk:
                    continue
                score = 1.0
            else:
                score = SequenceMatcher(None, sn, pn).ratio()
            if score >= 0.65:
                scored.append((score, p))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        for score, p in scored[:3]:
            output.append({"sig_id": s["id"], "sig_title": s["title"], "poly_id": p["id"],
                           "poly_question": p.get("question"), "poly_slug": p.get("slug"),
                           "score": round(score, 4), "match_status": "same_race" if race_key(s["title"]) and race_key(s["title"]) == race_key(p.get("question") or "") else ("exact_title" if sn == normalize(p.get("question") or "") else "review")})
    return output


def poly_midpoints(matches, poly_by_id):
    tokens = sorted({yes_token(poly_by_id[str(m["poly_id"])]) for m in matches if yes_token(poly_by_id[str(m["poly_id"])])})
    midpoints = {}
    for start in range(0, len(tokens), 500):
        batch = [{"token_id": token} for token in tokens[start:start + 500]]
        midpoints.update(request_json(CLOB + "/midpoints", data=batch))
    return midpoints


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def size_positions(rows, bankroll, deploy, market_cap, race_cap, min_gap):
    counts = defaultdict(int)
    for row in rows:
        counts[str(row['sig_id'])] += 1
    eligible = []
    for row in rows:
        if counts[str(row['sig_id'])] != 1 or row.get('buy_gap') is None or row['buy_gap'] < min_gap:
            continue
        key = race_key(row['sig_title'])
        group = '|'.join(key[1:]) if key else str(row['sig_id'])
        eligible.append({**row, 'race_group': group, 'target_budget': 0.0})
    remaining = min(deploy, bankroll)
    for _ in range(len(eligible) + 2):
        groups = defaultdict(float)
        for row in eligible:
            groups[row['race_group']] += row['target_budget']
        available = [r for r in eligible if r['target_budget'] < market_cap - 0.001 and groups[r['race_group']] < race_cap - 0.001]
        if remaining < 0.01 or not available:
            break
        total_weight = sum(r['buy_gap'] for r in available)
        proposed = {r['sig_id']: min(remaining * r['buy_gap'] / total_weight, market_cap - r['target_budget']) for r in available}
        group_proposed = defaultdict(float)
        for row in available:
            group_proposed[row['race_group']] += proposed[row['sig_id']]
        used = 0.0
        for row in available:
            group = row['race_group']
            scale = min(1.0, (race_cap - groups[group]) / group_proposed[group])
            amount = proposed[row['sig_id']] * scale
            row['target_budget'] += amount
            used += amount
        remaining -= used
        if used < 0.001:
            break
    for row in eligible:
        row['target_budget'] = math.floor(row['target_budget'] * 100) / 100
        row['target_bankroll_pct'] = round(100 * row['target_budget'] / bankroll, 3)
        row['buy_limit'] = round(row['poly_reference'] - min_gap, 4)
        row['max_shares_at_limit'] = math.floor(row['target_budget'] / row['buy_limit'])
        row['cost_at_current_ask'] = round(row['max_shares_at_limit'] * row['sig_buy_price'], 2)
    return sorted(eligible, key=lambda row: row['buy_gap'], reverse=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "market_compare_output")
    parser.add_argument("--sig-only", action="store_true", help="Export SIG markets and prices without querying Polymarket")
    parser.add_argument("--poly-probe", action="store_true", help="Check the Polymarket connection with a single small request")
    parser.add_argument("--report", action="store_true", help="Summarize the saved comparison and unmatched SIG questions")
    parser.add_argument("--list-gaps", action="store_true", help="Rank the saved comparison by buy-side price gap")
    parser.add_argument("--list-upside", action="store_true", help="Rank positive gaps by percentage upside on purchase cost and by cheapest buy price")
    parser.add_argument("--inspect-trading-api", action="store_true", help="Read the public SIG trading and portfolio API schema")
    parser.add_argument("--account-snapshot", action="store_true", help="Read SIG account balance, positions and open orders")
    parser.add_argument("--inspect-entry", action="store_true", help="Read example SIG books and contract definitions before sizing entries")
    parser.add_argument("--prepare-entry-plan", action="store_true", help="Prepare a fresh, capped SIG SUSQie entry plan")
    parser.add_argument("--execute-entry-plan", action="store_true", help="Execute the prepared SIG SUSQie entry plan once")
    parser.add_argument("--review-entry-plan", action="store_true", help="Inspect the planned contracts and Polymarket resolution rules")
    parser.add_argument("--reconcile-entry-execution", action="store_true", help="Verify entry order sides, fills, holdings and reserved cash")
    parser.add_argument("--return-summary", action="store_true", help="Calculate filled-basket upside to the Polymarket entry references")
    parser.add_argument("--exit-preview", action="store_true", help="Read exit quotes for low-probability positions without submitting orders")
    parser.add_argument("--query", default="Texas Senate", help="Search term for --poly-probe")
    parser.add_argument("--bankroll", type=float, default=100000)
    parser.add_argument("--deploy", type=float, default=80000)
    parser.add_argument("--market-cap", type=float, default=5000)
    parser.add_argument("--race-cap", type=float, default=10000)
    parser.add_argument("--min-gap", type=float, default=0.02)
    args = parser.parse_args()
    if args.return_summary or args.exit_preview:
        from sig_entry_execution import return_summary
        return_summary(exit_preview=args.exit_preview)
        return
    if args.prepare_entry_plan or args.execute_entry_plan or args.review_entry_plan or args.reconcile_entry_execution:
        from sig_entry_execution import prepare, execute, review, reconcile
        if args.prepare_entry_plan:
            prepare()
        elif args.execute_entry_plan:
            execute()
        elif args.reconcile_entry_execution:
            reconcile()
        else:
            review()
        return
    if args.inspect_entry:
        key = load_key()
        with (args.output / 'sig_open_markets.csv').open(encoding='utf-8-sig', newline='') as handle:
            markets = {row['market_id']: row for row in csv.DictReader(handle)}
        for market_id in ('360', '388'):
            market = markets[market_id]
            book = request_json(SIG + '/exchanges/' + market['exchange_id'] + '/orderbook', key=key, params={'depth': 200})
            nodes = request_json(SIG + '/markets/' + market_id + '/nodes', key=key)
            print(json.dumps({'market': market['title'], 'exchange': market['exchange_id'], 'book': book, 'nodes': nodes}, ensure_ascii=True)[:14000])
        return
    if args.account_snapshot:
        key = load_key()
        account = request_json(SIG + '/account', key=key)
        portfolio = request_json(SIG + '/portfolio/positions', key=key)
        orders, cursor = [], None
        while True:
            params = {'status': 'open', 'limit': 200}
            if cursor:
                params['cursor'] = cursor
            page = request_json(SIG + '/orders', key=key, params=params)
            orders.extend(page.get('data', []))
            if not page.get('pagination', {}).get('hasMore'):
                break
            cursor = page['pagination']['nextCursor']
        args.output.mkdir(parents=True, exist_ok=True)
        snapshot = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'account': account, 'portfolio': portfolio, 'open_orders': orders}
        (args.output / 'sig_account_snapshot.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
        print(json.dumps({'account_fields': list(account), 'balance_fields': {k: v for k, v in account.items() if any(word in k.lower() for word in ('balance', 'currency', 'advance', 'tournament', 'available', 'scope'))}, 'portfolio_summary': portfolio.get('summary'), 'position_count': len(portfolio.get('positions', [])), 'open_order_count': len(orders)}, ensure_ascii=True))
        for position in portfolio.get('positions', []):
            print(json.dumps({k: position.get(k) for k in ('exchangeId', 'marketId', 'marketTitle', 'quantity', 'marketValue', 'costBasis')}, ensure_ascii=True))
        return
    if args.inspect_trading_api:
        req = urllib.request.Request(SIG + '/docs', headers={'User-Agent': 'MoneyLaundering-market-compare/1.0'})
        with urllib.request.urlopen(req, timeout=20) as response:
            html = response.read().decode()
        start = html.index('"content": {') + len('"content": ')
        spec, _ = json.JSONDecoder().raw_decode(html[start:])
        for path, methods in spec['paths'].items():
            if path == '/account':
                for method, detail in methods.items():
                    if method in ('get', 'post', 'delete'):
                        print(json.dumps({'method': method, 'path': path, 'summary': detail.get('summary'), 'parameters': detail.get('parameters'), 'body': detail.get('requestBody'), 'responses': detail.get('responses', {}).get('200') or detail.get('responses', {}).get('201')}, ensure_ascii=True))
        for name, schema in spec['components']['schemas'].items():
            if name in ('SingleOrderInput', 'OrderInput', 'OrderOptions', 'ExchangeOrderbook'):
                print(name, json.dumps(schema, ensure_ascii=True))
        return
    if args.list_upside:
        with (args.output / 'matched_price_differences.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames
            rows = list(reader)
        eligible = []
        for row in rows:
            if not row.get('sig_buy_price') or not row.get('buy_gap'):
                continue
            cost, gap = float(row['sig_buy_price']), float(row['buy_gap'])
            if cost <= 0 or gap <= 0:
                continue
            eligible.append({**row, 'gap_percentage_points': round(100 * gap, 4), 'upside_to_reference_pct': round(100 * gap / cost, 4)})
        eligible.sort(key=lambda row: row['upside_to_reference_pct'], reverse=True)
        fields = ['rank'] + columns + ['gap_percentage_points', 'upside_to_reference_pct']
        ranked = [{'rank': rank, **row} for rank, row in enumerate(eligible, 1)]
        write_csv(args.output / 'comparison_sorted_by_relative_upside.csv', ranked, fields)
        cheapest = sorted(eligible, key=lambda row: (float(row['sig_buy_price']), -row['upside_to_reference_pct']))
        write_csv(args.output / 'comparison_sorted_by_buy_price.csv', [{'rank': rank, **row} for rank, row in enumerate(cheapest, 1)], fields)
        print(f'{len(eligible)} positive-gap comparisons ranked; snapshot {eligible[0]["timestamp_utc"] if eligible else "none"}')
        for label, items in [('Largest relative upside', eligible[:10]), ('Lowest buy price', cheapest[:5])]:
            print(label)
            for row in items:
                print(json.dumps({key: row.get(key) for key in ('sig_id', 'sig_title', 'preferred_side', 'sig_buy_price', 'poly_reference', 'gap_percentage_points', 'upside_to_reference_pct')}, ensure_ascii=True))
        return
    if args.list_gaps:
        with (args.output / 'matched_price_differences.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames
            rows = list(reader)
        rows.sort(key=lambda row: float(row['buy_gap']) if row.get('buy_gap') else -math.inf, reverse=True)
        ranked = [{'rank': rank, **row} for rank, row in enumerate(rows, 1)]
        path = args.output / 'comparison_sorted_by_gap.csv'
        write_csv(path, ranked, ['rank'] + columns)
        print(f'Saved {len(ranked)} comparison rows to {path}')
        if ranked:
            print(f"Snapshot: {ranked[0]['timestamp_utc']}")
        for row in ranked[:15]:
            print(json.dumps({key: row.get(key) for key in ('rank', 'sig_id', 'sig_title', 'preferred_side', 'sig_buy_price', 'poly_reference', 'buy_gap')}, ensure_ascii=True))
        return
    if args.report:
        def read(name):
            with (args.output / name).open(encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        sig_rows = read("sig_open_markets.csv")
        matched = read("matched_price_differences.csv")
        related = read("polymarket_open_markets.csv")
        covered = {row['sig_id'] for row in matched}
        missing = [row for row in sig_rows if row['market_id'] not in covered]
        print(json.dumps({'sig_markets': len(sig_rows), 'comparison_rows': len(matched), 'covered_sig_markets': len(covered), 'with_poly_price': sum(bool(row['poly_yes_mid']) for row in matched), 'columns': list(matched[0]) if matched else [], 'statuses': sorted({row['match_status'] for row in matched})}))
        for row in matched[:5]:
            print(json.dumps(row, ensure_ascii=True))
        questions = sorted({row['question'] for row in related})
        for row in missing[:15]:
            nearest = sorted(questions, key=lambda q: SequenceMatcher(None, normalize(row['title']), normalize(q)).ratio(), reverse=True)[:2]
            print(json.dumps({'unmatched': row['title'], 'nearest': nearest}, ensure_ascii=True))
        return
    if args.poly_probe:
        url = GAMMA + "/public-search?" + urllib.parse.urlencode({'q': args.query, 'events_status': 'active', 'limit_per_type': 10})
        req = urllib.request.Request(url, headers={"User-Agent": "MoneyLaundering-market-compare/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                page = json.load(response)
                print(f"Polymarket HTTP {response.status}; {len(page.get('events', []))} related events returned")
                for event in page.get('events', []):
                    print(json.dumps({'event': event.get('title'), 'markets': [m.get('question') for m in event.get('markets', [])]}, ensure_ascii=True))
        except urllib.error.HTTPError as exc:
            print(f"Polymarket HTTP {exc.code}: {exc.read(500).decode('utf-8', errors='replace').strip()}")
            raise SystemExit(1) from None
        except (OSError, urllib.error.URLError) as exc:
            print(f"Polymarket connection failed: {type(exc).__name__}")
            raise SystemExit(1) from None
        return
    key = load_key()
    print("Fetching SIG open markets...", flush=True)
    sig = sig_markets(key)
    print(f"SIG: {len(sig)} open markets; fetching prices...", flush=True)
    sig_quote = sig_prices(key, sig)
    timestamp = datetime.now(timezone.utc).isoformat()
    sig_rows = []
    sig_by_id = {str(s["id"]): s for s in sig}
    for s in sig:
        for ex in s.get("exchanges", []):
            quote = sig_quote.get(str(ex["id"]), {})
            sig_rows.append({"timestamp_utc": timestamp, "market_id": s["id"], "title": s["title"],
                             "exchange_id": ex["id"], "option": ex.get("option"),
                             "last_price": quote.get("latestPrice", ex.get("latestPrice")),
                             "best_bid": quote.get("bestBid"), "best_ask": quote.get("bestAsk")})
    out = args.output
    write_csv(out / "sig_open_markets.csv", sig_rows, ["timestamp_utc", "market_id", "title", "exchange_id", "option", "last_price", "best_bid", "best_ask"])
    print(f"Saved {len(sig_rows)} SIG exchange rows to {out / 'sig_open_markets.csv'}", flush=True)
    if args.sig_only:
        return
    print("Searching Polymarket for SIG markets...", flush=True)
    try:
        poly = poly_markets(sig)
    except RuntimeError as exc:
        if "HTTP 451" in str(exc):
            raise SystemExit("Polymarket returned HTTP 451 from this location. SIG CSV was saved; run the full comparison only where Polymarket permits API access.") from None
        raise
    print(f"Polymarket: {len(poly)} open markets; matching titles...", flush=True)
    matches = candidates(sig, poly)
    poly_by_id = {str(p["id"]): p for p in poly}
    print(f"Matched {len(matches)} contracts; fetching Polymarket YES midpoints...", flush=True)
    midpoints = poly_midpoints(matches, poly_by_id)
    sig_quote = sig_prices(key, sig)
    timestamp = datetime.now(timezone.utc).isoformat()
    for row in sig_rows:
        quote = sig_quote.get(str(row['exchange_id']), {})
        row.update(timestamp_utc=timestamp, last_price=quote.get('latestPrice'), best_bid=quote.get('bestBid'), best_ask=quote.get('bestAsk'))
    write_csv(out / "sig_open_markets.csv", sig_rows, ["timestamp_utc", "market_id", "title", "exchange_id", "option", "last_price", "best_bid", "best_ask"])
    poly_rows = []
    matched_poly_ids = {str(row['poly_id']) for row in matches}
    for p in poly:
        if str(p['id']) not in matched_poly_ids:
            continue
        outcomes = as_list(p.get("outcomes"))
        prices = as_list(p.get("outcomePrices"))
        for i, outcome in enumerate(outcomes):
            poly_rows.append({"timestamp_utc": timestamp, "market_id": p["id"], "question": p.get("question"),
                              "slug": p.get("slug"), "outcome": outcome,
                              "gamma_price": prices[i] if i < len(prices) else None})
    result = []
    for match in matches:
        s = sig_by_id[str(match["sig_id"])]
        p = poly_by_id[str(match["poly_id"])]
        yes = next((ex for ex in s.get("exchanges", []) if str(ex.get("option", "")).upper() == "YES"), None)
        if not yes:
            continue
        quote = sig_quote.get(str(yes["id"]), {})
        sig_price = quote.get("latestPrice")
        bid, ask = quote.get('bestBid'), quote.get('bestAsk')
        sig_mid = (float(bid) + float(ask)) / 2 if bid is not None and ask is not None and float(bid) <= float(ask) else None
        token = yes_token(p)
        poly_price = midpoints.get(token)
        try:
            diff = round(float(sig_mid) - float(poly_price), 5)
        except (TypeError, ValueError):
            diff = None
        result.append({**match, "timestamp_utc": timestamp, "sig_yes_last": sig_price,
                       "sig_yes_mid": sig_mid,
                       "sig_yes_bid": quote.get("bestBid"), "sig_yes_ask": quote.get("bestAsk"),
                       "poly_yes_mid": poly_price, "sig_minus_poly": diff})
        row = result[-1]
        row.update(preferred_side=None, sig_buy_price=None, poly_reference=None, buy_gap=None)
        if poly_price is not None and bid is not None and ask is not None and float(bid) <= float(ask):
            fair = float(poly_price)
            yes_gap, no_gap = fair - float(ask), float(bid) - fair
            side = 'YES' if yes_gap >= no_gap else 'NO'
            row.update(preferred_side=side, sig_buy_price=float(ask) if side == 'YES' else 1 - float(bid), poly_reference=fair if side == 'YES' else 1 - fair, buy_gap=round(max(yes_gap, no_gap), 6))
    result.sort(key=lambda row: abs(row["sig_minus_poly"] or 0), reverse=True)
    write_csv(out / "polymarket_open_markets.csv", poly_rows, ["timestamp_utc", "market_id", "question", "slug", "outcome", "gamma_price"])
    fields = ["timestamp_utc", "match_status", "score", "sig_id", "sig_title", "sig_yes_last", "sig_yes_bid", "sig_yes_ask", "sig_yes_mid", "poly_id", "poly_question", "poly_slug", "poly_yes_mid", "sig_minus_poly", "preferred_side", "sig_buy_price", "poly_reference", "buy_gap"]
    write_csv(out / "matched_price_differences.csv", result, fields)
    covered = {str(row['sig_id']) for row in result}
    unmatched = [row for row in sig_rows if str(row['market_id']) not in covered]
    write_csv(out / "unmatched_sig_markets.csv", unmatched, ["market_id", "title", "exchange_id", "option", "last_price"])
    sizing = size_positions(result, args.bankroll, args.deploy, args.market_cap, args.race_cap, args.min_gap)
    write_csv(out / "position_sizing_preview.csv", sizing, fields + ['race_group', 'target_budget', 'target_bankroll_pct', 'buy_limit', 'max_shares_at_limit', 'cost_at_current_ask'])
    print(f"Covered {len(covered)}/{len(sig)} SIG questions; {len(unmatched)} unmatched. Sizing preview: {len(sizing)} eligible positions; {sum(row['target_budget'] for row in sizing):.2f} allocated.", flush=True)
    print(f"Saved {len(result)} possible matched YES prices ({sum(x['match_status'] == 'exact_title' for x in result)} exact title matches) to {out}")


if __name__ == "__main__":
    main()
