
"""Automatic signal logging and outcome tracking for FX Signal Desk."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EAT = ZoneInfo("Africa/Addis_Ababa")


def load_state(path=".state/trades.json"):
    if not os.path.exists(path):
        return {"signals": [], "last_report_date": None}

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("signals", [])
        data.setdefault("last_report_date", None)
        return data
    except (OSError, ValueError):
        print("[tracker] Could not read state file.")
        return {"signals": [], "last_report_date": None}


def save_state(state, path=".state/trades.json"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = path + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    os.replace(temp, path)


def record_signal(
    state,
    *,
    symbol,
    side,
    strategy,
    timeframe,
    entry,
    sl,
    tp1,
    tp2,
    timestamp,
    entry_type="market",
):
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return False

    order_type = (entry_type or "market").lower()
    signal_id = "|".join(
        (symbol, side, strategy, timeframe, str(timestamp))
    )

    if any(x["id"] == signal_id for x in state["signals"]):
        return False

    state["signals"].append({
        "id": signal_id,
        "symbol": symbol,
        "side": side,
        "strategy": strategy,
        "timeframe": timeframe,
        "entry": float(entry),
        "sl": float(sl),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "signal_time": str(timestamp),
        "last_checked": str(timestamp),
        "entry_type": order_type,
        "status": (
            "PENDING"
            if order_type in ("limit", "stop")
            else "OPEN"
        ),
        "outcome_time": None,
    })

    return True


def _as_utc(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def update_open_signals(state, frames):
    """Update pending and open signals using subsequent candles.

    Conservative rule: if SL and TP1 are both touched in one candle,
    count the result as a loss.
    """
    changed = False

    for trade in state["signals"]:
        if trade["status"] not in ("OPEN", "PENDING"):
            continue

        symbol_frames = frames.get(trade["symbol"], {})
        df = symbol_frames.get(trade["timeframe"])

        if df is None or df.empty:
            continue

        last_checked = _as_utc(trade["last_checked"])
        later = df[df.index > last_checked]

        if later.empty:
            continue

        side = trade["side"]
        entry = float(trade["entry"])
        sl = float(trade["sl"])
        tp = float(trade["tp1"])
        order_type = trade["entry_type"]

        if trade["status"] == "PENDING":
            for bar_time, bar in later.iterrows():
                high = float(bar["high"])
                low = float(bar["low"])

                if order_type == "limit":
                    filled = (
                        low <= entry if side == "BUY"
                        else high >= entry
                    )
                else:
                    filled = (
                        high >= entry if side == "BUY"
                        else low <= entry
                    )

                trade["last_checked"] = bar_time.isoformat()
                changed = True

                if filled:
                    trade["status"] = "OPEN"
                    last_checked = _as_utc(bar_time)
                    break

            if trade["status"] == "PENDING":
                continue

            # Include the entry candle when checking its outcome.
            later = df[df.index >= last_checked]

        for bar_time, bar in later.iterrows():
            high = float(bar["high"])
            low = float(bar["low"])

            if side == "BUY":
                stop_hit = low <= sl
                target_hit = high >= tp
            else:
                stop_hit = high >= sl
                target_hit = low <= tp

            trade["last_checked"] = bar_time.isoformat()
            changed = True

            if stop_hit:
                trade["status"] = "LOSS"
                trade["outcome_time"] = bar_time.isoformat()
                break

            if target_hit:
                trade["status"] = "WIN"
                trade["outcome_time"] = bar_time.isoformat()
                break

    return changed


def daily_report(state, day=None):
    """Summarize signals created on an Ethiopia calendar day."""
    day = day or datetime.now(EAT).date().isoformat()
    rows = []

    for trade in state["signals"]:
        try:
            created_day = (
                _as_utc(trade["signal_time"])
                .astimezone(EAT)
                .date()
                .isoformat()
            )
        except (ValueError, TypeError):
            continue

        if created_day == day:
            rows.append(trade)

    wins = sum(t["status"] == "WIN" for t in rows)
    losses = sum(t["status"] == "LOSS" for t in rows)
    opened = sum(t["status"] == "OPEN" for t in rows)
    pending = sum(t["status"] == "PENDING" for t in rows)

    closed = wins + losses
    win_rate = wins / closed * 100 if closed else 0.0

    return (
        f"📊 KFX DAILY SIGNAL REPORT — {day} (EAT)\n"
        f"Signals recorded: {len(rows)}\n"
        f"✅ Wins (TP1): {wins}\n"
        f"❌ Losses (SL): {losses}\n"
        f"⏳ Open: {opened}\n"
        f"📌 Pending: {pending}\n"
        f"Win rate of closed signals: {win_rate:.1f}%\n"
        "Results are based on candle data, not broker execution."
    )
