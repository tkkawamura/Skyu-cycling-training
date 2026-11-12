"""Daily nutrition targets and meal split for the S級コーチ app.

All numbers are computed here so the review model only transcribes the tables.
"""
from __future__ import annotations

import math
from typing import Any

# --- Constants (edit here to change the rules) -----------------------------
LIFE_KCAL = {"デスクワーク": 1750, "外出": 1950}
DEFICIT_TRAIN = 150  # ride day (2026-11-12 UCI-based update: keep EA >= 30 kcal/kg FFM)
DEFICIT_REST_BEFORE_HARD = 0  # rest day when tomorrow is high/mid intensity
DEFICIT_REST_OTHER = 150  # rest day otherwise
PROTEIN_G = 130
FAT_G = {"高強度": 75, "中強度": 65, "低強度": 55, "レスト": 45}  # about 20%+ of energy
C_FLOOR_G = 250  # lower bound of carbs on low-intensity and rest days
C_FLOOR_OUTING_BONUS_G = 50
KJ_HIGH = 1200
KJ_MID = 800
CARB_SPLIT = {  # breakfast / lunch / dinner, decided by tomorrow's plan
    "高強度": (0.30, 0.30, 0.40),
    "中強度": (0.35, 0.30, 0.35),
    "低強度": (0.40, 0.30, 0.30),
}
FAT_BREAKFAST_SHARE = 0.25

# App option names -> intensity categories
FUELING_CATEGORY = {
    "高強度": "高強度",
    "2時間のZ2またはZ2上側": "中強度",
    "1時間半以内低強度": "低強度",
}
TOMORROW_CATEGORY = {
    "高強度": "高強度",
    "2時間のZ2またはZ2上側": "中強度",
    "低強度もしくはレスト": "低強度",
}


def round_half_up(value: float, unit: int = 1) -> int:
    return int(math.floor(value / unit + 0.5)) * unit


def classify_today(ride_kj: float, fueling_option: str | None) -> str:
    if ride_kj <= 0:
        return "レスト"
    if fueling_option in FUELING_CATEGORY:
        return FUELING_CATEGORY[fueling_option]
    if ride_kj >= KJ_HIGH:
        return "高強度"
    if ride_kj >= KJ_MID:
        return "中強度"
    return "低強度"


def compute_targets(day_type: str, ride_kj: float, today_category: str, tomorrow_category: str | None) -> dict[str, Any]:
    life = LIFE_KCAL.get(day_type, LIFE_KCAL["デスクワーク"])
    if today_category == "レスト":
        deficit = DEFICIT_REST_BEFORE_HARD if tomorrow_category in ("高強度", "中強度") else DEFICIT_REST_OTHER
    else:
        deficit = DEFICIT_TRAIN
    raw_kcal = life + ride_kj - deficit
    protein = PROTEIN_G
    fat = FAT_G[today_category]
    carbs = round_half_up((raw_kcal - protein * 4 - fat * 9) / 4)
    floor_applied = False
    if today_category in ("低強度", "レスト"):
        floor = C_FLOOR_G + (C_FLOOR_OUTING_BONUS_G if day_type == "外出" else 0)
        if carbs < floor:
            carbs = floor
            floor_applied = True
    kcal = protein * 4 + fat * 9 + carbs * 4
    return {
        "life_kcal": life,
        "ride_kj": round_half_up(ride_kj),
        "deficit": deficit,
        "p": protein,
        "f": fat,
        "c": carbs,
        "kcal": kcal,
        "c_floor_applied": floor_applied,
    }


def split_meals(remaining: dict[str, int], tomorrow_category: str | None) -> dict[str, Any]:
    split_key = tomorrow_category if tomorrow_category in CARB_SPLIT else "中強度"
    c_share = CARB_SPLIT[split_key]

    p_b = round_half_up(remaining["p"] / 3, 5)
    p_l = p_b
    p_d = remaining["p"] - p_b - p_l

    f_b = round_half_up(remaining["f"] * FAT_BREAKFAST_SHARE, 5)
    f_l = round_half_up((remaining["f"] - f_b) / 2, 5)
    f_d = remaining["f"] - f_b - f_l

    c_b = round_half_up(remaining["c"] * c_share[0], 5)
    c_l = round_half_up(remaining["c"] * c_share[1], 5)
    c_d = remaining["c"] - c_b - c_l

    k_b = round_half_up(p_b * 4 + f_b * 9 + c_b * 4, 10)
    k_l = round_half_up(p_l * 4 + f_l * 9 + c_l * 4, 10)
    k_d = remaining["kcal"] - k_b - k_l

    meals = {
        "朝（ライド後）": {"p": p_b, "f": f_b, "c": c_b, "kcal": k_b},
        "昼": {"p": p_l, "f": f_l, "c": c_l, "kcal": k_l},
        "夕": {"p": p_d, "f": f_d, "c": c_d, "kcal": k_d},
    }
    return {"split_key": split_key, "carb_share": c_share, "meals": meals}


def compute_day_plan(
    day_type: str,
    ride_kj: float,
    fueling_option: str | None,
    supply: dict[str, float] | None,
    tomorrow_option: str | None,
) -> dict[str, Any]:
    """supply is the confirmed fueling (kcal, p, f, c) or None when it comes from a photo."""
    today_category = classify_today(ride_kj, fueling_option)
    tomorrow_category = TOMORROW_CATEGORY.get(tomorrow_option or "")
    targets = compute_targets(day_type, ride_kj, today_category, tomorrow_category)
    plan: dict[str, Any] = {
        "today_category": today_category,
        "tomorrow_category": tomorrow_category,
        "targets": targets,
        "supply": supply,
        "warnings": [],
    }
    split_key = tomorrow_category if tomorrow_category in CARB_SPLIT else "中強度"
    plan["carb_share"] = CARB_SPLIT[split_key]
    if supply is None:
        return plan

    remaining = {
        "p": round_half_up(targets["p"] - supply["p"]),
        "f": round_half_up(targets["f"] - supply["f"]),
        "c": round_half_up(targets["c"] - supply["c"]),
        "kcal": round_half_up(targets["kcal"] - supply["kcal"]),
    }
    for key, value in remaining.items():
        if value < 0:
            remaining[key] = 0
            if "⚠補給が目標を超過" not in plan["warnings"]:
                plan["warnings"].append("⚠補給が目標を超過")
    plan["remaining"] = remaining
    plan.update(split_meals(remaining, tomorrow_category))
    return plan
