"""Decide today's weight-goal phase and whether to eat at maintenance.

Inputs come from the Intervals.icu wellness trend (daily rows with date,
weight, resting_heart_rate, hrv, sleep_score, form).
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import mean
from typing import Any

# --- Constants (edit here to change the rules) -----------------------------
# Final goal: same BMI as 175cm/60kg (19.6) at 167cm -> about 54.6kg.
GOAL_STAGES_KG = [60.0, 57.0, 54.6]
MAINTENANCE_DAYS = 14  # hold weight this long after reaching each stage
REBOUND_MARGIN_KG = 0.5  # stay in the reached stage until the average rebounds this far above it
WEIGHT_AVG_DAYS = 7
BASELINE_DAYS = 28
RHR_RISE_BPM = 5  # resting HR this much above baseline -> maintain
HRV_DROP_RATIO = 0.15  # HRV this much below baseline -> maintain
SLEEP_SCORE_MIN = 60
FORM_MIN = -25
MAINTAIN_TOMORROW_OPTIONS = {"FTPテスト・レース"}


def _parse(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _rows(trend: list[dict[str, Any]] | None, today: date) -> list[dict[str, Any]]:
    rows = []
    for row in trend or []:
        row_date = _parse(row.get("date"))
        if row_date and row_date <= today:
            rows.append({**row, "_date": row_date})
    return sorted(rows, key=lambda item: item["_date"])


def _avg(values: list[float]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return mean(values) if values else None


def rolling_weight(rows: list[dict[str, Any]], on: date) -> float | None:
    start = on - timedelta(days=WEIGHT_AVG_DAYS - 1)
    return _avg([row.get("weight") for row in rows if start <= row["_date"] <= on])


def stage_reached_on(rows: list[dict[str, Any]], days: list[date], target: float) -> date | None:
    """First day of the current spell at/below target. A rebound above target+margin resets it."""
    reached = None
    for day in days:
        avg = rolling_weight(rows, day)
        if avg is None:
            continue
        if avg > target + REBOUND_MARGIN_KG:
            reached = None
        elif avg <= target and reached is None:
            reached = day
    return reached


def weight_phase(trend: list[dict[str, Any]] | None, today: date, current_weight: float | None = None) -> dict[str, Any]:
    rows = _rows(trend, today)
    weight = rolling_weight(rows, today)
    if weight is None:
        weight = current_weight
    total = len(GOAL_STAGES_KG)
    if weight is None:
        return {"status": "unknown", "stage": 1, "total": total, "target": GOAL_STAGES_KG[0], "final": GOAL_STAGES_KG[-1], "weight_avg": None, "maintain": False}

    days = sorted({row["_date"] for row in rows} | {today})
    for index, target in enumerate(GOAL_STAGES_KG):
        stage = index + 1
        reached = stage_reached_on(rows, days, target)
        if reached is None and weight <= target:
            reached = today
        if reached is None:
            return {"status": "cutting", "stage": stage, "total": total, "target": target, "final": GOAL_STAGES_KG[-1], "weight_avg": weight, "maintain": False}
        held = (today - reached).days + 1
        if index == total - 1:
            return {"status": "final_maintain", "stage": stage, "total": total, "target": target, "final": target, "weight_avg": weight, "maintain": True, "reached": reached.isoformat()}
        if held <= MAINTENANCE_DAYS:
            return {
                "status": "stage_maintain",
                "stage": stage,
                "total": total,
                "target": target,
                "next_target": GOAL_STAGES_KG[index + 1],
                "final": GOAL_STAGES_KG[-1],
                "weight_avg": weight,
                "maintain": True,
                "reached": reached.isoformat(),
                "held_days": held,
            }
    return {"status": "final_maintain", "stage": total, "total": total, "target": GOAL_STAGES_KG[-1], "final": GOAL_STAGES_KG[-1], "weight_avg": weight, "maintain": True}


def condition_baselines(trend: list[dict[str, Any]] | None, today: date) -> dict[str, Any]:
    """28-day averages (excluding today) used for the fatigue checks."""
    rows = _rows(trend, today)
    base_start = today - timedelta(days=BASELINE_DAYS)
    base_rows = [row for row in rows if base_start <= row["_date"] < today]

    def summary(key: str) -> dict[str, Any]:
        values = [float(row[key]) for row in base_rows if row.get(key) is not None]
        return {"avg": mean(values) if values else None, "days": len(values)}

    return {
        "resting_heart_rate": summary("resting_heart_rate"),
        "hrv": summary("hrv"),
        "sleep_score": summary("sleep_score"),
    }


def fatigue_signals(trend: list[dict[str, Any]] | None, today: date) -> list[str]:
    rows = _rows(trend, today)
    today_row = next((row for row in reversed(rows) if row["_date"] == today), None)
    if not today_row:
        return []
    base_start = today - timedelta(days=BASELINE_DAYS)
    base_rows = [row for row in rows if base_start <= row["_date"] < today]
    reasons = []
    rhr = today_row.get("resting_heart_rate")
    rhr_base = _avg([row.get("resting_heart_rate") for row in base_rows])
    if rhr is not None and rhr_base is not None and rhr >= rhr_base + RHR_RISE_BPM:
        reasons.append(f"安静時心拍の上昇（{rhr:.0f}bpm／基準{rhr_base:.0f}bpm）")
    hrv = today_row.get("hrv")
    hrv_base = _avg([row.get("hrv") for row in base_rows])
    if hrv is not None and hrv_base and hrv <= hrv_base * (1 - HRV_DROP_RATIO):
        reasons.append(f"HRVの低下（{hrv:.0f}ms／基準{hrv_base:.0f}ms）")
    sleep = today_row.get("sleep_score")
    if sleep is not None and sleep < SLEEP_SCORE_MIN:
        reasons.append(f"睡眠スコアの低下（{sleep:.0f}）")
    form = today_row.get("form")
    if form is not None and form <= FORM_MIN:
        reasons.append(f"疲労の蓄積（Form {form:.1f}）")
    return reasons


def decide_mode(
    trend: list[dict[str, Any]] | None,
    today: date,
    tomorrow_option: str | None,
    current_weight: float | None = None,
    manual_maintain: bool = False,
) -> dict[str, Any]:
    phase = weight_phase(trend, today, current_weight)
    reasons: list[str] = []
    if manual_maintain:
        reasons.append("本人が「今日は維持」を選択（体調のサイン）")
    if tomorrow_option in MAINTAIN_TOMORROW_OPTIONS:
        reasons.append("明日がFTPテスト・レース")
    reasons += fatigue_signals(trend, today)
    if phase.get("status") == "stage_maintain":
        reasons.append(f"{phase['target']:.1f}kg到達後の維持期間（{phase['held_days']}/{MAINTENANCE_DAYS}日目）")
    elif phase.get("status") == "final_maintain":
        reasons.append(f"最終目標{phase['final']:.1f}kgに到達")
    return {"phase": phase, "maintain": bool(reasons), "reasons": reasons}
