from __future__ import annotations

import json
from typing import Any

from openai import OpenAI


class RideAnalyzer:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def assess(self, payload: dict[str, Any], rpe: int | None) -> dict[str, Any]:
        if not self.api_key:
            return self._fallback_assessment(payload, rpe)

        client = OpenAI(api_key=self.api_key)
        compact = {
            "metrics": payload.get("metrics", {}),
            "latest_ride": payload.get("latest_ride", {}),
            "trend": payload.get("trend", [])[-10:],
            "fit_activity_context": compact_fit_context(payload.get("fit_activity_context")),
            "rpe": rpe,
        }
        prompt = (
            "You are a cycling coach. Evaluate today's ride in Japanese. "
            "If fit_activity_context exists, use it as the most detailed source for the ride. "
            "Only use metrics marked available; do not infer missing/null/sample_count=0 fields. "
            "Return strict JSON with keys: headline, score, good, concern, next_action, tomorrow, note. "
            "Be practical and concise. Do not provide medical advice."
        )
        try:
            response = client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
                ],
                text={"format": {"type": "json_object"}},
            )
            return json.loads(response.output_text)
        except Exception as exc:
            fallback = self._fallback_assessment(payload, rpe)
            fallback["note"] = f"OpenAI evaluation failed; local fallback is shown: {exc}"
            return fallback

    def _fallback_assessment(self, payload: dict[str, Any], rpe: int | None) -> dict[str, Any]:
        metrics = payload.get("metrics", {})
        ride = payload.get("latest_ride") or {}
        fit_context = payload.get("fit_activity_context") or {}
        fit_summary = ((fit_context.get("activity") or {}).get("summary") or {})
        fit_load = ((fit_context.get("activity") or {}).get("load") or {})

        form = _to_float(metrics.get("form"))
        fatigue = _to_float(metrics.get("fatigue"))
        fitness = _to_float(metrics.get("fitness"))
        load = _to_float(ride.get("training_load") or ride.get("load") or fit_load.get("session_load_score"))

        concern = "RPE is not set, so subjective effort is not reflected yet."
        if rpe and rpe >= 8:
            concern = "RPE is high. Bias tomorrow toward recovery unless you feel unusually fresh."
        elif form is not None and form < -20:
            concern = "Form is quite low, suggesting accumulated fatigue."
        elif load and load > 100:
            concern = "Session load is high. Prioritize sleep, carbohydrate, and easy movement."

        score = 70
        if load:
            score += min(15, int(load / 10))
        if form is not None and form < -25:
            score -= 15
        if rpe and rpe >= 9:
            score -= 10
        score = max(30, min(95, score))

        power = fit_summary.get("mean_power_w") or ride.get("average_watts") or "-"
        wp = fit_load.get("weighted_power_w") or ride.get("weighted_average_watts") or "-"
        return {
            "headline": "Local ride review",
            "score": score,
            "good": f"Fitness {fitness if fitness is not None else '-'} / Fatigue {fatigue if fatigue is not None else '-'} / mean power {power} W / weighted power {wp} W.",
            "concern": concern,
            "next_action": "Set RPE after the ride to make the review more personal.",
            "tomorrow": "If legs are heavy, keep it Z1-Z2. If fresh, short tempo is the upper limit.",
            "note": "OPENAI_API_KEY is not set or evaluation failed, so this is a local fallback.",
        }


def compact_fit_context(context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not context:
        return None
    return {
        "schema_version": context.get("schema_version"),
        "llm_summary": context.get("llm_summary"),
        "athlete_inputs": context.get("athlete_inputs"),
        "activity": context.get("activity"),
        "physiology": context.get("physiology"),
        "coach_context": context.get("coach_context"),
    }


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
