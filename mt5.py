import MetaTrader5 as mt5
from notify import send_discord_message


def init_mt5(file_name: str) -> None:
    try:
        if not mt5.initialize():
            message = f"❌ MT5 initialization failed in `{file_name}`"
            send_discord_message("critical", message)
            raise RuntimeError("MT5 initialization failed")
        else:
            message = f"✅ MT5 successfully initialized in `{file_name}`"
            send_discord_message("normal", message)
    except Exception as e:
        error_msg = f"🚨 Exception during MT5 init in `{file_name}`: {e}"
        send_discord_message("critical", error_msg)
        raise

