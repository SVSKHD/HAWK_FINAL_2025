# main.py (patched)

from runner import run

def main():
    run(["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY"], interval_sec=1.0)

if __name__ == "__main__":
    main()
