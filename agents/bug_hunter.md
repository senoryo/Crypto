# Bug Hunter Agent

## Role
Perform static analysis across the entire codebase to find code-level bugs, async pitfalls, and anti-patterns that could cause runtime failures.

## Scope
- All Python source files in the project
- Exclude: `tests/`, `agents/`, `.git/`, `__pycache__/`, `venv/`

## What to Look For

### Async Pitfalls
- **Missing await**: Coroutine calls without `await` — the coroutine is created but never executed
- **Fire-and-forget tasks**: `asyncio.create_task()` where the returned task is not stored — if the task raises an exception, it is silently lost
- **Resource leaks**: WebSocket connections or file handles opened but never closed, especially in error paths

### Variable Safety
- **Unbound variables**: Variables used inside conditional blocks that may not execute, leaving the variable undefined on certain code paths
- **Type coercion**: Unsafe `float()` or `int()` conversions without `try/except` that would crash on malformed input

### Error Handling
- **Bare except**: `except:` or `except Exception: pass` that swallow all exceptions, hiding real errors
- **Silent exceptions**: `try/except` blocks where the except clause does nothing meaningful (no logging, no re-raise, no error response)
- **Incomplete error propagation**: Error conditions detected but not communicated back to the caller or upstream component

### Concurrency
- **Race conditions**: Shared mutable state accessed from multiple async tasks without locks or atomic operations
- **Non-atomic read-modify-write**: Patterns like `x = dict[key]; x += 1; dict[key] = x` that can interleave with concurrent access
- **Lock ordering**: Multiple locks acquired in inconsistent order across different code paths (deadlock risk)

### Logic Errors
- **Off-by-one**: Loop bounds, slice indices, or comparisons that are off by one
- **Incorrect operator**: Using `=` vs `==`, `and` vs `or`, `is` vs `==` incorrectly
- **Dead code**: Unreachable code after return/raise/break, or conditions that are always true/false

## Learned Themes

### Theme: Duplicate assignments to the same variable with one guarded and one unguarded means the guard is dead code
When a variable is assigned the same expression twice -- once unguarded and once inside a try/except -- the guarded assignment is unreachable because the unguarded one either succeeds (making the guard redundant) or crashes before reaching it. Look for patterns where defensive code is rendered ineffective by an earlier eager assignment.
**Origin**: `om/order_manager.py` had `price = float(...)` on line 97 (unguarded) followed by the same expression inside `try/except ValueError` on line 100, making the error handler dead code.

### Theme: Private attribute access across module boundaries creates invisible coupling
When code directly accesses another object's private attributes (e.g., `client._ws`, `client._connected`), changes to the internal implementation silently break the accessor. Scan for cross-object `._` access patterns and flag them as coupling risks, recommending public interface methods instead.
**Origin**: GUIBROKER accessed `_om_client._ws` directly, and CoinbaseFIXAdapter accessed `_client._connected` directly, bypassing the WSClient and FIXClient abstractions.

### Theme: Fire-and-forget coroutines from non-async contexts (signal handlers, call_later callbacks) silently lose exceptions
When `asyncio.ensure_future()` or `asyncio.create_task()` is called from a signal handler or `call_later` callback and the returned future is not stored, any exception raised by the coroutine is silently discarded. This is especially dangerous for shutdown and cleanup code where errors should be surfaced.
**Origin**: OM signal handler and posmanager `_schedule_broadcast` both used fire-and-forget patterns that could silently lose exceptions.

### Theme: Shared mutable state accessed across await boundaries requires explicit synchronization even in single-threaded async code
Python's GIL prevents data corruption in synchronous code, but async coroutines can interleave at every `await` point. A dict that is read in one coroutine and modified in another across `await` boundaries can produce inconsistent state. If one path already uses a lock (e.g., for positions), check whether sibling data structures (e.g., the order book) need the same protection.
**Origin**: `self.orders` in OM was accessed by both GUIBROKER message handlers and EXCHCONN execution report handlers concurrently across await points, with no lock (while `_positions` had one).

### Theme: CSS source order determines override precedence at equal specificity — rules intended as defaults must precede rules intended as overrides
When two CSS rules have the same selector specificity, the one appearing later in the source file wins. This means "default hidden" rules (`display: none`) must be placed BEFORE media query blocks that conditionally show the element. Placing defaults after the media query silently negates the override. When reviewing frontend code, verify that any "hidden by default, shown conditionally" pattern has correct source ordering.
**Origin**: `#mobile-tabs { display: none }` was placed after `@media (max-width: 768px) { #mobile-tabs { display: flex } }` in styles.css, causing the tab bar to be hidden on all viewports because the later rule always won.
