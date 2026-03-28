
# Volume Generation Bot v1.0

**Panther Platform** | Maker-Maker Ping-Pong Strategy

Automated volume generation bot for Bybit USDT perpetual futures. Designed to hit $100K+ daily notional volume while preserving capital.

## Strategy

The bot runs a **Maker-Maker Ping-Pong** cycle with direction alternation:

1. Places a limit **buy** slightly below mid price (maker order)
2. Waits for fill, then places a limit **sell** at entry + 0.05% spread (maker order)
3. On fill, flips to a **short cycle** (sell-first, buy-to-cover)
4. Repeats, alternating long/short to distribute volume evenly

With $150 equity at 30x leverage, each round trip churns ~$5,400 in notional volume. ~19 round trips hits the $100K daily target. All entries and exits are post-only limit orders at the 0.02% maker fee rate. Net cost per round trip is roughly $0.05.

## Setup

```bash
pip install ccxt
