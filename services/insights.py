"""Extra report blocks: same-menu comparison, Fuji Hill time estimate, chart data."""
from __future__ import annotations

import math
import re
from typing import Any

# Mt. Fuji Hill Climb (Fuji Subaru Line): 24km, 1,255m climb.
FUJI_DISTANCE_M = 24000
FUJI_CLIMB_M = 1255
BIKE_KIT_KG = 8.0
CRR = 0.0045
CDA = 0.35
RHO = 1.05
DRIVETRAIN = 0.975
RACE_FTP_RATIO = 0.95  # ~70-minute all-out effort

GENERIC_NAME = re.compile(
    r"^(morning|afternoon|evening|night|lunch)?\s*(virtual\s*)?ride$|バーチャルライド$|ライド$|^ride$|^zwift$",
    re.IGNORECASE,
)


# --- Fuji Hill ---------------------------------------------------------------
def _power_for_speed(v: float, rider_kg: float) -> float:
    g = 9.81
    theta = math.atan(FUJI_CLIMB_M / FUJI_DISTANCE_M)
    mass = rider_kg + BIKE_KIT_KG
    return (mass * g * (math.sin(theta) + CRR * math.cos(theta)) * v + 0.5 * CDA * RHO * v**3) / DRIVETRAIN


def fuji_minutes(power_w: float, rider_kg: float) -> float:
    lo, hi = 0.3, 15.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _power_for_speed(mid, rider_kg) < power_w else (lo, mid)
    return FUJI_DISTANCE_M / ((lo + hi) / 2) / 60


def fuji_power_for(minutes: float, rider_kg: float) -> float:
    return _power_for_speed(FUJI_DISTANCE_M / (minutes * 60), rider_kg)


def markdown_fuji(ftp: float | None, weight: float | None, targets_min: tuple[int, ...], final_kg: float) -> list[str]:
    if not ftp or not weight:
        return []
    race_w = ftp * RACE_FTP_RATIO
    lines = [
        "### 富士ヒル予想タイム（ツール計算・物理モデルの目安）",
        f"- 今のFTP{ftp:.0f}W・体重{weight:.1f}kgでの予想：**{fuji_minutes(race_w, weight):.1f}分**（平均{race_w:.0f}W＝FTPの{RACE_FTP_RATIO:.0%}で一定ペース）",
    ]
    for target in targets_min:
        need_now = fuji_power_for(target, weight)
        need_final = fuji_power_for(target, final_kg)
        lines.append(
            f"- {target}分に必要なFTP：今の体重なら{need_now / RACE_FTP_RATIO:.0f}W ／ {final_kg:g}kgなら{need_final / RACE_FTP_RATIO:.0f}W"
        )
    lines.append("- 前提：24km・獲得1,255m、機材と装備8kg、単独・無風。集団走行や当日の条件で数分変わります。")
    return lines


# --- Same-menu comparison ------------------------------------------------------
def _is_ride(item: dict[str, Any]) -> bool:
    kind = str(item.get("type") or "").lower()
    return "ride" in kind or "cycling" in kind


def _name_key(name: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    if not text or GENERIC_NAME.search(text):
        return None
    return text.lower()


def _ef(item: dict[str, Any]) -> float | None:
    if item.get("efficiency_factor"):
        return float(item["efficiency_factor"])
    np_w, hr = item.get("weighted_average_watts"), item.get("average_heartrate")
    return float(np_w) / float(hr) if np_w and hr else None


def find_previous_same_menu(latest: dict[str, Any] | None, activities: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    if not latest or not _is_ride(latest):
        return None, ""
    latest_id = str(latest.get("id"))
    latest_date = str(latest.get("date") or "")
    older = [
        item
        for item in activities
        if _is_ride(item) and str(item.get("id")) != latest_id and str(item.get("date") or "") < latest_date
    ]
    older.sort(key=lambda item: str(item.get("start_time") or item.get("date") or ""), reverse=True)
    key = _name_key(latest.get("name"))
    if key:
        for item in older:
            if _name_key(item.get("name")) == key:
                return item, "同じ名前のメニュー"
    np_w = latest.get("weighted_average_watts")
    dur = latest.get("moving_time")
    if not np_w or not dur:
        return None, ""
    for item in older:
        other_np, other_dur = item.get("weighted_average_watts"), item.get("moving_time")
        if not other_np or not other_dur:
            continue
        if abs(other_np - np_w) / np_w <= 0.07 and abs(other_dur - dur) / dur <= 0.25:
            return item, "強度（NP±7%）と時間（±25%）が近いライド"
    return None, ""


def markdown_same_menu(latest: dict[str, Any] | None, activities: list[dict[str, Any]]) -> list[str]:
    previous, reason = find_previous_same_menu(latest, activities)
    if not previous:
        return []

    def fmt(item: dict[str, Any]) -> list[str]:
        ef = _ef(item)
        dec = item.get("decoupling")
        return [
            str(item.get("date") or "-"),
            f"{(item.get('moving_time') or 0) / 60:.0f}分",
            f"{item['weighted_average_watts']:.0f}W" if item.get("weighted_average_watts") else "-",
            f"{item['average_heartrate']:.0f}" if item.get("average_heartrate") else "-",
            f"{ef:.2f}" if ef else "-",
            f"{dec:+.1f}%" if dec is not None else "-",
        ]

    now, before = fmt(latest), fmt(previous)
    labels = ["日付", "時間", "NP", "平均HR", "EF", "Dec"]
    lines = [
        f"## 前回の同じメニューとの比較（{reason}：{previous.get('name') or '-'}）",
        "|項目|前回|今回|",
        "|---|---:|---:|",
    ]
    lines += [f"|{label}|{b}|{n}|" for label, b, n in zip(labels, before, now)]
    lines.append("- EFは高いほど、Dec（デカップリング）は低いほど有酸素の質が良い。")
    return lines


# --- Chart data for Claude ---------------------------------------------------
def markdown_chart_data(chart: dict[str, Any] | None) -> list[str]:
    points = (chart or {}).get("points") or []
    if not points:
        return []
    minutes: dict[int, dict[str, list[float]]] = {}
    for point in points:
        bucket = minutes.setdefault(int(point["min"]), {"power": [], "hr": [], "wbal": []})
        for key in ("power", "hr", "wbal"):
            if point.get(key) is not None:
                bucket[key].append(point[key])
    has_wbal = bool(chart.get("has_wbal"))
    header = "分,W,bpm" + (",Wbal%" if has_wbal else "")
    rows = [header]
    for minute in sorted(minutes):
        b = minutes[minute]
        row = [
            str(minute),
            f"{sum(b['power']) / len(b['power']):.0f}" if b["power"] else "",
            f"{sum(b['hr']) / len(b['hr']):.0f}" if b["hr"] else "",
        ]
        if has_wbal:
            row.append(f"{min(b['wbal']):.0f}" if b["wbal"] else "")
        rows.append(",".join(row))
    segs = "、".join(
        f"{seg['id']} {seg['start']:.0f}〜{seg['end']:.0f}分" for seg in (chart.get("segments") or []) if seg.get("id")
    )
    return [
        "## グラフ用データ（FIT・1分平均）",
        f"- 主要区間：{segs or '-'}",
        "```csv",
        *rows,
        "```",
    ]
