"""Automatic signal logging and outcome tracking for the FX Signal Desk."""
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
        print("[tracker] Could not read state file; starting a fresh state.")
        return {"signals": [], "last_report_date": None}


def save_state(state, path=".state/trades.json"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(temp, path)


def record_signal(state, *, symbol, side, strategy, timeframe,
                  entry, sl, tp1, tp2, timestamp, entry_type="market"):
    """Store a signal once. Values are taken from the alert, not guessed."""
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return False

    stamp = str(timestamp)
    signal_id = "|".join((symbol, side, strategy, timeframe, stamp))
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
        "signal_time": stamp,
        "last_checked": stamp,
        "entry_type": (entry_type or "market").lower(),
        "status": "OPEN",
        "outcome_time": None,
    })
    return True


def _as_utc(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def update_open_signals(state, frames):
    """Check only candles after each signal. If TP1 and SL hit in one candle, SL wins."""
    changed = False

    for trade in state["signals"]:
        if trade["status"] != "OPEN":
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
        sl = float(trade["sl"])
        tp = float(trade["tp1"])

        for bar_time, bar in later.iterrows():
            high, low = float(bar["high"]), float(bar["low"])

            if side == "BUY":
                stop_hit = low <= sl
                target_hit = high >= tp
            else:
                stop_hit = high >= sl
                target_hit = low <= tp

            if stop_hit:
                trade["status"] = "LOSS"
                trade["outcome_time"] = bar_time.isoformat()
                changed = True
                break

            if target_hit:
                trade["status"] = "WIN"
                trade["outcome_time"] = bar_time.isoformat()
                changed = True
                break

            trade["last_checked"] = bar_time.isoformat()
            changed = True

    return changed


def daily_report(state, day=None):
    """Report signals created on an Ethiopia calendar day."""
    day = day or datetime.now(EAT).date().isoformat()
    rows = []
    for trade in state["signals"]:
        try:
            created_day = _as_utc(trade["signal_time"]).astimezone(EAT).date().isoformat()
        except (ValueError, TypeError):
            continue
        if created_day == day:
            rows.append(trade)

    wins = sum(t["status"] == "WIN" for t in rows)
    losses = sum(t["status"] == "LOSS" for t in rows)
    opened = sum(t["status"] == "OPEN" for t in rows)
    closed = wins + losses
    rate = (wins / closed * 100) if closed else 0.0

    return (
        f"📊 KFX DAILY SIGNAL REPORT — {day} (EAT)\n"
        f"Signals recorded: {len(rows)}\n"
        f"✅ Wins (TP1): {wins}\n"
        f"❌ Losses (SL): {losses}\n"
        f"⏳ Still open: {opened}\n"
        f"Win rate of closed signals: {rate:.1f}%\n"
        "Unfilled pending orders are not separately simulated in this first version."
    )
