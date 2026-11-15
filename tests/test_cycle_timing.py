import pandas as pd

from research.studies.timing.experiments import cycle_timing


def test_recovery_requires_valid_entry_and_keeps_existing_stop():
    dates = pd.bdate_range("2024-01-02", periods=6)
    prices = pd.DataFrame({"open": [10, 11, 11, 10, 9, 7.8],
                           "close": [10, 10, 10, 8.5, 7.9, 7.8]}, index=dates)
    signals = dict(zip(dates, [
        ("04", {"close": 10, "plat_low": 8}),
        ("05", {"close": 10, "ext_on": True, "ext_seg": 1, "ext_count": 3}),
        ("04", {"close": 10, "plat_low": 9}),
        ("04", {"close": 10, "plat_low": 9}),
        ("08", {"close": 8.5}),
        ("04", {"close": 7.9, "plat_low": 7.5}),
    ]))
    signals = {date: (stage, {"ext_on": False, **row}) for date, (stage, row) in signals.items()}
    baseline, recovered = [], []
    original, _ = cycle_timing.simulate(prices, signals, ledger=baseline)
    variant, _ = cycle_timing.simulate(prices, signals, ledger=recovered, refill=True)

    assert [(trade["date"], trade["side"]) for trade in baseline] == [
        (dates[0], "buy"), (dates[1], "sell"), (dates[5], "sell"),
    ]
    assert [(trade["date"], trade["side"]) for trade in recovered] == [
        (dates[0], "buy"), (dates[1], "sell"), (dates[3], "buy"), (dates[5], "sell"),
    ]
    assert original.exposure.loc[dates[3]] < 0.3
    assert variant.exposure.loc[dates[3]] > 0.99
    assert recovered[2]["reason"] == "refill"
    assert recovered[2]["stop_price"] == 8
    assert recovered[2]["entry_stop"] == 9
