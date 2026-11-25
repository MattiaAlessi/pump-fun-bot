"""Pump.fun replay bot: early-buy sniper with a quick take-profit and a hard stop.

The strategy watches every coin from its CreateEvent. It buys young curves that hold
real SOL and show buying from a small set of wallets (a launch bundled by many wallets
is usually the creator's cluster waiting to dump), then takes profit fast, cuts losers,
and leaves at once when the creator sells. It never holds a coin past a short slot limit,
so the five position slots keep rotating onto new launches and the run collects round
trips on many coins.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL

# Entry
MAX_AGE = 1
MIN_REAL_SOL = LAMPORTS
MAX_BUYERS = 1
MIN_DEV_BUY = LAMPORTS // 10
MIN_RUNUP_BPS = 2000
STAKE = LAMPORTS // 5
MIN_BUY = LAMPORTS // 200
BUY_SLIPPAGE_BPS = 2_500

# Exit
TAKE_PROFIT_BPS = 8000
STOP_LOSS_BPS = 1200
MAX_HOLD_SLOTS = 10
STALE_SLOTS = 4

coins = {}


def _coin(mint, slot):
    c = coins.get(mint)
    if c is None:
        c = {
            "created_slot": slot,
            "vq0": 0,
            "vt0": 0,
            "creator": None,
            "dev_buy": 0,
            "buyers": {},
            "dev_sold": False,
            "last_trade_slot": slot,
            "entries": 0,
            "done": False,
        }
        coins[mint] = c
    return c


def _held_sell(state, mint):
    for p in state.pending.values():
        if p.order["side"] == "sell" and p.order["mint"] == mint:
            return True
    return False


def _buy_pending(state, mint):
    for p in state.pending.values():
        if p.order["side"] == "buy" and p.order["mint"] == mint:
            return True
    return False


def on_batch(state, batch):
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()

    # Fold this batch's events into our own per-coin tape.
    for e in batch["events"]:
        mint = e["mint"]
        kind = e["event"]
        slot = e["slot"]
        c = _coin(mint, slot)
        if kind == "CreateEvent":
            c["created_slot"] = slot
            c["creator"] = e.get("creator")
            c["vq0"] = int(e["virtual_sol_reserves"])
            c["vt0"] = int(e["virtual_token_reserves"])
            c["dev_buy"] = 0
            c["last_trade_slot"] = slot
        elif kind == "TradeEvent":
            sol = int(e["sol_amount"])
            user = e.get("user")
            c["last_trade_slot"] = slot
            if e["is_buy"]:
                if user != c["creator"]:
                    c["buyers"][user] = c["buyers"].get(user, 0) + sol
                elif slot == c["created_slot"]:
                    c["dev_buy"] += sol
            else:
                if user == c["creator"]:
                    c["dev_sold"] = True
        else:
            c["done"] = True

    orders = []
    slots_free = caps["max_open_positions"] - state.committed_position_count()
    free = state.free_cash_lamports()

    # Exits first, so the slots freed refill in the same batch when possible.
    for mint, holding in list(state.positions.items()):
        curve = state.curves.get(mint)
        c = coins.get(mint)
        if curve is None or c is None:
            continue
        if curve.complete:
            c["done"] = True
            continue
        if _held_sell(state, mint):
            continue
        value = holding.token_amount * curve.virtual_quote_reserves // curve.virtual_token_reserves
        cost = holding.cost_lamports
        age = state.slot - c["created_slot"]
        exit_now = (
            value * 10_000 >= cost * (10_000 + TAKE_PROFIT_BPS)
            or value * 10_000 <= cost * (10_000 - STOP_LOSS_BPS)
            or c["dev_sold"]
            or age > MAX_HOLD_SLOTS
            or state.slot - c["last_trade_slot"] > STALE_SLOTS
        )
        if exit_now:
            orders.append(
                {
                    "side": "sell",
                    "mint": mint,
                    "token_amount": holding.token_amount,
                    "max_slippage_bps": 10_000,
                }
            )

    # Entries: youngest qualifying launches, one buy per coin per visit.
    for e in batch["events"]:
        if e["event"] != "CreateEvent":
            continue
        mint = e["mint"]
        c = coins[mint]
        if c["done"] or c["entries"] >= 2:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            c["done"] = True
            continue
        age = state.slot - c["created_slot"]
        if age > MAX_AGE:
            continue
        if curve.real_quote_reserves < MIN_REAL_SOL:
            continue
        if c["dev_sold"]:
            continue
        if len(c["buyers"]) > MAX_BUYERS:
            continue
        if c["dev_buy"] < MIN_DEV_BUY:
            continue
        # Only buy launches that already trade above their create price, by a margin.
        if (curve.virtual_quote_reserves * c["vt0"] * 10_000
                < c["vq0"] * curve.virtual_token_reserves * (10_000 + MIN_RUNUP_BPS)):
            continue
        if _buy_pending(state, mint):
            continue
        cap = state.max_buy_lamports(mint)
        amount = min(cap, STAKE)
        if amount < MIN_BUY:
            continue
        if len(orders) >= caps["max_orders_per_batch"] or slots_free <= 0 or free < amount + tx:
            continue
        orders.append(
            {
                "side": "buy",
                "mint": mint,
                "quote_lamports": amount,
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            }
        )
        free -= amount + tx
        slots_free -= 1
        c["entries"] += 1

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
