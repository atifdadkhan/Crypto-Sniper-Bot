import numpy as np
import pandas as pd
import pytest

from spot_strategy_engines import (
    DataInsufficient, OVERLAY_ONLY, STRATEGY_IDS, evaluate_all, evaluate_strategy,
)
from backtesting.spot_strategy_runner import simulate_spot


def sample_frame(n=600, features=True):
    rng=np.random.default_rng(2026)
    close=100*np.exp(np.cumsum(rng.normal(0.0002,0.008,n)))
    op=np.r_[close[0],close[:-1]]
    frame=pd.DataFrame({
        "timestamp":pd.date_range("2025-01-01",periods=n,freq="15min",tz="UTC"),
        "open":op,"high":np.maximum(op,close)*(1+rng.uniform(0.0001,0.01,n)),
        "low":np.minimum(op,close)*(1-rng.uniform(0.0001,0.01,n)),
        "close":close,"volume":rng.uniform(100,1000,n),
    })
    if features:
        frame["oi"]=1000+np.cumsum(rng.uniform(0,3,n))
        frame["cvd"]=np.cumsum(rng.normal(0,10,n))
        frame["funding_rate"]=rng.normal(0,0.0001,n)
        frame["spot_price"]=close
        frame["futures_price"]=close*(1+rng.normal(0.0001,0.001,n))
        frame["expiry_days"]=30.0
    return frame


def test_all_12_independent_engines_are_registered():
    assert len(STRATEGY_IDS)==12
    assert len(set(STRATEGY_IDS))==12


@pytest.mark.parametrize("sid",STRATEGY_IDS)
def test_every_engine_is_spot_safe(sid):
    out=evaluate_strategy(sample_frame(),sid)
    assert len(out)==600
    assert set(out.position.unique()).issubset({0,1})
    assert set(out.action.unique()).issubset({"BUY","EXIT_TO_USDT","HOLD","FEATURE_ONLY"})


@pytest.mark.parametrize("sid",sorted(OVERLAY_ONLY))
def test_funding_and_basis_are_feature_only(sid):
    out=evaluate_strategy(sample_frame(),sid)
    assert not out.tradeable_for_user.any()
    assert not out.tradeable.any()
    assert out.action.eq("FEATURE_ONLY").all()


@pytest.mark.parametrize("sid,column",[
    ("donchian_oi","oi"),("cvd_short","cvd"),("funding_arb","funding_rate"),
    ("funding_meanrev","funding_rate"),("basis_trade","futures_price"),
    ("funding_meanrev_chop","funding_rate"),("compression_breakout","oi"),
])
def test_missing_special_data_fails_closed(sid,column):
    with pytest.raises(DataInsufficient,match="missing required column"):
        evaluate_strategy(sample_frame().drop(columns=[column]),sid)


def test_short_conditions_map_to_exit_to_usdt_not_short_position():
    out=evaluate_strategy(sample_frame(),"donchian_ema_short")
    assert set(out.position.unique()).issubset({0,1})
    assert not out.position.eq(-1).any()
    assert out.entry_short.any() or out.exit_short.any()


def test_a_b_c_are_variants_of_one_engine_not_three_engines():
    frame=sample_frame()
    a,b,c=[evaluate_strategy(frame,"keltner_trend",v) for v in "ABC"]
    assert a.variant.eq("A").all() and b.variant.eq("B").all() and c.variant.eq("C").all()
    pd.testing.assert_series_equal(a.entry_long,b.entry_long)
    pd.testing.assert_series_equal(a.entry_long,c.entry_long)


def test_causal_last_bar_signal_is_unchanged_by_future_rows():
    frame=sample_frame()
    full=evaluate_strategy(frame,"zscore_reversion")
    prefix=evaluate_strategy(frame.iloc[:-20].copy(),"zscore_reversion")
    for key in ("entry_long","exit_long","action","signal_strength"):
        left,right=full[key].iloc[-21],prefix[key].iloc[-1]
        if isinstance(left,(float,np.floating)):
            assert left==pytest.approx(right,nan_ok=True)
        else:
            assert left==right


def test_evaluate_all_reports_missing_data_per_engine():
    results=evaluate_all(sample_frame(features=False))
    assert len(results)==12
    assert results["keltner_trend"].iloc[-1].action in {"BUY","EXIT_TO_USDT","HOLD"}
    assert results["funding_arb"].iloc[0].status=="DATA INSUFFICIENT"


def test_spot_backtest_enters_next_open_and_deducts_costs():
    n=30
    close=np.linspace(100,130,n)
    op=np.r_[close[0],close[:-1]]
    bars=pd.DataFrame({"timestamp":pd.date_range("2026-01-01",periods=n,freq="15min",tz="UTC"),
        "open":op,"high":np.maximum(op,close)+1,"low":np.minimum(op,close)-1,"close":close})
    signals=pd.DataFrame({"entry_long":[False]*15+[True]+[False]*(n-16),
        "exit_long":[False]*n})
    trades=simulate_spot("TESTUSDT","15m",bars,signals,"grid",0.12)
    assert len(trades)==1
    assert trades.iloc[0].side=="BUY"
    assert trades.iloc[0].entry_price==pytest.approx(op[16])
    assert trades.iloc[0].cost_r>0


def test_spot_backtest_uses_stop_first_for_ambiguous_candle():
    n=10
    bars=pd.DataFrame({"timestamp":pd.date_range("2026-01-01",periods=n,freq="15min",tz="UTC"),
        "open":[100.0]*n,"high":[101.0]*n,"low":[99.0]*n,"close":[100.0]*n})
    bars.loc[16,["high","low"]]=[110.0,90.0]
    signals=pd.DataFrame({"entry_long":[False]*15+[True]+[False]*(n-16),"exit_long":[False]*n})
    trades=simulate_spot("TESTUSDT","15m",bars,signals,"grid",0.0)
    assert len(trades)==1
    assert trades.iloc[0].exit_reason=="STOP_FIRST"
