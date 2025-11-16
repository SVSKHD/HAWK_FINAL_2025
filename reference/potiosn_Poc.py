import MetaTrader5 as mt5
from datetime import datetime


def make_astra_tag() -> str:
    """
    Returns today's comment prefix: Astra-DDMMYY
    """
    return "AstraBot-" + datetime.utcnow().strftime("%d%m%y")


tag = make_astra_tag()   # e.g., Astra-151125


# -------------------------------
# SAFELY GET ALL POSITIONS
# -------------------------------

if not mt5.initialize():
    print("not connected")
else:
    positions = mt5.positions_get()

    if positions is None:
        print("⚠ No positions returned. MT5 may not be connected.")
        print("MT5 last_error:", mt5.last_error())
        exit()

    print(f"Total positions = {len(positions)}")

    # -------------------------------
    # FILTER ONLY Astra positions
    # -------------------------------
    astra_positions = [
        p for p in positions
        if getattr(p, "comment", "").startswith(tag)
    ]

    print(f"Astra positions matching '{tag}' = {len(astra_positions)}\n")

    for p in astra_positions:
        print(f"{p.ticket}  {p.symbol}  {p.volume}  {p.comment}")

    import MetaTrader5 as mt5


def get_positions_by_symbols(symbol: str):
    positions = mt5.positions_get(symbol=symbol)
    return positions


# Example usage
positions = get_positions_by_symbols("BTCUSD")
print(len(positions))
