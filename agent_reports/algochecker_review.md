# AlgoChecker Adversarial Review Report

**Date**: 2026-03-02
**Reviewer**: AlgoChecker agent (adversarial reviewer)
**Scope**: All algo engine code — engine.py, parent_order.py, base.py, sor.py, vwap.py, twap.py, is_strategy.py

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 6 (F01, F02, F03, F05, F06, F23) |
| WARNING  | 12 (F04, F07, F08, F09, F10, F11, F12, F13, F15, F16, F20, F21, F24) |
| INFO     | 3 (F17, F18, F19) |

| Priority | Control Gaps |
|----------|-------------|
| P0       | 5 (CG01-CG05) |
| P1       | 4 (CG06-CG09) |
| P2       | 1 (CG10) |

## CRITICAL Findings

### F01 — No defense-in-depth at OM layer
OM has no algo-specific rate limits, no heartbeat dead-man's switch, and no concept of algo vs human orders. Rate limiting only exists in the algo engine process.

### F02 — RateLimiter uses unbounded list
`RateLimiter._timestamps` is an unbounded list. Under sustained throughput, grows linearly. Use deque or token bucket instead.

### F03 — SOR spray mode sends FULL qty to EACH venue (overfill risk)
Spray sends `qty` to each of N venues. If multiple fill, parent gets N*qty. Cancel-on-first-fill races with network latency.
**Fix**: Send `qty/N` per venue, or add parent-level overfill guard.

### F05 — No state persistence / crash recovery
All state is in-memory. Engine crash = orphaned child orders, lost fills, potential duplicate sends on restart.

### F06 — Off-by-one in max orders per algo check
`len(parent.child_orders) > max` should be `>=`. Allows max+1 orders.

### F23 — No hard time limit enforcement
`horizon_seconds` is advisory. If scheduler is stuck in pause loop, algo runs indefinitely. Need hard wall-clock deadline.
**Note**: F23 was partially addressed by the fix-builder's deadline enforcement task.

## WARNING Findings

### F04 — SOR randomization can leak quantity in best mode
### F07 — VWAP circuit breaker doesn't cancel active orders
### F08 — TWAP residual concentrated in final sweep
### F09 — IS adaptive trajectory can oscillate
### F10 — VWAP participation cap bypassed when no volume data
### F11 — Engine assumes connected before first message
### F12 — Auto-resume doesn't verify exchange state
### F13 — No authentication between algo engine and OM
### F15 — No overfill detection in parent_order.process_fill()
### F16 — Spray mode is textbook spoofing (regulatory risk)
### F20 — TWAP cancel-then-aggressive has 10ms race condition
### F21 — Kill switch fails silently when OM disconnected
### F24 — Aggregate notional underestimates exposure

## P0 Control Gaps (must fix before production)

1. **CG01**: No out-of-band kill switch (OM should cancel ALGO-prefixed orders independently)
2. **CG02**: No algo order identification in OM (separate rate limits for algo flow)
3. **CG03**: No state persistence / crash recovery
4. **CG04**: No hard time limit enforcement (partially addressed)
5. **CG05**: No overfill protection

## P1 Control Gaps

6. **CG06**: No per-algo slippage limit
7. **CG07**: No per-algo rate limit (only global)
8. **CG08**: No structured audit trail (JSON events for TCA)
9. **CG09**: No exchange reconnection reconciliation

## P2 Control Gaps

10. **CG10**: No wash trading prevention
