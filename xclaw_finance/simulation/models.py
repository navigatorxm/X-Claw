"""
Simulation mode constants — static price feed and default virtual balances.

Prices are intentionally static so simulation results are deterministic and
reproducible. No external API calls are needed or made.
"""
from __future__ import annotations
from decimal import Decimal

# Static price feed for simulation (USD pairs)
SIMULATION_PRICES: dict[str, Decimal] = {
    "BTCUSDT":  Decimal("67500.00"),
    "ETHUSDT":  Decimal("3450.00"),
    "SOLUSDT":  Decimal("185.00"),
    "BNBUSDT":  Decimal("590.00"),
    "USDCUSDT": Decimal("1.00"),
    "XRPUSDT":  Decimal("0.62"),
    "ADAUSDT":  Decimal("0.45"),
    "DOTUSDT":  Decimal("8.50"),
}

# Virtual balances seeded for each new simulation wallet
DEFAULT_SIM_BALANCES: dict[str, Decimal] = {
    "USDT": Decimal("100000.00"),   # $100k virtual capital
    "BTC":  Decimal("1.0"),
    "ETH":  Decimal("10.0"),
}
