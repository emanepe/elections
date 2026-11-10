"""One-batch entry into the SIG SUSQie tournament; never uses Polymarket trading keys."""
import csv
import json
import math
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from compare_sig_polymarket import ROOT, SIG, GAMMA, CLOB, load_key, request_json, race_key, yes_token, write_csv

OUT = ROOT / 'market_compare_output'
PLAN = OUT / 'sig_entry_plan.json'
JOURNAL = OUT / 'sig_entry_execution.json'
MIN_RETURN = 0.05
MARKET_CAP = 5000.0
RACE_CAP = 10000.0
RESERVE = 20000.0
DEPLOY_CAP = 80000.0


def read_csv(name):
    with (OUT / name).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temp.replace(path)


def poly_book(token):
    book = request_json(CLOB + '/book', params={'token_id': token})
    bids = [float(row['price']) for row in book.get('bids', []) if float(row.get('size', 0)) > 0]
    asks = [float(row['price']) for row in book.get('asks', []) if float(row.get('size', 0)) > 0]
    if not bids or not asks:
        raise ValueError('Polymarket book has no two-sided quote')
    bid, ask = max(bids), min(asks)
    if ask < bid or ask - bid > 0.025:
        raise ValueError('Polymarket reference spread is crossed or above 2.5 points')
    return (bid + ask) / 2


def prepare():
    key = load_key()
    account = request_json(SIG + '/account', key=key)
    portfolio = request_json(SIG + '/portfolio/positions', key=key)
    orders = request_json(SIG + '/orders', key=key, params={'status': 'open', 'limit': 200})
    if orders.get('data'):
        raise RuntimeError('Existing open orders require reconciliation before a new batch')
    balance = float(account.get('balance') or 0)
    if balance <= RESERVE:
        raise RuntimeError('No spendable cash above the 20,000 reserve')
    comparisons = read_csv('matched_price_differences.csv')
    mapping = {r['market_id']: r for r in read_csv('sig_open_markets.csv')}
    counts = Counter(row['sig_id'] for row in comparisons)
    pools = []
    for row in comparisons:
        cost = float(row.get('sig_buy_price') or 0)
        gap = float(row.get('buy_gap') or 0)
        rk = race_key(row['sig_title'])
        if counts[row['sig_id']] != 1 or not rk or cost <= 0 or gap / cost < MIN_RETURN:
            continue
        pools.append({**row, 'old_return': gap / cost, 'group': '|'.join(rk[1:]), 'exchange_id': mapping[row['sig_id']]['exchange_id']})
    pools.sort(key=lambda row: row['old_return'], reverse=True)
    shortlist, seen = [], set()
    for row in pools:
        if row['group'] not in seen:
            shortlist.append(row)
            seen.add(row['group'])
        if len(shortlist) == 20:
            break
    print(f'Checking {len(shortlist)} distinct races against fresh references and books...', flush=True)

    def inspect(row):
        try:
            market = request_json(GAMMA + '/markets/' + row['poly_id'])
            if market.get('closed') or not market.get('active', True) or not market.get('acceptingOrders', True):
                raise ValueError('Polymarket is not open for orders')
            if race_key(market.get('question') or '') != race_key(row['sig_title']):
                raise ValueError('Polymarket question no longer matches the party and race')
            token = yes_token(market)
            if not token:
                raise ValueError('No Polymarket YES token')
            fair_yes = poly_book(token)
            nodes = request_json(SIG + '/markets/' + row['sig_id'] + '/nodes', key=key)
            root = nodes.get('root') or {}
            details = root.get('contract_details') or {}
            if root.get('node_type') != 'contract' or details.get('raceStage') != 'General' or details.get('resolutionType') != 'Party Winner':
                raise ValueError('SIG contract is not a verified general-election party-winner contract')
            contexts = [c for c in nodes.get('contexts', []) if c.get('type') == 'tournament' and c.get('tournament', {}).get('currencyName') == 'SUSQies' and c.get('tournament', {}).get('slug') == 'midterm-elections']
            if len(contexts) != 1:
                raise ValueError('Cannot establish a single SUSQie Midterm Elections context')
            tournament_id = contexts[0]['tournament']['id']
            book = request_json(SIG + '/exchanges/' + row['exchange_id'] + '/orderbook', key=key, params={'depth': 200, 'tournamentId': tournament_id})
            if not book.get('bids') or not book.get('asks') or float(book['bestBid']) > float(book['bestAsk']):
                raise ValueError('SIG book is empty or crossed')
            yes_price, no_price = float(book['bestAsk']), 1 - float(book['bestBid'])
            yes_return = (fair_yes - yes_price) / yes_price if yes_price > 0 else -1
            no_return = (1 - fair_yes - no_price) / no_price if no_price > 0 else -1
            side = 'yes' if yes_return >= no_return else 'no'
            fair = fair_yes if side == 'yes' else 1 - fair_yes
            roi = max(yes_return, no_return)
            if roi < MIN_RETURN:
                raise ValueError('Fresh relative upside below 5%')
            source = book['asks'] if side == 'yes' else book['bids']
            levels = sorted([(round(float(level['price']) if side == 'yes' else 1 - float(level['price']), 6), math.floor(float(level['quantity']))) for level in source], key=lambda pair: pair[0])
            return {**row, 'side': side, 'poly_yes_token': token, 'reference': fair, 'return_fraction': roi, 'levels': levels, 'tournament_id': tournament_id, 'sig_contract': details, 'poly_description': market.get('description'), 'poly_liquidity': market.get('liquidityNum', market.get('liquidity')), 'poly_yes_reference': fair_yes}, None
        except (ValueError, RuntimeError, KeyError, TypeError) as exc:
            return None, {'market': row['sig_title'], 'reason': str(exc)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        checked = list(pool.map(inspect, shortlist))
    entries = [entry for entry, failure in checked if entry]
    excluded = [failure for entry, failure in checked if failure]
    if not entries:
        raise RuntimeError('No fresh, verified, liquid candidates remain')
    tournament_ids = {row['tournament_id'] for row in entries}
    if len(tournament_ids) != 1:
        raise RuntimeError('Candidates span multiple tournament balances')
    tournament_id = next(iter(tournament_ids))
    scoped = request_json(SIG + '/account', key=key, params={'tournamentId': tournament_id})
    balance = float(scoped.get('balance') or 0)
    market_used, group_used = defaultdict(float), defaultdict(float)
    for position in portfolio.get('positions', []):
        if position.get('settled'):
            continue
        used = max(float(position.get('costBasis') or 0), float(position.get('marketValue') or 0))
        market_used[str(position['marketId'])] += used
        rk = race_key(position.get('marketTitle') or '')
        if rk:
            group_used['|'.join(rk[1:])] += used
    existing_exposure = sum(market_used.values())
    budget = max(0, min(DEPLOY_CAP - existing_exposure, balance - RESERVE))
    weights = {r['sig_id']: r['return_fraction'] for r in entries}
    targets = {r['sig_id']: 0.0 for r in entries}
    remaining = budget
    for _ in range(len(entries) + 2):
        available = [r for r in entries if targets[r['sig_id']] < min(MARKET_CAP - market_used[r['sig_id']], RACE_CAP - group_used[r['group']]) - 0.01]
        if not available or remaining < 0.01:
            break
        total_weight = sum(weights[r['sig_id']] for r in available)
        spent = 0.0
        for row in available:
            cap = max(0, min(MARKET_CAP - market_used[row['sig_id']], RACE_CAP - group_used[row['group']]))
            amount = min(remaining * weights[row['sig_id']] / total_weight, cap - targets[row['sig_id']])
            targets[row['sig_id']] += amount
            spent += amount
        remaining -= spent
    plan_id = str(uuid.uuid4())
    planned = []
    for row in sorted(entries, key=lambda r: r['return_fraction'], reverse=True):
        target = math.floor(targets[row['sig_id']] * 100) / 100
        threshold = math.floor((row['reference'] / (1 + MIN_RETURN) + 1e-10) / 0.005) * 0.005
        quantity, price_limit, modeled_cost = 0, None, 0.0
        for price, size in row['levels']:
            if price <= 0 or price > threshold + 1e-9 or size <= 0:
                continue
            maximum = math.floor(target / price + 1e-9)
            take = min(size, max(0, maximum - quantity))
            if take <= 0:
                break
            quantity += take
            modeled_cost += take * price
            price_limit = price
            if take < size:
                break
        if quantity <= 0 or price_limit is None:
            excluded.append({'market': row['sig_title'], 'reason': 'No affordable liquidity inside the profitable limit'})
            continue
        assert quantity * price_limit <= target + 1e-6
        planned.append({**row, 'target_budget': target, 'quantity': quantity, 'price_limit': round(price_limit, 3), 'maximum_cost': round(quantity * price_limit, 2), 'modeled_cost': round(modeled_cost, 2), 'modeled_upside_pct': round(100 * (quantity * row['reference'] - modeled_cost) / modeled_cost, 3), 'idempotency_key': f'sig-entry-{plan_id}-{row["exchange_id"]}-{row["side"]}'})
    result = {'plan_id': plan_id, 'created_at': datetime.now(timezone.utc).isoformat(), 'tournament_id': tournament_id, 'currency': 'SUSQies', 'balance_before': balance, 'reserve': RESERVE, 'deploy_cap': DEPLOY_CAP, 'market_cap': MARKET_CAP, 'race_cap': RACE_CAP, 'minimum_relative_upside': MIN_RETURN, 'entries': planned, 'excluded': excluded}
    save(PLAN, result)
    columns = ['sig_id', 'sig_title', 'side', 'poly_question', 'reference', 'return_fraction', 'target_budget', 'quantity', 'price_limit', 'maximum_cost', 'modeled_cost', 'modeled_upside_pct']
    write_csv(OUT / 'sig_entry_plan.csv', planned, columns)
    print(json.dumps({'planned_positions': len(planned), 'maximum_spend': round(sum(r['maximum_cost'] for r in planned), 2), 'modeled_spend': round(sum(r['modeled_cost'] for r in planned), 2), 'excluded': excluded}, ensure_ascii=True), flush=True)
    for row in planned:
        print(json.dumps({k: row[k] for k in columns}, ensure_ascii=True), flush=True)


def execute():
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    if plan.get('currency') != 'SUSQies':
        raise RuntimeError('Execution is restricted to the verified SUSQie tournament')
    if datetime.now(timezone.utc) - datetime.fromisoformat(plan['created_at']) > timedelta(minutes=5):
        raise RuntimeError('Plan expired; re-analyse before entering')
    if JOURNAL.exists():
        previous = json.loads(JOURNAL.read_text(encoding='utf-8'))
        if previous.get('orders'):
            raise RuntimeError('An execution journal already contains orders; reconcile it before another batch')
    key = load_key()
    account = request_json(SIG + '/account', key=key, params={'tournamentId': plan['tournament_id']})
    open_orders = request_json(SIG + '/orders', key=key, params={'status': 'open', 'limit': 200, 'tournamentId': plan['tournament_id']})
    if open_orders.get('data'):
        raise RuntimeError('Open orders appeared after planning; stop for reconciliation')
    current_positions = request_json(SIG + '/portfolio/positions', key=key)
    if current_positions.get('positions'):
        raise RuntimeError('Positions appeared after the empty-account plan; re-analyse before execution')
    maximum = sum(row['maximum_cost'] for row in plan['entries'])
    balance = float(account.get('balance') or 0)
    if maximum > min(plan['deploy_cap'], balance - plan['reserve']) + 0.001:
        raise RuntimeError('Insufficient cash while preserving the reserve')
    journal = {'plan_id': plan['plan_id'], 'started_at': datetime.now(timezone.utc).isoformat(), 'orders': [], 'skipped': []}
    save(JOURNAL, journal)
    last_write = 0.0
    def wait_write():
        nonlocal last_write
        elapsed = time.monotonic() - last_write
        if elapsed < 2.2:
            time.sleep(2.2 - elapsed)
        last_write = time.monotonic()
    total_maximum = 0.0
    for row in plan['entries']:
        fair_yes = poly_book(row['poly_yes_token'])
        fair = fair_yes if row['side'] == 'yes' else 1 - fair_yes
        if (fair - row['price_limit']) / row['price_limit'] < MIN_RETURN - 1e-9:
            journal['skipped'].append({'sig_id': row['sig_id'], 'reason': 'Fresh reference no longer supports the limit'})
            save(JOURNAL, journal)
            continue
        if total_maximum + row['maximum_cost'] > min(plan['deploy_cap'], balance - plan['reserve']) + 0.001:
            raise RuntimeError('Batch cash limit reached')
        current_account = request_json(SIG + '/account', key=key, params={'tournamentId': plan['tournament_id']})
        spendable = max(0, float(current_account.get('balance') or 0) - plan['reserve'])
        quantity = min(row['quantity'], math.floor(spendable / row['price_limit'] + 1e-9))
        if quantity <= 0:
            journal['skipped'].append({'sig_id': row['sig_id'], 'reason': 'No cash above the 20,000 reserve'})
            save(JOURNAL, journal)
            continue
        body = {'exchangeId': row['exchange_id'], 'side': row['side'], 'action': 'buy', 'quantity': quantity, 'price': row['price_limit'], 'tournamentId': plan['tournament_id'], 'expirationDate': (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(), 'idempotencyKey': row['idempotency_key']}
        item = {'market': row['sig_title'], 'sig_id': row['sig_id'], 'side': row['side'], 'maximum_cost': row['maximum_cost'], 'request': body, 'state': 'intent_saved'}
        journal['orders'].append(item)
        save(JOURNAL, journal)
        wait_write()
        try:
            response = request_json(SIG + '/orders', key=key, data=body)
        except Exception as exc:
            item['state'] = 'submission_uncertain'
            item['error'] = str(exc)
            save(JOURNAL, journal)
            raise RuntimeError('Order submission failed or is uncertain; stopped to prevent duplicate entries') from exc
        item['response'] = response
        item['state'] = 'placed'
        total_maximum += row['maximum_cost']
        save(JOURNAL, journal)
        print(json.dumps({'market': row['sig_title'], 'side': row['side'], 'order_id': response.get('orderId'), 'quantity_traded': response.get('quantityTraded'), 'total_cost': response.get('totalCost'), 'remaining': response.get('remainingQuantity')}, ensure_ascii=True), flush=True)
        if response.get('open') and response.get('orderId') is not None:
            wait_write()
            req = __import__('urllib.request', fromlist=['Request']).Request(SIG + '/orders/' + str(response['orderId']), headers={'Authorization': 'Bearer ' + key, 'User-Agent': 'MoneyLaundering-market-compare/1.0'}, method='DELETE')
            with __import__('urllib.request', fromlist=['urlopen']).urlopen(req, timeout=20) as result:
                item['cancel_response'] = json.load(result)
            save(JOURNAL, journal)
    after = request_json(SIG + '/account', key=key, params={'tournamentId': plan['tournament_id']})
    portfolio = request_json(SIG + '/portfolio/positions', key=key)
    journal['finished_at'] = datetime.now(timezone.utc).isoformat()
    journal['balance_after'] = after.get('balance')
    journal['portfolio_after'] = portfolio
    journal['filled_cost_from_placement'] = round(sum(float(item.get('response', {}).get('totalCost') or 0) for item in journal['orders']), 2)
    save(JOURNAL, journal)
    print(json.dumps({'orders_submitted': len(journal['orders']), 'skipped': len(journal['skipped']), 'filled_cost': journal['filled_cost_from_placement'], 'balance_after': journal['balance_after'], 'position_count': len(portfolio.get('positions', []))}, ensure_ascii=True), flush=True)


def review():
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    comparisons = read_csv('matched_price_differences.csv')
    counts = Counter(row['sig_id'] for row in comparisons)
    print(json.dumps({'ambiguous_mappings_excluded': sorted({row['sig_title'] for row in comparisons if counts[row['sig_id']] > 1})}, ensure_ascii=True))
    for row in plan['entries']:
        print(json.dumps({'market': row['sig_title'], 'sig_contract': row['sig_contract'], 'poly_question': row['poly_question'], 'poly_resolution_rules': row.get('poly_description'), 'poly_liquidity': row.get('poly_liquidity')}, ensure_ascii=True))


def reconcile():
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    journal = json.loads(JOURNAL.read_text(encoding='utf-8'))
    key = load_key()
    account = request_json(SIG + '/account', key=key, params={'tournamentId': plan['tournament_id']})
    portfolio = request_json(SIG + '/portfolio/positions', key=key)
    open_orders = request_json(SIG + '/orders', key=key, params={'status': 'open', 'limit': 200, 'tournamentId': plan['tournament_id']})
    positions = {str(row['exchangeId']): row for row in portfolio.get('positions', [])}
    intended = {row['exchange_id']: row for row in plan['entries']}
    audited = []
    for item in journal['orders']:
        response = item.get('response', {})
        if response.get('orderId') is None:
            raise RuntimeError('An order lacks a definitive placement response')
        order = request_json(SIG + '/orders/' + str(response['orderId']), key=key)
        request = item['request']
        exchange = str(request['exchangeId'])
        assert order['action'] == 'buy' and order['side'] == request['side']
        assert str(order['exchangeId']) == exchange and order.get('tournamentId') == plan['tournament_id']
        assert not order.get('open'), 'An entry order is still open'
        position = positions.get(exchange)
        filled = float(response.get('quantityTraded') or 0)
        signed = filled if request['side'] == 'yes' else -filled
        if filled:
            assert position is not None and abs(float(position['quantity']) - signed) < 1e-6, 'Position side or quantity does not match fills'
        cost = float(position.get('costBasis') or 0) if position else 0
        planned = intended[exchange]
        assert cost <= planned['target_budget'] + 0.02, 'Contract cost exceeds the planned cap'
        audited.append({'market': item['market'], 'sig_id': item['sig_id'], 'side': request['side'].upper(), 'order_id': response['orderId'], 'requested_shares': request['quantity'], 'filled_shares': filled, 'average_buy_price': cost / filled if filled else None, 'cost': cost, 'poly_reference_at_planning': planned['reference'], 'upside_at_planning_pct': (planned['reference'] / (cost / filled) - 1) * 100 if filled and cost > 0 else None, 'full_requested_fill': filled == request['quantity'], 'side_verified': True, 'order_open': order.get('open')})
    total = sum(row['cost'] for row in audited)
    cash = float(account.get('balance') or 0)
    assert cash >= plan['reserve'], 'Cash reserve was not preserved'
    assert total <= plan['deploy_cap'] + 0.02
    assert not open_orders.get('data'), 'Unexpected open orders remain'
    assert abs(total + cash - plan['balance_before']) < 0.05, 'Cash and cost basis do not reconcile'
    write_csv(OUT / 'executed_positions.csv', audited, list(audited[0]) if audited else ['market', 'side', 'cost'])
    journal['reconciliation'] = {'checked_at': datetime.now(timezone.utc).isoformat(), 'sides_verified': len(audited), 'filled_cost': round(total, 2), 'cash': cash, 'open_orders': len(open_orders.get('data', [])), 'partial_fills': [row for row in audited if not row['full_requested_fill']]}
    save(JOURNAL, journal)
    print(json.dumps(journal['reconciliation'], ensure_ascii=True), flush=True)
    for row in audited:
        print(json.dumps(row, ensure_ascii=True), flush=True)


def return_summary(exit_preview=False):
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    journal = json.loads(JOURNAL.read_text(encoding='utf-8'))
    audited = read_csv('executed_positions.csv')
    reference_value = sum(float(row['filled_shares']) * float(row['poly_reference_at_planning']) for row in audited)
    cost = float(journal['filled_cost_from_placement'])
    profit = reference_value - cost
    low = [row for row in audited if float(row['poly_reference_at_planning']) < 0.5]
    summary = {'benchmark_value': round(reference_value, 2), 'actual_cost': cost, 'benchmark_profit': round(profit, 2), 'benchmark_return_on_spend_pct': round(100 * profit / cost, 3), 'benchmark_return_on_initial_bankroll_pct': round(100 * profit / plan['balance_before'], 3), 'low_probability_cost': round(sum(float(row['cost']) for row in low), 2), 'low_probability_positions': [{key: row[key] for key in ('market', 'side', 'filled_shares', 'cost', 'poly_reference_at_planning', 'upside_at_planning_pct')} for row in low]}
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    if not exit_preview:
        return
    key = load_key()
    portfolio = request_json(SIG + '/portfolio/positions', key=key)
    positions = {str(row['marketId']): row for row in portfolio.get('positions', [])}
    planned = {row['sig_id']: row for row in plan['entries']}
    for row in low:
        market_id = row['sig_id']
        position = positions[market_id]
        intent = planned[market_id]
        book = request_json(SIG + '/exchanges/' + intent['exchange_id'] + '/orderbook', key=key, params={'depth': 200, 'tournamentId': plan['tournament_id']})
        quantity = abs(float(position['quantity']))
        side = 'yes' if float(position['quantity']) > 0 else 'no'
        source = book['bids'] if side == 'yes' else book['asks']
        levels = sorted([(float(level['price']) if side == 'yes' else 1 - float(level['price']), float(level['quantity'])) for level in source], key=lambda pair: pair[0], reverse=True)
        remaining, proceeds, worst = quantity, 0.0, None
        for price, available in levels:
            take = min(remaining, available)
            if take > 0:
                proceeds += take * price
                remaining -= take
                worst = price
            if remaining <= 1e-8:
                break
        cost_basis = float(position.get('costBasis') or 0)
        print(json.dumps({'exit_preview_only': True, 'market': row['market'], 'side': side.upper(), 'quantity': quantity, 'best_sell_price': levels[0][0] if levels else None, 'modeled_exit_proceeds': round(proceeds, 2), 'modeled_average_sell_price': proceeds / quantity if remaining <= 1e-8 else None, 'unfilled_at_available_depth': remaining, 'worst_sell_price': worst, 'cost_basis': cost_basis, 'modeled_profit_loss': round(proceeds - cost_basis, 2) if remaining <= 1e-8 else None, 'book_timestamp': book.get('asOf')}, ensure_ascii=True), flush=True)
