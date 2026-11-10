"""Merge batch-level spot autopilot research and signal artifacts without double counting."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _metrics(trades):
    if trades.empty:
        return {"trades":0,"win_rate_pct":None,"profit_factor":None,"net_r":0.0,"max_drawdown_r":0.0}
    net=pd.to_numeric(trades.net_r,errors="coerce").dropna()
    wins=float(net[net>0].sum()); losses=float(-net[net<0].sum())
    eq=net.cumsum(); dd=eq-eq.cummax().clip(lower=0)
    return {"trades":int(len(net)),"win_rate_pct":round(float((net>0).mean()*100),2),
        "profit_factor":round(wins/losses,3) if losses>0 else None,
        "net_r":round(float(net.sum()),3),"max_drawdown_r":round(float(dd.min()),3)}


def _batch_order(path):
    try: return int(path.parent.parent.parent.name.rsplit("-",1)[1])
    except (ValueError,IndexError): return path.parent.parent.parent.name


def aggregate(input_dir, output_dir, minimum_oos_trades=30):
    root,out=Path(input_dir),Path(output_dir)
    summaries=sorted(root.glob("*/backtest_results/spot_strategy_engines/spot_strategy_summary.json"),
                     key=_batch_order)
    if not summaries:
        raise RuntimeError("No spot strategy batch summaries were restored")
    combined,trade_parts,signal_rows,status_parts=[],[],[]
    seen=set(); duplicate_symbols=[]; batch_names=[]
    for summary_path in summaries:
        batch_name=summary_path.parents[2].name
        batch_names.append(batch_name)
        part=json.loads(summary_path.read_text(encoding="utf-8"))
        keep=set()
        for symbol in sorted(part):
            if symbol in seen:
                duplicate_symbols.append({"symbol":symbol,"skipped_batch":batch_name})
            else:
                seen.add(symbol); keep.add(symbol); combined.append((symbol,part[symbol]))
        trade_path=summary_path.parent/"spot_strategy_oos_trades.csv"
        if trade_path.is_file():
            try:
                trades=pd.read_csv(trade_path)
            except pd.errors.EmptyDataError:
                trades=pd.DataFrame()
            if not trades.empty and "symbol" in trades:
                trades=trades[trades.symbol.astype(str).isin(keep)]
                if not trades.empty: trade_parts.append(trades)
        artifact_root=summary_path.parents[2]
        signal_path=artifact_root/"signal_results/spot_signals.json"
        if signal_path.is_file():
            try:
                payload=json.loads(signal_path.read_text(encoding="utf-8"))
                signal_rows.extend(x for x in payload.get("signals",[]) if x.get("symbol") in keep)
            except (ValueError,TypeError):
                pass
        status_path=artifact_root/"signal_results/spot_engine_status.csv"
        if status_path.is_file():
            try:
                statuses=pd.read_csv(status_path)
                if not statuses.empty and "symbol" in statuses:
                    statuses=statuses[statuses.symbol.astype(str).isin(keep)]
                    if not statuses.empty: status_parts.append(statuses)
            except (ValueError, OSError):
                pass

    summary=dict(combined)
    trades=pd.concat(trade_parts,ignore_index=True) if trade_parts else pd.DataFrame()
    out.mkdir(parents=True,exist_ok=True)
    trades.to_csv(out/"spot_strategy_oos_trades.csv",index=False)
    (out/"spot_strategy_summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False),encoding="utf-8")
    per_coin=[]
    for symbol,item in summary.items():
        for tf,engines in item.get("strategies",{}).items():
            for sid,record in engines.items():
                m=record.get("out_of_sample",{})
                per_coin.append({"symbol":symbol,"timeframe":tf,"strategy":sid,
                    "status":record.get("status"),"oos_trades":m.get("trades",0),
                    "oos_win_rate_pct":m.get("win_rate_pct"),"oos_profit_factor":m.get("profit_factor"),
                    "oos_net_r":m.get("net_r"),"oos_max_drawdown_r":m.get("max_drawdown_r")})
    pd.DataFrame(per_coin).to_csv(out/"per_coin_strategy_leaderboard.csv",index=False)

    global_rows=[]
    if not trades.empty:
        for (tf,sid),group in trades.groupby(["timeframe","strategy"]):
            m=_metrics(group.sort_values("entry_time").reset_index(drop=True))
            markets=int(group.symbol.nunique())
            status=("DATA INSUFFICIENT" if m["trades"]<minimum_oos_trades or markets<5 else
                "PROMISING — NEEDS WALK-FORWARD" if m["profit_factor"] is not None
                and m["profit_factor"]>=1.20 and m["net_r"]>0 else
                "EXPERIMENTAL — NOT APPROVED")
            global_rows.append({"timeframe":tf,"strategy":sid,"markets":markets,
                "status":status,"oos_trades":m["trades"],"oos_win_rate_pct":m["win_rate_pct"],
                "oos_profit_factor":m["profit_factor"],"oos_net_r":m["net_r"],
                "oos_max_drawdown_r":m["max_drawdown_r"]})
    board=pd.DataFrame(global_rows)
    if not board.empty:
        order={"PROMISING — NEEDS WALK-FORWARD":0,"EXPERIMENTAL — NOT APPROVED":1,"DATA INSUFFICIENT":2}
        board["_rank"]=board.status.map(order).fillna(3)
        board=board.sort_values(["_rank","oos_profit_factor","oos_net_r"],
            ascending=[True,False,False],na_position="last").drop(columns="_rank")
    board.to_csv(out/"global_oos_strategy_leaderboard.csv",index=False)

    signals_payload={"generated_at_utc":pd.Timestamp.now(tz="UTC").isoformat(),
        "exchange":"Binance","market_type":"spot","markets_scanned":len(summary),
        "signals_count":len(signal_rows),"signals":signal_rows,
        "execution_policy":"signals_only_no_orders_no_short_selling_no_leverage_no_futures"}
    (out/"spot_signals.json").write_text(json.dumps(signals_payload,indent=2,allow_nan=False),encoding="utf-8")
    pd.DataFrame(signal_rows).to_csv(out/"spot_signals.csv",index=False)
    if status_parts:
        pd.concat(status_parts,ignore_index=True).to_csv(out/"spot_engine_status.csv",index=False)
    pd.DataFrame(duplicate_symbols).to_csv(out/"duplicate_symbols.csv",index=False)

    report=["# Crypto Sniper Spot Autopilot — Aggregated Research","",
        f"Completed batch artifacts: {len(batch_names)}",
        f"Unique spot markets analysed: {len(summary)}",
        f"Duplicate symbols excluded: {len(duplicate_symbols)}",
        f"Out-of-sample trade rows: {len(trades)}",
        f"Latest completed-candle signals in restored batches: {len(signal_rows)}",
        "Each market/strategy is independently evaluated; BTC results are never copied to ETH or other coins.",
        "Spot-only: bearish/short conditions map to EXIT_TO_USDT. Funding/basis overlays are feature-only.",
        "Missing OI/CVD/funding/basis history remains DATA INSUFFICIENT; no feature values are fabricated.",
        "Pooled R is a trade-level summary, not a capital-weighted portfolio return.",
        "No strategy is automatically promoted to live execution. No exchange orders are sent.","",
        "| Timeframe | Strategy | Markets | OOS trades | PF | Net R | Max DD R | Status |",
        "|---|---|---:|---:|---:|---:|---:|---|"]
    for r in board.to_dict("records"):
        report.append(f"| {r['timeframe']} | {r['strategy']} | {r['markets']} | {r['oos_trades']} | {r['oos_profit_factor']} | {r['oos_net_r']} | {r['oos_max_drawdown_r']} | {r['status']} |")
    (out/"spot_autopilot_report.md").write_text("\n".join(report)+"\n",encoding="utf-8")
    return {"markets":len(summary),"trades":len(trades),"signals":len(signal_rows),
            "leaderboard":str(out/"global_oos_strategy_leaderboard.csv")}


def main():
    p=argparse.ArgumentParser(description="Aggregate Binance spot autopilot batches")
    p.add_argument("--input-dir",default="batch_results")
    p.add_argument("--output-dir",default="backtest_results/spot_autopilot")
    p.add_argument("--minimum-oos-trades",type=int,default=30)
    a=p.parse_args()
    if a.minimum_oos_trades<1: p.error("minimum-oos-trades must be >=1")
    result=aggregate(a.input_dir,a.output_dir,a.minimum_oos_trades)
    print(f"Markets: {result['markets']}; OOS trades: {result['trades']}; signals: {result['signals']}")
    print(result["leaderboard"])


if __name__=="__main__":
    main()
