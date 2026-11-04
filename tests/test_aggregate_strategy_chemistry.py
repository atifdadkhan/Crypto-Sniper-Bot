import json

import pandas as pd

from backtesting.aggregate_strategy_chemistry import main


def _write_batch(root, offset, symbols, trade_rows):
    batch = root / f"at-trendhive-chemistry-batch-{offset}"
    batch.mkdir()
    (batch / "batch_offset.json").write_text(
        json.dumps({"offset": offset, "downloaded_symbols": symbols}),
        encoding="utf-8",
    )
    summary = {
        symbol: {
            "asset_class": "crypto_or_unverified",
            "strategies": {
                "15m": {
                    "combo": {
                        "research_status": "EXPERIMENTAL — NOT APPROVED",
                        "in_sample": {"trades": 1},
                        "out_of_sample": {"trades": 0},
                        "segments_tested": 1,
                    }
                }
            },
        }
        for symbol in symbols
    }
    (batch / "strategy_chemistry_summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    pd.DataFrame(trade_rows, columns=["symbol", "strategy", "timeframe", "sample_period"]).to_csv(
        batch / "strategy_chemistry_trades.csv", index=False
    )


def test_aggregate_deduplicates_symbols_by_earliest_numeric_batch(tmp_path, monkeypatch):
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "outputs"
    input_dir.mkdir()

    _write_batch(
        input_dir,
        120,
        ["KASUSDT", "LATERUSDT"],
        [
            ["KASUSDT", "combo", "15m", "IN_SAMPLE"],
            ["LATERUSDT", "combo", "15m", "IN_SAMPLE"],
        ],
    )
    _write_batch(
        input_dir,
        0,
        ["KASUSDT", "FIRSTUSDT"],
        [
            ["KASUSDT", "combo", "15m", "IN_SAMPLE"],
            ["FIRSTUSDT", "combo", "15m", "IN_SAMPLE"],
        ],
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "aggregate_strategy_chemistry",
            "--input-dir", str(input_dir),
            "--output-dir", str(output_dir),
            "--source-run-id", "test-run",
        ],
    )
    main()

    summary = json.loads(
        (output_dir / "strategy_chemistry_summary.json").read_text(encoding="utf-8")
    )
    assert set(summary) == {"KASUSDT", "FIRSTUSDT", "LATERUSDT"}

    duplicates = pd.read_csv(output_dir / "strategy_chemistry_duplicate_symbols.csv")
    assert duplicates.to_dict("records") == [
        {
            "symbol": "KASUSDT",
            "kept_offset": 0,
            "skipped_offset": 120,
            "kept_batch": "at-trendhive-chemistry-batch-0",
            "skipped_batch": "at-trendhive-chemistry-batch-120",
        }
    ]

    trades = pd.read_csv(output_dir / "strategy_chemistry_trades.csv")
    assert len(trades[trades["symbol"] == "KASUSDT"]) == 1
    assert set(trades["symbol"]) == {"KASUSDT", "FIRSTUSDT", "LATERUSDT"}
