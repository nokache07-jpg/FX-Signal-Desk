
"""Live scanner with automatic signal outcome tracking."""
from __future__ import annotations

import json
import os

import pandas as pd

from . import DEFAULT_SYMBOLS
from .analysis import analyze, format_text
from .config import load as load_config
from .data import load_frames
from .modes import MODES
from .news import fetch_news, upcoming_high
from .notify import send
from .tracker import load_state, record_signal, save_state, update_open_signals
from .workflow import evaluate

BAR_MIN = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
GRADE_RANK = {"A": 3, "B": 2, "C": 1, "D": 0}


def closed_only(df: pd.DataFrame, iv: str, now: pd.Timestamp) -> pd.DataFrame:
    """Drop the still-forming candle."""
    if df.index[-1] + pd.Timedelta(minutes=BAR_MIN[iv]) > now:
        return df.iloc[:-1]
    return df


def _record_workflow_signal(state: dict, result: dict, cfg: dict) -> bool:
    """Save a final workflow signal using its actual execution timeframe."""
    sg = result.get("signal")
    if not sg:
        return False

    timeframe = cfg["workflow"]["execute"]
    minutes = BAR_MIN.get(timeframe, 5)
    signal_time = pd.Timestamp(sg["timestamp"]) + pd.Timedelta(minutes=minutes)
    tp1, tp2 = sg["take_profit"]

    return record_signal(
        state,
        symbol=sg["instrument"],
        side=sg["direction"],
        strategy=sg["strategy"],
        timeframe=timeframe,
        entry=sg["entry"],
        sl=sg["stop_loss"],
        tp1=tp1,
        tp2=tp2,
        timestamp=signal_time.isoformat(),
        entry_type=sg.get("entry_type", "market"),
    )


def _record_analysis_signal(state: dict, result: dict) -> bool:
    """Save a signal produced by the scalp, swing, or daily analysis mode."""
    sg = result.get("signal")
    if not sg:
        return False

    timeframe = result["interval"]
    minutes = BAR_MIN.get(timeframe, 15)
    signal_time = pd.Timestamp(result["time"]) + pd.Timedelta(minutes=minutes)

    return record_signal(
        state,
        symbol=result["symbol"],
        side=sg["side"],
        strategy=sg["strategy"],
        timeframe=timeframe,
        entry=sg["entry"],
        sl=sg["sl"],
        tp1=sg["tp1"],
        tp2=sg["tp2"],
        timestamp=signal_time.isoformat(),
        entry_type=sg.get("entry_type", "market"),
    )


def scan(
    symbols=None,
    modes=None,
    notify=True,
    min_grade="B",
    state_path=".state/seen.json",
) -> list[dict]:
    symbols = symbols or DEFAULT_SYMBOLS
    modes = modes or list(MODES)

    seen = set()
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                seen = set(json.load(f))
        except (OSError, ValueError):
            print("[scan] Could not read seen-state file; starting with an empty set.")

    now = pd.Timestamp.now(tz="UTC")
    fresh = []

    cfg = load_config()
    wf = cfg["workflow"]

    # Load the tracker and market candles.
    tracker_state = load_state()

    need = {
        MODES[m]["interval"]
        for m in modes
    } | {
        MODES[m]["htf"]
        for m in modes
        if MODES[m]["htf"]
    }

    if "scalp" in modes:
        need |= {wf["plan"], wf["setup"], wf["execute"]}
        if wf["use_precision"]:
            need.add(wf["precision"])

    frames = load_frames(symbols, sorted(need))

    # Update previously recorded signals before adding new ones.
    if update_open_signals(tracker_state, frames):
        print("[tracker] Updated outcomes for existing signals.")

    news = fetch_news()
    events = news["events"] if news.get("ok") else []

    for sym in symbols:
        for mode in modes:
            m = MODES[mode]

            if mode == "scalp":
                try:
                    fr = {
                        tf: closed_only(d, tf, now)
                        for tf, d in frames.get(sym, {}).items()
                        if d is not None and not d.empty
                    }
                    result = evaluate(sym, fr, cfg, events, now)
                except Exception as e:
                    print(f"[scan] {sym} workflow: {e}")
                    continue

                sg = result.get("signal")
                if not sg:
                    continue

                key = (
                    f"{sym}|workflow|{sg['strategy_key']}|"
                    f"{sg['direction']}|{sg['entry_zone']}"
                )
                if key in seen:
                    continue

                seen.add(key)
                fresh.append(result)

                if _record_workflow_signal(tracker_state, result, cfg):
                    print(f"[tracker] Recorded {sym} {sg['direction']} workflow signal.")

                message = format_workflow(result)
                (send if notify else print)(message)
                continue

            try:
                df = frames.get(sym, {}).get(m["interval"])
                if df is None or df.empty:
                    continue

                df = closed_only(df, m["interval"], now)

                htf = None
                if m["htf"]:
                    htf = frames.get(sym, {}).get(m["htf"])
                    if htf is not None and not htf.empty:
                        htf = closed_only(htf, m["htf"], now)

                result = analyze(sym, mode, df, htf, m["htf_name"])

            except Exception as e:
                print(f"[scan] {sym} {mode}: {e}")
                continue

            sg = result.get("signal")
            if not sg or GRADE_RANK[sg["grade"]] < GRADE_RANK[min_grade]:
                continue

            key = (
                f"{sym}|{mode}|{sg['strategy']}|"
                f"{sg['side']}|{sg['time']}"
            )
            if key in seen:
                continue

            seen.add(key)
            fresh.append(result)

            if _record_analysis_signal(tracker_state, result):
                print(f"[tracker] Recorded {sym} {sg['side']} {mode} signal.")

            message = format_text(result)
            warning = upcoming_high(events, sym, now)
            if warning:
                message += "\n" + warning

            (send if notify else print)(message)

    # Save both deduplication state and tracker state.
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-500:], f)

    save_state(tracker_state)

    if not fresh:
        print(
            f"[scan] {now:%Y-%m-%d %H:%M} UTC - "
            f"checked {len(symbols)} pairs, no new graded setups"
        )

    print(
        f"[tracker] Total recorded signals: "
        f"{len(tracker_state['signals'])}"
    )

    return fresh


def format_workflow(r: dict) -> str:
    """Telegram card for a final BUY / SELL from the multi-timeframe workflow."""
    from .alerts import from_workflow
    return from_workflow(r)
