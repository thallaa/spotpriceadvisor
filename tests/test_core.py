import importlib
import os
import sys
from decimal import Decimal

import pytest


def reload_module_with_token(token_value: str | None, allow_default: str = ""):
    """Reloads spotpriceadvisor_api with environment overrides."""
    sys.modules.pop("spotpriceadvisor_api", None)
    if token_value is None:
        os.environ.pop("SPOTPRICE_TOKEN", None)
    else:
        os.environ["SPOTPRICE_TOKEN"] = token_value
    if allow_default:
        os.environ["SPOTPRICE_ALLOW_DEFAULT"] = allow_default
    else:
        os.environ.pop("SPOTPRICE_ALLOW_DEFAULT", None)
    return importlib.import_module("spotpriceadvisor_api")


def test_default_token_raises():
    with pytest.raises(RuntimeError):
        reload_module_with_token(None)


def test_empty_token_allows_no_auth():
    mod = reload_module_with_token("")
    assert mod.TOKEN == ""


def test_best_q15_window_finds_min_start():
    mod = reload_module_with_token("testtoken")
    start = 1_700_000_000
    ts_prices = [(start + i * 900, Decimal(10 + i)) for i in range(12)]
    ts_prices[4] = (ts_prices[4][0], Decimal("1.0"))  # cheapest streak starts at i=4
    best = mod.best_q15_window(ts_prices, 4)
    assert best[0] == ts_prices[4][0]
    # average should reflect the low segment
    assert best[1] < Decimal("5")


def test_best_q15_window_ignores_gaps():
    mod = reload_module_with_token("testtoken")
    base = 1_700_000_000
    contiguous = [(base + i * 900, Decimal(5 + i)) for i in range(5)]
    gap = (base + 10_000, Decimal("100"))
    tail = [(base + 20_000 + i * 900, Decimal(2)) for i in range(5)]
    ts_prices = contiguous + [gap] + tail
    best = mod.best_q15_window(ts_prices, 3)
    # Best should come from the tail segment with constant price 2
    assert best[0] == tail[0][0]
    assert best[1] == Decimal(2)
