from independent_engines import evaluate_engines

def bars(n=25):
    return [{"open":100.0,"high":101.0,"low":99.0,"close":100.5,"volume":100.0} for _ in range(n)]

def test_missing_data_fails_closed():
    assert evaluate_engines([]) == []
    assert evaluate_engines(bars(24)) == []
    assert evaluate_engines(bars(), "BUY") == []

def test_sweep_is_independent():
    x=bars()
    x[-1]={"open":99.0,"high":101.0,"low":98.0,"close":100.6,"volume":200.0}
    evidence=evaluate_engines(x)
    assert len(evidence)==4
    assert next(e for e in evidence if e.engine=="Liquidity Sweep").confirmed
    assert not next(e for e in evidence if e.engine=="Breakout").confirmed

def test_bad_ohlcv_rejected():
    x=bars()
    x[-1]["close"]=float("nan")
    assert evaluate_engines(x)==[]
