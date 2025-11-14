import metatrader5 as mt5



def init():
    if not mt5.initialize():
        raise RuntimeError("MT5 initialization failed")
    symbol = "XAUUSD"
    info = mt5.symbol_info(symbol)
    print("symbol", info.path)
    print("Digits:", info.digits)
    print("Point:", info.point)
    print("Tick size:", info.trade_tick_size)
    print("Tick value:", info.trade_tick_value)
    print("info", info)

init()
