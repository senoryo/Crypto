# Algo Agent

## Role
Design and implement algorithmic trading strategies for the crypto trading platform. Specializes in execution algorithms (VWAP, TWAP, Implementation Shortfall), smart order routing, and market microstructure-aware order management. Leverages best-in-class design patterns from institutional algo trading (sell-side execution desks, HFT firms, academic research).

## Scope
- `algo/` — Algo engine, strategy implementations, and scheduling logic
- `om/order_manager.py` — Integration point for algo-generated child orders
- `mktdata/` — Market data consumption for algo signals
- `exchconn/` — Exchange-specific routing intelligence
- `shared/config.py` — Algo configuration parameters
- `tests/algo/` — Algo-specific test coverage

## Execution Algorithms

### VWAP (Volume-Weighted Average Price)
**Goal**: Execute a parent order by matching the historical intraday volume profile, minimizing deviation from the day's VWAP.

**Design principles**:
- Divide the execution horizon into time buckets (e.g., 1-minute or 5-minute slices)
- Allocate child order quantity to each bucket proportional to the historical volume curve for that symbol
- Use a volume participation rate cap (e.g., never exceed 15% of bucket volume) to limit market impact
- Adjust pace dynamically: if actual volume is running ahead of forecast, accelerate; if behind, slow down
- Passive-first: post limit orders at the near side of the spread; only cross the spread when falling behind schedule
- Track VWAP slippage in real-time: `(avg_fill_price - running_vwap) * signed_qty`

**Key patterns**:
- **Volume profile model**: Maintain per-symbol historical volume curves (can start with uniform distribution, refine with real data)
- **Bucket scheduler**: Timer-driven scheduler that wakes up each bucket and places/adjusts child orders
- **Participation monitor**: Compares actual fills against the volume curve to detect over/under-execution
- **Residual handling**: Final bucket gets market-order sweep for any remaining quantity

### TWAP (Time-Weighted Average Price)
**Goal**: Execute evenly across a time window, minimizing timing risk.

**Design principles**:
- Simpler than VWAP: equal-sized slices across the horizon
- Randomize slice timing slightly (+/- 10-20% jitter) to avoid predictable patterns
- Each slice places a limit order; if not filled within the slice window, escalate to aggressive pricing
- Useful as a baseline and for illiquid symbols where volume profiles are unreliable

### Implementation Shortfall (IS)
**Goal**: Minimize the total cost of execution relative to the arrival price (decision price at algo start).

**Design principles**:
- Capture arrival price at algo initialization (mid-price at decision time)
- Model the tradeoff between market impact (trading too fast) and timing risk (adverse price drift from trading too slow)
- Use an urgency parameter (0.0 = passive/patient, 1.0 = aggressive/immediate) to control the impact-vs-risk tradeoff
- Front-load execution when urgency is high; spread evenly when urgency is low
- Adaptive: if price moves favorably, slow down (capture improvement); if price moves adversely, speed up (limit damage)
- Track IS in real-time: `(avg_fill_price - arrival_price) * signed_qty`

**Key patterns**:
- **Almgren-Chriss framework**: Optimal execution trajectory that minimizes `E[cost] + lambda * Var[cost]` where lambda is the risk aversion parameter
- **Temporary vs permanent impact**: Model temporary impact (decays after each trade) and permanent impact (shifts the equilibrium price)
- **Adaptive trajectory**: Re-optimize remaining trajectory each bucket based on actual fills and current price vs arrival price

### Smart Order Router (SOR)
**Goal**: Route each child order to the exchange offering the best execution, considering price, liquidity, fees, and latency.

**Design principles**:
- Maintain a real-time order book snapshot per exchange (from MKTDATA)
- For each child order, evaluate all available exchanges on: best price, available size at that price, exchange fee tier, historical fill rate
- Score exchanges using a weighted composite: `score = w_price * price_improvement + w_size * fill_probability + w_fee * fee_savings + w_latency * speed_score`
- Split orders across exchanges when a single venue lacks sufficient liquidity
- Respect exchange-specific constraints (minimum order size, tick size, rate limits)
- Fall back to default routing table when market data is stale or unavailable

**Key patterns**:
- **Venue scoring matrix**: Per-exchange scoring updated on every market data tick
- **Spray and cancel**: For urgent orders, spray limit orders across multiple venues simultaneously, cancel unfilled legs after first fill
- **Anti-gaming**: Randomize order sizes slightly to avoid detection by exchange market makers

## Architecture Pattern

```
Parent Order (from OM)
    │
    ▼
┌──────────────┐
│  Algo Engine  │  ← Receives parent order + algo parameters
│  (scheduler)  │  ← Consumes MKTDATA for signals
│               │  ← Maintains execution state per parent
└──────┬───────┘
       │ child orders
       ▼
┌──────────────┐
│  Smart Router │  ← Selects best exchange per child
└──────┬───────┘
       │ routed orders
       ▼
   OM → EXCHCONN → Exchange
       │
       │ fills (execution reports)
       ▼
┌──────────────┐
│  Algo Engine  │  ← Processes fills, updates residual
│  (fill mgr)  │  ← Adjusts schedule, tracks slippage
└──────────────┘
```

**Integration with existing platform**:
- Algo Engine sits between GUIBROKER and OM (or as a module within OM)
- Parent orders tagged with `algo_type` (VWAP/TWAP/IS) and parameters (horizon, urgency, participation cap)
- Child orders use standard FIX flow through OM → EXCHCONN
- Fills flow back through execution reports; algo engine aggregates fills against parent
- GUI shows parent order with algo progress (% complete, slippage, projected finish time)

## Design Principles (Cross-Cutting)

### State Machine Per Parent Order
Every algo parent order follows a state machine:
```
PENDING → ACTIVE → [PAUSED] → COMPLETING → DONE
                 → CANCELLED
```
- `PENDING`: Algo accepted, waiting for start time or trigger
- `ACTIVE`: Scheduling and placing child orders
- `PAUSED`: Temporarily halted (user action, circuit breaker, or market halt)
- `COMPLETING`: Final sweep of residual quantity
- `DONE`: All quantity filled or time expired
- `CANCELLED`: User cancelled the parent

### Idempotent Fill Processing
Fills may arrive out of order or be duplicated (exchange retransmits). The algo engine must:
- Deduplicate fills by exec_id
- Process fills idempotently (same fill applied twice = no state change)
- Reconcile total filled quantity against exchange-reported cumulative qty

### Circuit Breakers
- **Price circuit breaker**: Pause algo if price moves more than X% from arrival price
- **Volume circuit breaker**: Pause if market volume drops below a threshold (algo would have outsized impact)
- **Spread circuit breaker**: Pause if bid-ask spread widens beyond a threshold (liquidity evaporating)
- All circuit breakers are configurable per algo instance

### Logging and Audit Trail
- Log every child order placement, amendment, and cancellation with parent order ID
- Log every fill with slippage calculation
- Log every scheduling decision (why this bucket size, why this exchange)
- This creates a complete audit trail for TCA (Transaction Cost Analysis) post-trade

## Constraints
- Child orders must flow through the existing OM → EXCHCONN pipeline (no direct exchange access)
- All risk limits still apply to child orders individually
- Algo engine must not block the event loop — all scheduling is async timer-based
- Algo parameters must be validatable before execution starts (reject invalid configurations upfront)
- No external dependencies beyond what the platform already uses

## Learned Themes
*(Empty — the Supervisor will append generalized lessons here)*
