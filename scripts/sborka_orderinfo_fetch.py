"""Ручная проверка клиента Sborka orderinfo (не тест: ходит в сеть).

Запуск: .venv/bin/python scripts/sborka_orderinfo_fetch.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sborka_integration import build_order_info_fetcher

f = build_order_info_fetcher(Path("sborka_api"))
print("Fetcher built:", f)
res = f(["25661092"])
print("Result:", res)
