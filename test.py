import MetaTrader5 as mt5

def calculate_profit(action: str, symbol: str, lot: float, open_price: float, close_price: float):
    # Ensure MT5 connection
    if not mt5.initialize():
        print("❌ Failed to initialize MT5:", mt5.last_error())
        return None

    # Determine order type
    action_type = mt5.ORDER_TYPE_BUY if action.lower() == 'buy' else mt5.ORDER_TYPE_SELL

    # Calculate profit
    profit = mt5.order_calc_profit(action_type, symbol, lot, open_price, close_price)

    if profit is None:
        print("⚠️ Calculation failed:", mt5.last_error())
        mt5.shutdown()
        return None

    mt5.shutdown()
    print(f"✅ Profit for {action.upper()} {symbol}: ${profit:.2f}")
    return profit


# Example test cases
calculate_profit('buy', "XAUUSD", 0.5, 3963.0, 3978.0)   # BUY → price up
calculate_profit('sell', "XAUUSD", 0.5, 3963.0, 3948.0)  # SELL → price down
