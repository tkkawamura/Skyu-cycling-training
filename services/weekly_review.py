"""Weekly weight / energy-balance review and the daily log written to Intervals.icu.

Every report generation writes one line to that day's wellness comment, e.g.
"S級コーチ: デスク / 明日=高強度 / 目標2,287kcal / 消費1,750kcal / 収支+537kcal".
On Mondays the app reads those lines back (days without a line are estimated
from the day's work kJ and the next day's ride) and compares the balance with
the 7-day average weight. Every other Monday it proposes a deficit change.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from services import nutrition
from services.goal_phase import rolling_weight

LOG_PREFIX = "S級コーチ:"
KCAL_PER_KG = 7200
JUDGE_ANCHOR = date(2026, 11, 30)  # a judgment Monday; judgments every 14 days from here
JUDGE_DAYS = 14
GAIN_KG = 0.2  # 14-day change at/above this -> deficit +100
FLAT_KG = 0.2  # |change| below this -> deficit +50
FAST_LOSS_KG = 1.0  # 14-day loss at/above this (0.5kg/week) -> deficit -50
MAINTAIN_DAYS_HOLD = 5  # this many maintenance days in the window -> no judgment
MIN_WEIGHT_DAYS = 4
ADJUST_KEYS = ("中強度", "低強度")
ADJUST_LABELS = {"中強度": "Z2の日", "低強度": "低強度の日"}  # weigh-ins needed in each 7-day window
SHORT_DAY = {"デスクワーク": "デスク", "外出": "外出"}
OPTION_BY_CATEGORY = {"高強度": "高強度", "中強度": "2時間のZ2またはZ2上側", "低強度": "低強度（90分以内）"}
BALANCE_RE = re.compile(r"収支([+\-−]?[\d,]+)kcal")


# --- Daily log ---------------------------------------------------------------
def daily_log_line(plan: dict[str, Any], day_type: str, tomorrow_option: str | None) -> str:
    t = plan["targets"]
    used = round((t.get("life_kcal") or 0) + (t.get("ride_kj") or 0))
    balance = round(t["kcal"] - used)
    parts = [
        f"{LOG_PREFIX} {SHORT_DAY.get(day_type, day_type)}",
        f"明日={tomorrow_option or '未選択'}",
        f"目標{t['kcal']:,}kcal",
        f"消費{used:,}kcal",
        f"収支{balance:+,d}kcal",
    ]
    if (plan.get("mode") or {}).get("maintain"):
        parts.append("維持")
    return " / ".join(parts)


def can_overwrite(existing: Any) -> bool:
    text = str(existing or "").strip()
    return not text or text.startswith(LOG_PREFIX)


def parse_log(comment: Any) -> dict[str, Any] | None:
    text = str(comment or "").strip()
    if not text.startswith(LOG_PREFIX):
        return None
    match = BALANCE_RE.search(text)
    if not match:
        return None
    balance = int(match.group(1).replace(",", "").replace("−", "-"))
    return {"balance": balance, "maintain": " / 維持" in text}


# --- Estimation for days without a log ---------------------------------------
def kj_by_date(activities: list[dict[str, Any]], body_mass_kg: float | None) -> dict[date, float]:
    totals: dict[date, float] = {}
    for item in activities or []:
        kind = str(item.get("type") or "").lower()
        if "ride" not in kind and "run" not in kind:
            continue
        try:
            day = date.fromisoformat(str(item.get("date"))[:10])
        except ValueError:
            continue
        kj = _num(item.get("work_kj"))
        if "run" in kind:
            calories = _num(item.get("calories"))
            distance_km = (_num(item.get("distance")) or 0) / 1000
            kj = calories or (distance_km * body_mass_kg if distance_km and body_mass_kg else None)
        elif kj is None:
            watts, secs = _num(item.get("average_watts")), _num(item.get("moving_time"))
            kj = watts * secs / 1000 if watts is not None and secs is not None else _num(item.get("calories"))
        if kj:
            totals[day] = totals.get(day, 0.0) + kj
    return totals


def estimate_balance(day: date, kj: dict[date, float], body_mass_kg: float | None) -> int:
    today_kj = kj.get(day, 0.0)
    tomorrow_category = nutrition.classify_today(kj.get(day + timedelta(days=1), 0.0), None)
    tomorrow_category = "低強度" if tomorrow_category == "レスト" else tomorrow_category
    plan = nutrition.compute_day_plan(
        "デスクワーク", today_kj, None, None, OPTION_BY_CATEGORY[tomorrow_category], False, body_mass_kg
    )
    t = plan["targets"]
    return round(t["kcal"] - t["life_kcal"] - today_kj)


# --- Review --------------------------------------------------------------------
def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rows(trend: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for row in trend or []:
        try:
            rows.append({**row, "_date": date.fromisoformat(str(row.get("date"))[:10])})
        except ValueError:
            continue
    return sorted(rows, key=lambda item: item["_date"])


def _weigh_ins(rows: list[dict[str, Any]], end: date) -> int:
    start = end - timedelta(days=6)
    return sum(1 for row in rows if start <= row["_date"] <= end and row.get("weight") is not None)


def is_judgment_week(today: date) -> bool:
    return ((today - JUDGE_ANCHOR).days // 7) % 2 == 0


def proposal(change_kg: float) -> tuple[str, str, dict[str, int]]:
    """Return (verdict, proposal text, new deficits).

    Only Z2 and low-intensity days move: rest days already sit on the carb floor,
    so a bigger rest-day deficit would change nothing, and hard days stay power-first.
    """
    current = dict(nutrition.DEFICIT_BY_DAY)
    if change_kg >= GAIN_KG:
        verdict, step = "増えている", 100
    elif abs(change_kg) < FLAT_KG:
        verdict, step = "横ばい", 50
    elif change_kg <= -FAST_LOSS_KG:
        verdict, step = "減るのが速すぎる（週0.5kg超）", -50
    else:
        return "順調に減っている", "据え置き", current
    new = dict(current)
    for key in ADJUST_KEYS:
        new[key] = max(0, current[key] + step)
    changes = "、".join(f"{ADJUST_LABELS[key]} {current[key]}→{new[key]}" for key in ADJUST_KEYS)
    sign = "増やす" if step > 0 else "減らす"
    return verdict, f"赤字を{abs(step)}kcal{sign}（{changes}）", new


def build_review(
    trend: list[dict[str, Any]] | None,
    activities: list[dict[str, Any]] | None,
    today: date,
    body_mass_kg: float | None,
    phase: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Weekly review shown on Mondays (or always with force=True)."""
    if today.weekday() != 0 and not force:
        return None
    judgment = is_judgment_week(today)
    span = JUDGE_DAYS if judgment else 7
    rows = _rows(trend)
    comments = {row["_date"]: row.get("comments") for row in rows}
    kj = kj_by_date(activities or [], body_mass_kg)

    days = [today - timedelta(days=offset) for offset in range(span, 0, -1)]
    logged = estimated = maintain_days = 0
    total = 0
    rest_surplus = rest_surplus_days = 0
    for day in days:
        log = parse_log(comments.get(day))
        if log:
            balance = log["balance"]
            logged += 1
            maintain_days += int(log["maintain"])
        else:
            balance = estimate_balance(day, kj, body_mass_kg)
            estimated += 1
        total += balance
        if balance > 0:
            rest_surplus += balance
            rest_surplus_days += 1

    end_avg = rolling_weight(rows, today)
    start_avg = rolling_weight(rows, today - timedelta(days=span))
    review: dict[str, Any] = {
        "judgment": judgment,
        "start": days[0],
        "end": days[-1],
        "span": span,
        "weight_start": start_avg,
        "weight_end": end_avg,
        "balance_total": total,
        "expected_kg": total / KCAL_PER_KG,
        "surplus_days": rest_surplus_days,
        "surplus_total": rest_surplus,
        "logged_days": logged,
        "estimated_days": estimated,
        "next_judgment": today + timedelta(days=7 if not judgment else JUDGE_DAYS),
        "phase": phase or {},
    }
    if end_avg is None or start_avg is None:
        review.update(verdict="体重データ不足", proposal="判定なし")
        return review
    change = end_avg - start_avg
    review["change_kg"] = change
    if not judgment:
        return review

    status = (phase or {}).get("status")
    if status in {"stage_maintain", "final_maintain"}:
        review.update(verdict="維持期間中", proposal="判定なし（据え置き）")
    elif _weigh_ins(rows, today) < MIN_WEIGHT_DAYS or _weigh_ins(rows, today - timedelta(days=span)) < MIN_WEIGHT_DAYS:
        review.update(verdict="体重の記録が少ない", proposal="判定なし（据え置き）")
    elif maintain_days >= MAINTAIN_DAYS_HOLD:
        review.update(verdict=f"維持の日が{maintain_days}日あった", proposal="判定なし（据え置き）")
    else:
        verdict, text, new = proposal(change)
        review.update(verdict=verdict, proposal=text, new_deficits=new)
    return review


def markdown_review(review: dict[str, Any] | None) -> list[str]:
    if not review:
        return []
    md = lambda d: f"{d.month}/{d.day}"  # noqa: E731
    title = "判定週" if review["judgment"] else "経過"
    lines = [f"## 📊 週次レビュー（{title}）{md(review['start'])}〜{md(review['end'])}"]
    ws, we = review.get("weight_start"), review.get("weight_end")
    weeks = review["span"] // 7
    if ws is not None and we is not None:
        lines.append(f"- 体重7日平均：{ws:.1f} → {we:.1f}kg（{we - ws:+.1f}kg / {weeks}週）")
    else:
        lines.append("- 体重7日平均：データ不足")
    lines.append(
        f"- 収支からの予測：{review['expected_kg']:+.1f}kg（実際の収支の合計 {review['balance_total']:+,d}kcal）"
    )
    if review["surplus_days"]:
        lines.append(f"- 食べ増しの日（主にレスト）：{review['surplus_days']}日・合計{review['surplus_total']:+,d}kcal")
    source = f"記録{review['logged_days']}日"
    if review["estimated_days"]:
        source += f"・推定{review['estimated_days']}日（ライドkJと翌日のライドから計算）"
    lines.append(f"- 収支の元データ：{source}")
    if review["judgment"]:
        lines.append(f"- **判定：{review.get('verdict')} → 提案：{review.get('proposal')}**")
    else:
        lines.append(f"- 次の判定：{md(review['next_judgment'])}")
    phase = review.get("phase") or {}
    if phase.get("status") == "cutting" and we is not None:
        lines.append(f"- 次の段階目標{phase['target']:g}kgまで：あと{we - phase['target']:.1f}kg")
    lines.append("")
    return lines
