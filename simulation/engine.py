"""
Paper-trading simulation engine.

Intercepts order placement and tracks hypothetical positions, fills,
and PnL — all against live market data without sending real orders.

Key behaviours:
  - place_order() records quote intent, returns a simulated order ID
  - check_fills() compares open quotes against live bids each cycle
  - Inventory & PnL are tracked entirely on paper
  - Fill model: our bid >= live best bid → fill (conservative lower-bound)
"""
import time
import csv
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import YES, NO
from measure.logs import get_logger


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class SimulatedFill:
    """A single hypothetical fill event."""
    timestamp: float
    market_condition_id: str
    market_question: str
    token: str          # "YES" or "NO"
    side: str           # "BUY" or "SELL"
    price: float
    size: int
    hours_to_resolution: float = 0.0
    spread: float = 0.0


@dataclass
class MarketSnap:
    """Snapshot of a single market for TUI rendering."""
    question: str
    condition_id: str
    hours_to_resolution: float
    yes_bid: float
    no_bid: float
    yes_quote: float
    no_quote: float
    yes_inventory: float
    no_inventory: float
    net_exposure: float
    market_mid: float
    own_mid: float
    spread: float
    fills_this_cycle: int


# ── Engine ───────────────────────────────────────────────────────────────────

class SimulationEngine:
    """
    Tracks hypothetical positions and fills without sending orders.

    For each market:
      - Records the two quotes placed (YES bid, NO bid)
      - Checks: did either quote cross the live best bid?
      - Updates paper inventory and paper PnL
      - Tracks: total fill events, spread captured per fill, position sizing
    """

    def __init__(self, config):
        self.config = config
        self.initial_equity = float(getattr(config, 'simulation_initial_equity', 100.0))

        # Paper inventory:  {condition_id: {YES: float, NO: float}}
        self.inventory: Dict[str, Dict[int, float]] = {}

        # Open simulated orders:  {order_id: {...}}
        self.open_orders: Dict[str, dict] = {}
        self._order_counter = 0

        # Fill history (all fills across all markets)
        self.fills: List[SimulatedFill] = []

        # Financial tracking
        self.total_spent = 0.0    # net USDC paid for BUYs − received from SELLs
        self.realized_pnl = 0.0   # from redeeming YES+NO pairs at $1 each
        self.start_time = time.time()

        # Equity snapshots for sparkline  [(timestamp, equity), ...]
        self.equity_history: List[Tuple[float, float]] = []

        # Market registry  {condition_id: {question, token_pair}}
        self.market_registry: Dict[str, dict] = {}

        # Latest live data per market
        self.latest_bids: Dict[str, List[float]] = {}   # {cid: [yes_bid, no_bid]}
        self.latest_mids: Dict[str, float] = {}          # {cid: market_mid}
        self.latest_hours: Dict[str, float] = {}         # {cid: hours_to_resolution}

        # Per-market latest quotes  {cid: [yes_quote, no_quote]}
        self.latest_quotes: Dict[str, List[float]] = {}

        # Per-market latest own-mid and spread
        self.latest_own_mids: Dict[str, float] = {}
        self.latest_spreads: Dict[str, float] = {}

        # Aggregate counters
        self.total_quote_cycles = 0
        self.total_fills_count = 0
        self.total_orders_placed = 0

        # TUI / runtime controls
        self.running = True
        self.speed_multiplier = 1.0
        self.paused = False

        # Per-market fills for the current cycle (for TUI display)
        self.recent_fills: List[SimulatedFill] = []

        # CSV logging
        self.log_path = getattr(config, 'simulation_log_path', 'logs/simulation.csv')
        os.makedirs(os.path.dirname(self.log_path) or '.', exist_ok=True)
        self._init_csv()

        # JSONL event logging (measurement layer, ROADMAP §7)
        self.event_logger = get_logger(
            getattr(config, 'events_log_path', 'logs/events.jsonl')
        )

        # Track the set of condition IDs we've registered
        self._registered_cids: set = set()

    # ── CSV ──────────────────────────────────────────────────────────────

    def _init_csv(self):
        if not os.path.exists(self.log_path):
            with open(self.log_path, 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow([
                    'timestamp', 'market', 'token', 'side', 'price', 'size',
                    'hours_to_resolution', 'market_spread', 'pnl_impact'
                ])

    def _log_fill(self, fill: SimulatedFill):
        with open(self.log_path, 'a', newline='') as f:
            w = csv.writer(f)
            w.writerow([
                fill.timestamp,
                fill.market_question,
                fill.token,
                fill.side,
                fill.price,
                fill.size,
                fill.hours_to_resolution,
                fill.spread,
                0.0,  # pnl_impact placeholder
            ])

        # Same fill, machine-readable, with the mid needed for mark-outs.
        market_mid = self.latest_mids.get(fill.market_condition_id)
        if market_mid is None:
            token_mid = None
        else:
            token_mid = market_mid if fill.token == 'YES' else 1.0 - market_mid
        self.event_logger.fill(
            mode='paper',
            condition_id=fill.market_condition_id,
            question=fill.market_question,
            token=fill.token,
            side=fill.side,
            price=fill.price,
            size=fill.size,
            fees=0.0,
            mid_at_fill=token_mid,
            market_spread=fill.spread,
            hours_to_resolution=fill.hours_to_resolution,
        )

    # ── ID generation ────────────────────────────────────────────────────

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"sim-{self._order_counter}"

    # ── Market registration ──────────────────────────────────────────────

    def register_market(self, market):
        """Register a market so the engine knows about its tokens."""
        cid = market.condition_id
        if cid not in self._registered_cids:
            self._registered_cids.add(cid)
            self.market_registry[cid] = {
                'question': market.question,
                'token_pair': market.token_pair,
            }
            self.inventory[cid] = {YES: 0.0, NO: 0.0}
            self.latest_bids[cid] = [0.0, 0.0]
            self.latest_mids[cid] = 0.5
            self.latest_hours[cid] = 999.0
            self.latest_quotes[cid] = [0.0, 0.0]
            self.latest_own_mids[cid] = 0.5
            self.latest_spreads[cid] = 0.0

    # ── Balance / inventory ──────────────────────────────────────────────

    def get_balance(self, token_id: str) -> float:
        """Return paper balance for a given token ID."""
        for cid, reg in self.market_registry.items():
            tp = reg['token_pair']
            if tp[YES] == token_id:
                return self.inventory[cid][YES]
            if tp[NO] == token_id:
                return self.inventory[cid][NO]
        return 0.0

    def get_inventory(self, market) -> Tuple[float, float]:
        """Return (yes_held, no_held) for a market."""
        cid = market.condition_id
        inv = self.inventory.get(cid, {YES: 0.0, NO: 0.0})
        return (inv[YES], inv[NO])

    def position_is_neutral(self, market, threshold: float = 5.0) -> Tuple[bool, Tuple[float, float]]:
        """Check if net exposure is within threshold. Returns (neutral, (yes, no))."""
        yes, no = self.get_inventory(market)
        return abs(yes - no) < threshold, (yes, no)

    # ── Order lifecycle ──────────────────────────────────────────────────

    def place_order(self, run, client, token: str, price: float,
                    size: int, side: str, is_FOK: bool) -> Optional[str]:
        """
        Intercept place_order(). Records the quote intent.
        Returns a simulated order ID so the caller can 'cancel' it later.
        """
        if size <= run.minimal_order_size:
            return None

        oid = self._next_order_id()
        self.open_orders[oid] = {
            'token': token,
            'price': price,
            'size': size,
            'side': side,
            'is_FOK': is_FOK,
            'timestamp': time.time(),
        }
        self.total_orders_placed += 1
        return oid

    def cancel_order(self, order_id: str):
        """Cancel a simulated order by ID."""
        self.open_orders.pop(order_id, None)

    def cancel_all_orders(self, market):
        """Cancel all open orders for a given market."""
        tp = market.token_pair
        to_remove = [
            oid for oid, o in self.open_orders.items()
            if o['token'] in tp
        ]
        for oid in to_remove:
            del self.open_orders[oid]

    def get_open_orders(self, market) -> List[str]:
        """Return list of open simulated order IDs for a market."""
        tp = market.token_pair
        return [oid for oid, o in self.open_orders.items() if o['token'] in tp]

    # ── Fill simulation ──────────────────────────────────────────────────

    def check_fills(self, market, live_bids: List[float],
                    hours_to_resolution: float) -> List[SimulatedFill]:
        """
        Compare outstanding quotes against live best bids.

        Fill rule (conservative, lower-bound):
          A BUY quote at price P fills iff P >= live_best_bid.
          (If our bid is at or above the market's best bid, sellers prefer us.)

        Args:
            market: Market dataclass
            live_bids: [yes_best_bid, no_best_bid]
            hours_to_resolution: float

        Returns:
            List of SimulatedFill events that occurred this cycle.
        """
        cid = market.condition_id
        tp = market.token_pair

        yes_bid, no_bid = live_bids
        self.latest_bids[cid] = [yes_bid, no_bid]
        self.latest_hours[cid] = hours_to_resolution

        new_fills: List[SimulatedFill] = []

        for oid, order in list(self.open_orders.items()):
            if order['token'] not in tp:
                continue

            is_yes = (order['token'] == tp[YES])
            market_bid = yes_bid if is_yes else no_bid
            token_idx = YES if is_yes else NO

            # ── Fill condition ──
            if order['price'] < market_bid:
                continue  # our bid is worse than market → no fill

            # ── Apply the fill ──
            inv = self.inventory[cid]
            if order['side'] == 'BUY':
                inv[token_idx] += order['size']
                cost = order['price'] * order['size']
                self.total_spent += cost
            else:  # SELL
                inv[token_idx] = max(0, inv[token_idx] - order['size'])
                proceeds = order['price'] * order['size']
                self.total_spent -= proceeds

            fill = SimulatedFill(
                timestamp=time.time(),
                market_condition_id=cid,
                market_question=self.market_registry[cid]['question'],
                token='YES' if is_yes else 'NO',
                side=order['side'],
                price=order['price'],
                size=order['size'],
                hours_to_resolution=hours_to_resolution,
                spread=self.latest_spreads.get(cid, 0.0),
            )
            new_fills.append(fill)
            self.fills.append(fill)
            self._log_fill(fill)
            self.total_fills_count += 1

            del self.open_orders[oid]

        # Redeem any YES+NO pairs at $1 each
        self._redeem_pairs(cid)

        # Store recent fills for TUI
        self.recent_fills = new_fills

        return new_fills

    def _redeem_pairs(self, cid: str):
        """Redeem equal YES+NO positions at $1 per pair (theoretical).

        Each redeemed pair yields $1. The original cost was already
        recorded via total_spent when the tokens were bought, so we credit
        realized_pnl directly without double-adjusting total_spent.
        """
        inv = self.inventory.get(cid)
        if not inv:
            return
        pairs = min(inv[YES], inv[NO])
        if pairs > 0:
            inv[YES] -= pairs
            inv[NO] -= pairs
            self.realized_pnl += pairs  # $1 per redeemed pair
            self.event_logger.log(
                'redeem',
                mode='paper',
                condition_id=cid,
                pairs=pairs,
                realized_pnl=self.realized_pnl,
            )

    # ── PnL & equity ─────────────────────────────────────────────────────

    def get_equity(self) -> float:
        """Total paper equity = cash + mark-to-market inventory value."""
        inventory_value = 0.0
        for cid, inv in self.inventory.items():
            mid = self.latest_mids.get(cid, 0.5)
            inventory_value += inv[YES] * mid + inv[NO] * (1.0 - mid)

        cash = self.initial_equity - self.total_spent + self.realized_pnl
        return cash + inventory_value

    def get_pnl(self) -> float:
        return self.get_equity() - self.initial_equity

    def record_equity_snapshot(self):
        self.equity_history.append((time.time(), self.get_equity()))

    # ── Stats aggregation ────────────────────────────────────────────────

    def get_stats(self) -> dict:
        uptime = time.time() - self.start_time
        equity = self.get_equity()
        pnl = self.get_pnl()
        orders_per_cycle = self.total_quote_cycles * 2 if self.total_quote_cycles > 0 else 1
        fill_rate = self.total_fills_count / max(1, orders_per_cycle)

        # Average spread captured per fill
        avg_spread = 0.0
        if self.fills:
            avg_spread = sum(f.spread for f in self.fills) / len(self.fills)

        return {
            'uptime': uptime,
            'pnl': pnl,
            'equity': equity,
            'fill_rate': fill_rate,
            'total_fills': self.total_fills_count,
            'quote_cycles': self.total_quote_cycles,
            'orders_placed': self.total_orders_placed,
            'equity_history': self.equity_history,
            'markets_active': len([cid for cid, inv in self.inventory.items()
                                   if (inv[YES] + inv[NO]) > 0]),
            'avg_spread_captured': avg_spread,
            'realized_pnl': self.realized_pnl,
        }

    # ── Market snapshot for TUI ──────────────────────────────────────────

    def get_market_snaps(self, markets) -> List[MarketSnap]:
        """Build render-ready snapshots for all registered markets."""
        snaps = []
        for m in markets:
            cid = m.condition_id
            bids = self.latest_bids.get(cid, [0.0, 0.0])
            quotes = self.latest_quotes.get(cid, [0.0, 0.0])
            inv = self.inventory.get(cid, {YES: 0.0, NO: 0.0})
            snaps.append(MarketSnap(
                question=m.question,
                condition_id=cid,
                hours_to_resolution=self.latest_hours.get(cid, 999),
                yes_bid=bids[YES],
                no_bid=bids[NO],
                yes_quote=quotes[YES],
                no_quote=quotes[NO],
                yes_inventory=inv[YES],
                no_inventory=inv[NO],
                net_exposure=inv[YES] - inv[NO],
                market_mid=self.latest_mids.get(cid, 0.5),
                own_mid=self.latest_own_mids.get(cid, 0.5),
                spread=self.latest_spreads.get(cid, 0.0),
                fills_this_cycle=len([
                    f for f in self.recent_fills
                    if f.market_condition_id == cid
                ]),
            ))
        return snaps
