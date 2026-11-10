"""Generate spot-only BUY / EXIT_TO_USDT signals from validated local Crypto Sniper data."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtesting.full_market_runner import ALL_TIMEFRAMES, load_market_timeframes
from spot_strategy_engines import DataInsufficient, STRATEGY_IDS, STRATEGY_SPECS, evaluate_strategy

INTERVAL_MS = {"15m":900000, "30m":1800000, "1h":3600000,
               "2h":7200000, "4h":14400000, "1d":86400000}


def generate(data_dir="data", output_dir="signal_results",
             timeframes=ALL_TIMEFRAMES, max_age_multiples=1.2):
    root, output = Path(data_dir), Path(output_dir)
    manifest_path = root/"download_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("DATA INSUFFICIENT: download_manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("exchange") != "Binance" or manifest.get("market_type") != "spot":
        raise RuntimeError("Refusing non-Binance or non-spot data")
    now_ms = int(datetime.now(timezone.utc).timestamp()*1000)
    folders = sorted(p for p in root.iterdir()
                     if p.is_dir() and not p.name.startswith(".") and p.name.endswith("USDT"))
    signals, statuses = [], []
    for folder in folders:
        frames, tf_status = load_market_timeframes(folder)
        for tf in timeframes:
            if tf not in frames:
                statuses.append({"symbol":folder.name,"timeframe":tf,"strategy":"*",
                                 "status":"DATA INSUFFICIENT",
                                 "reason":tf_status.get(tf,{}).get("reason","timeframe missing")})
                continue
            frame = frames[tf]
            try:
                last_ts = pd.to_datetime(frame.timestamp.iloc[-1],utc=True,errors="raise")
                close_ms = int(last_ts.timestamp()*1000)+INTERVAL_MS[tf]
            except (ValueError,TypeError,IndexError):
                statuses.append({"symbol":folder.name,"timeframe":tf,"strategy":"*",
                                 "status":"DATA INSUFFICIENT","reason":"invalid timestamp"})
                continue
            age = now_ms-close_ms
            if age < -30000 or age > INTERVAL_MS[tf]*max_age_multiples:
                statuses.append({"symbol":folder.name,"timeframe":tf,"strategy":"*",
                                 "status":"STALE DATA","reason":f"last candle age_ms={age}"})
                continue
            for sid in STRATEGY_IDS:
                spec=STRATEGY_SPECS[sid]
                try:
                    out=evaluate_strategy(frame,sid)
                except DataInsufficient as exc:
                    statuses.append({"symbol":folder.name,"timeframe":tf,"strategy":sid,
                                     "status":"DATA INSUFFICIENT","reason":str(exc)})
                    continue
                except (ValueError,TypeError,KeyError) as exc:
                    statuses.append({"symbol":folder.name,"timeframe":tf,"strategy":sid,
                                     "status":"ENGINE ERROR","reason":type(exc).__name__})
                    continue
                last=out.iloc[-1]
                if spec["type"]=="overlay":
                    overlay=str(last.get("overlay_signal","NEUTRAL"))
                    if overlay!="NEUTRAL":
                        signals.append({"symbol":folder.name,"timeframe":tf,"strategy":sid,
                            "strategy_name":spec["name"],"action":"FEATURE_ONLY",
                            "overlay_signal":overlay,"tradeable_for_user":False,
                            "price":float(last.close),"candle_close_utc":str(last.timestamp),
                            "reason":"Research/ML overlay only; not a spot order instruction"})
                    statuses.append({"symbol":folder.name,"timeframe":tf,"strategy":sid,
                                     "status":"OVERLAY ONLY","reason":"not executable"})
                    continue
                action=str(last.get("action","HOLD"))
                if action in ("BUY","EXIT_TO_USDT"):
                    strength=last.get("signal_strength",0.0)
                    strength=0.0 if pd.isna(strength) else float(strength)
                    meta={}
                    for key in ("z","grid_level","lockout","adx","chop","oi_change_3","oi_change_5"):
                        value=last.get(key)
                        if value is not None and not pd.isna(value):
                            meta[key]=value.item() if hasattr(value,"item") else value
                    signals.append({"symbol":folder.name,"timeframe":tf,"strategy":sid,
                        "strategy_name":spec["name"],"action":action,
                        "tradeable_for_user":bool(spec["tradeable"]),
                        "price":float(last.close),"signal_strength":strength,
                        "candle_close_utc":str(last.timestamp),"metadata":meta,
                        "reason":"Completed-candle signal; bearish cues only exit/avoid to USDT"})
                    statuses.append({"symbol":folder.name,"timeframe":tf,"strategy":sid,
                                     "status":"SIGNAL","reason":action})
                else:
                    statuses.append({"symbol":folder.name,"timeframe":tf,"strategy":sid,
                                     "status":"NO SIGNAL","reason":"no event on latest closed candle"})
    output.mkdir(parents=True,exist_ok=True)
    payload={"generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "exchange":"Binance","market_type":"spot","markets_scanned":len(folders),
        "signals_count":len(signals),"signals":signals,
        "execution_policy":"signals_only_no_orders_no_short_selling_no_leverage_no_futures"}
    (output/"spot_signals.json").write_text(json.dumps(payload,indent=2,allow_nan=False),encoding="utf-8")
    pd.DataFrame(signals).to_csv(output/"spot_signals.csv",index=False)
    pd.DataFrame(statuses).to_csv(output/"spot_engine_status.csv",index=False)
    return payload


def main():
    p=argparse.ArgumentParser(description="Generate per-coin spot-only strategy signals")
    p.add_argument("--data-dir",default="data")
    p.add_argument("--output-dir",default="signal_results")
    p.add_argument("--timeframes",default="15m,30m,1h,2h,4h,1d")
    p.add_argument("--max-age-multiples",type=float,default=1.2)
    a=p.parse_args()
    tfs=tuple(x.strip() for x in a.timeframes.split(",") if x.strip())
    if not tfs or any(x not in INTERVAL_MS for x in tfs) or a.max_age_multiples<=0:
        p.error("invalid timeframes or max-age-multiples")
    result=generate(a.data_dir,a.output_dir,tfs,a.max_age_multiples)
    print(f"Markets scanned: {result['markets_scanned']}; signals: {result['signals_count']}")
    print(f"JSON: {Path(a.output_dir)/'spot_signals.json'}")
    print("Signals only; no exchange orders.")


if __name__=="__main__":
    main()
