from services.sborka_integration import build_order_info_fetcher
from pathlib import Path

f = build_order_info_fetcher(Path("sborka_api"))
print("Fetcher built:", f)
res = f(["25661092"])
print("Result:", res)
