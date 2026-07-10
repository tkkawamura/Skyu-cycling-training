from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from fitparse import FitFile


@dataclass
class AthleteInputs:
    critical_power_w: float | None = None
    body_mass_kg: float | None = None
    max_heart_rate_bpm: float | None = None
    w_prime_kj: float | None = None


class FitActivityContextBuilder:
    def __init__(self, athlete: AthleteInputs) -> None:
        self.athlete = athlete

    def build(self, fit_path: str | Path) -> dict[str, Any]:
        records, laps = self._read_fit(fit_path)
        if not records:
            raise ValueError("No record messages found in FIT file.")

        summary = self._summary(records)
        load = self._load(records, summary)
        auto_segments = self._auto_segments(records, load)
        user_laps = self._user_lap_segments(records, laps, load)
        interval_lap_comparison = self._interval_lap_comparison(auto_segments, user_laps)
        w_prime_series = self._w_prime_balance_series(records)
        duration_curves = self._duration_curves(records)
        distributions = self._distributions(records)
        metric_presence = self._metric_presence(records, auto_segments, user_laps, duration_curves)

        selected = [seg for seg in auto_segments if seg.get("selected")]
        llm_summary = {
            "session_summary": {
                "duration_s": summary["duration_s"],
                "distance_m": summary["distance_m"],
                "total_work_kj": summary["total_work_kj"],
                "mean_power_w": summary["mean_power_w"],
                "max_power_w": summary["max_power_w"],
                "mean_heart_rate_bpm": summary["mean_heart_rate_bpm"],
                "mean_cadence_rpm": summary["mean_cadence_rpm"],
                "weighted_power_w": load["weighted_power_w"],
                "intensity_ratio": load["intensity_ratio"],
                "session_load_score": load["session_load_score"],
            },
            "key_intervals": [self._llm_segment(seg) for seg in selected[:4]],
            "key_laps": [self._llm_lap(seg) for seg in user_laps[:4]],
            "key_intervals_pedaling_summary": [
                {
                    "segment_id": seg["segment_id"],
                    "has_pedaling_dynamics": seg["has_pedaling_dynamics"],
                    "estimated_crank_torque_mean_nm": seg["pedaling_dynamics"]["estimated_crank_torque"]["mean_nm"],
                }
                for seg in selected[:4]
            ],
            "metric_presence": metric_presence,
            "data_presence_matrix": self._data_presence_matrix(auto_segments, user_laps),
            "interval_lap_comparison": interval_lap_comparison,
            "available_metrics": {
                "activity_summary": {
                    "power": available(summary["mean_power_w"]),
                    "heart_rate": available(summary["mean_heart_rate_bpm"]),
                    "cadence": available(summary["mean_cadence_rpm"]),
                    "distance": available(summary["distance_m"]),
                },
                "duration_curves": {
                    "mode": "standard",
                    "power": available(duration_curves["power"]["representative_points"]),
                    "heart_rate": available(duration_curves["heart_rate"]["representative_points"]),
                    "cadence": available(duration_curves["cadence"]["representative_points"]),
                    "velocity": available(duration_curves["velocity"]["representative_points"]),
                },
                "segments": {
                    "auto_interval_count": len(auto_segments),
                    "user_lap_count": len(user_laps),
                    "auto_intervals_with_pedaling_dynamics": sum(1 for seg in auto_segments if seg["has_pedaling_dynamics"]),
                    "user_laps_with_pedaling_dynamics": sum(1 for seg in user_laps if seg["has_pedaling_dynamics"]),
                },
            },
        }

        return {
            "schema_version": "fit_activity_context.v2",
            "llm_summary": llm_summary,
            "meta": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "uploaded_fit_file",
                "scope": "single_activity",
                "privacy": {
                    "file_name_included": False,
                    "raw_records_included": False,
                    "location_fields_included": False,
                },
                "density": {
                    "profile": "ai_compact",
                    "duration_curve_mode": "standard",
                    "dense_1hz_series_included": False,
                    "detailed_duration_curves_included": False,
                    "duration_curve_detail": {
                        "mode": "standard",
                        "included": False,
                        "point_count": sum(len(v["representative_points"]) for v in duration_curves.values()),
                    },
                    "removed_series": [
                        "w_prime_balance.points",
                        "interval_detection.w_prime_balance_points",
                        "duration_curves.*.points",
                    ],
                },
            },
            "athlete_inputs": {
                "critical_power_w": self.athlete.critical_power_w,
                "body_mass_kg": self.athlete.body_mass_kg,
                "max_heart_rate_bpm": self.athlete.max_heart_rate_bpm,
            },
            "activity": {
                "summary": summary,
                "load": load,
                "data_quality": self._data_quality(records),
            },
            "physiology": self._physiology(records, w_prime_series),
            "signals": {
                "distributions": distributions,
                "duration_curves": duration_curves,
            },
            "segments": {
                "auto_interval_segments": auto_segments,
                "user_lap_segments": user_laps,
            },
            "coach_context": self._coach_context(load, auto_segments),
            "method_notes": [
                {"module": "stream_processing"},
                {"module": "duration_curves"},
                {"module": "distributions"},
                {
                    "module": "user_laps",
                    "basis": "fit_lap_messages",
                    "method": "fit_lap_bounds_on_elapsed_stream",
                },
                {
                    "module": "interval_detection",
                    "basis": "power_above_reference_power",
                    "method": "threshold_segments_with_short_gap_merge",
                },
                {
                    "module": "w_prime_balance",
                    "method": "critical_power_summary_only",
                    "notes": ["w_prime balance points are intentionally removed from compact context"],
                },
            ],
            "coach_prompt": {
                "language": "ja",
                "prompt": "このFIT解析JSONだけを根拠に、日本語でライドレビューを作成してください。availableの指標だけを評価し、missing/null/sample_count=0は推測で補わないでください。",
            },
        }

    def _read_fit(self, fit_path: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fit = FitFile(str(fit_path))
        records = []
        laps = []
        first_ts = None

        for message in fit.get_messages():
            if message.name not in {"record", "lap"}:
                continue
            row = {field.name: field.value for field in message}
            if message.name == "record":
                ts = row.get("timestamp")
                if ts and first_ts is None:
                    first_ts = ts
                row["elapsed_s"] = int((ts - first_ts).total_seconds()) if ts and first_ts else len(records)
                records.append(normalize_record(row))
            elif message.name == "lap":
                laps.append(normalize_lap(row, first_ts))
        return records, laps

    def _summary(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        powers = values(records, "power")
        hrs = values(records, "heart_rate")
        cadences = values(records, "cadence")
        speeds = values(records, "speed")
        distances = values(records, "distance")
        altitudes = values(records, "altitude")
        temperatures = values(records, "temperature")
        duration = int(max(row["elapsed_s"] for row in records) - min(row["elapsed_s"] for row in records) + 1)
        distance = max(distances) - min(distances) if len(distances) >= 2 else None
        ascent = total_ascent(altitudes)
        work_kj = sum(powers) / 1000 if powers else None
        return {
            "duration_s": duration,
            "distance_m": round_or_none(distance, 1),
            "total_work_kj": round_or_none(work_kj, 1),
            "total_ascent_m": round_or_none(ascent, 1),
            "mean_power_w": round_or_none(mean(powers), 1) if powers else None,
            "max_power_w": round_or_none(max(powers), 1) if powers else None,
            "mean_heart_rate_bpm": round_or_none(mean(hrs), 1) if hrs else None,
            "max_heart_rate_bpm": round_or_none(max(hrs), 1) if hrs else None,
            "mean_cadence_rpm": round_or_none(mean(cadences), 1) if cadences else None,
            "max_cadence_rpm": round_or_none(max(cadences), 1) if cadences else None,
            "mean_speed_mps": round_or_none(mean(speeds), 1) if speeds else None,
            "max_speed_mps": round_or_none(max(speeds), 1) if speeds else None,
            "mean_temperature_c": round_or_none(mean(temperatures), 1) if temperatures else None,
            "record_count": len(records),
        }

    def _load(self, records: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
        powers = values(records, "power")
        cp = self.athlete.critical_power_w
        wp = normalized_power(powers)
        best_20 = best_average(powers, 1200)
        intensity = wp / cp if wp and cp else None
        load = ((summary["duration_s"] * wp * (intensity or 0)) / (cp * 3600) * 100) if wp and cp else None
        return {
            "weighted_power_w": round_or_none(wp, 0),
            "intensity_ratio": round_or_none(intensity, 3),
            "session_load_score": round_or_none(load, 1),
            "best_20min_power_w": round_or_none(best_20, 0),
            "best_20min_power_to_mass_wkg": round_or_none(best_20 / self.athlete.body_mass_kg, 2) if best_20 and self.athlete.body_mass_kg else None,
            "critical_power_w": cp,
            "reference_power": {"model": "critical_power", "critical_power_w": cp},
            "notes": [] if cp else ["critical_power_w not provided; load metrics are limited"],
        }

    def _auto_segments(self, records: list[dict[str, Any]], load: dict[str, Any]) -> list[dict[str, Any]]:
        cp = self.athlete.critical_power_w or load.get("weighted_power_w") or 0
        powers = values(records, "power")
        if not powers or not cp:
            return []
        threshold = cp * 0.88
        ranges = []
        start = None
        last = None
        for idx, row in enumerate(records):
            power = row.get("power")
            if power is not None and power >= threshold:
                if start is None:
                    start = idx
                last = idx
            elif start is not None:
                if last is not None and records[last]["elapsed_s"] - records[start]["elapsed_s"] >= 45:
                    ranges.append((start, last))
                start = None
                last = None
        if start is not None and last is not None and records[last]["elapsed_s"] - records[start]["elapsed_s"] >= 45:
            ranges.append((start, last))
        ranges = merge_short_gaps(ranges, records, max_gap_s=20)
        segments = [self._segment(records, start, end, f"seg_{i + 1:02d}", "auto_interval_detection", cp) for i, (start, end) in enumerate(ranges[:12])]
        segments.sort(key=lambda seg: seg["power"]["load_score"] or 0, reverse=True)
        selected_ids = {seg["segment_id"] for seg in segments[:4]}
        segments.sort(key=lambda seg: seg["start_s"])
        for seg in segments:
            seg["selected"] = seg["segment_id"] in selected_ids
            seg["segment_role"] = "work" if seg["selected"] else "tempo_unselected"
        return segments

    def _user_lap_segments(self, records: list[dict[str, Any]], laps: list[dict[str, Any]], load: dict[str, Any]) -> list[dict[str, Any]]:
        cp = self.athlete.critical_power_w or load.get("weighted_power_w") or 0
        if not laps:
            return [self._segment(records, 0, len(records) - 1, "lap_01", "synthetic_full_activity", cp, role="user_lap")]
        output = []
        for idx, lap in enumerate(laps[:20]):
            start_s = lap.get("start_s")
            duration_s = lap.get("duration_s")
            if start_s is None or duration_s is None:
                continue
            end_s = start_s + duration_s
            start_i = nearest_index(records, start_s)
            end_i = nearest_index(records, end_s)
            seg = self._segment(records, start_i, end_i, f"lap_{idx + 1:02d}", "fit_lap_message", cp, role="user_lap")
            seg["source_lap"] = lap
            output.append(seg)
        return output

    def _segment(
        self,
        records: list[dict[str, Any]],
        start_i: int,
        end_i: int,
        segment_id: str,
        source: str,
        cp: float | int | None,
        role: str = "work",
    ) -> dict[str, Any]:
        rows = records[start_i : end_i + 1]
        powers = values(rows, "power")
        hrs = values(rows, "heart_rate")
        cadences = values(rows, "cadence")
        speeds = values(rows, "speed")
        distances = values(rows, "distance")
        temps = values(rows, "temperature")
        duration = rows[-1]["elapsed_s"] - rows[0]["elapsed_s"] + 1
        mean_power = mean(powers) if powers else None
        wp = normalized_power(powers)
        cp_ratio = mean_power / cp if mean_power and cp else None
        work_kj = sum(powers) / 1000 if powers else None
        distance = max(distances) - min(distances) if len(distances) >= 2 else None
        torque = estimated_torque(rows)
        hr_mean = mean(hrs) if hrs else None
        return {
            "segment_id": segment_id,
            "segment_source": source,
            "segment_role": role,
            "has_pedaling_dynamics": torque["sample_count"] > 0,
            "start_s": rows[0]["elapsed_s"],
            "end_s": rows[-1]["elapsed_s"],
            "duration_s": duration,
            "power": {
                "mean_w": round_or_none(mean_power, 1),
                "weighted_w": round_or_none(wp, 0),
                "max_w": round_or_none(max(powers), 1) if powers else None,
                "work_kj": round_or_none(work_kj, 1),
                "cp_ratio": round_or_none(cp_ratio, 3),
                "intensity_ratio": round_or_none(wp / cp, 3) if wp and cp else None,
                "variability_ratio": round_or_none(wp / mean_power, 3) if wp and mean_power else None,
                "load_score": round_or_none(((duration * wp * (wp / cp)) / (cp * 3600) * 100), 1) if wp and cp else None,
                "mean_wkg": round_or_none(mean_power / self.athlete.body_mass_kg, 2) if mean_power and self.athlete.body_mass_kg else None,
                "weighted_wkg": round_or_none(wp / self.athlete.body_mass_kg, 2) if wp and self.athlete.body_mass_kg else None,
            },
            "heart_rate": {
                "mean_bpm": round_or_none(hr_mean, 1),
                "efficiency_factor_w_per_bpm": round_or_none(wp / hr_mean, 3) if wp and hr_mean else None,
                "decoupling_pct": decoupling(rows),
            },
            "movement": {
                "distance_m": round_or_none(distance, 1),
                "mean_cadence_rpm": round_or_none(mean(cadences), 1) if cadences else None,
                "mean_speed_kmh": round_or_none(mean(speeds) * 3.6, 1) if speeds else None,
                "mean_temperature_c": round_or_none(mean(temps), 1) if temps else None,
            },
            "pedaling_dynamics": empty_pedaling_dynamics(torque),
            "classification": {
                "effort_type": effort_type(cp_ratio),
                "execution_pattern": execution_pattern(duration, cp_ratio),
            },
            "metric_presence": segment_metric_presence(powers, hrs, cadences, speeds, distance, torque["sample_count"], role),
        }

    def _interval_lap_comparison(self, intervals: list[dict[str, Any]], laps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for interval in intervals:
            if not interval.get("selected"):
                continue
            for lap in laps:
                overlap = max(0, min(interval["end_s"], lap["end_s"]) - max(interval["start_s"], lap["start_s"]) + 1)
                if overlap <= 0:
                    continue
                i_power = interval["power"]["mean_w"]
                l_power = lap["power"]["mean_w"]
                rows.append(
                    {
                        "interval_id": interval["segment_id"],
                        "lap_id": lap["segment_id"],
                        "overlap_s": overlap,
                        "interval_start_s": interval["start_s"],
                        "interval_duration_s": interval["duration_s"],
                        "lap_start_s": lap["start_s"],
                        "lap_duration_s": lap["duration_s"],
                        "interval_mean_power_w": i_power,
                        "lap_mean_power_w": l_power,
                        "mean_power_delta_w": round_or_none(i_power - l_power, 1) if i_power and l_power else None,
                        "interval_has_pedaling_dynamics": interval["has_pedaling_dynamics"],
                        "lap_has_pedaling_dynamics": lap["has_pedaling_dynamics"],
                    }
                )
        return rows

    def _duration_curves(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        durations = [1, 5, 10, 15, 30, 45, 60, 120, 180, 300, 600, 1200, 1800, 2700, 3600, 5400]
        return {
            "power": curve(records, "power", durations),
            "heart_rate": curve(records, "heart_rate", durations),
            "cadence": curve(records, "cadence", durations),
            "velocity": curve(records, "speed", durations),
        }

    def _distributions(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "power_w": histogram(values(records, "power"), bucket_size=25),
            "heart_rate_bpm": histogram(values(records, "heart_rate"), bucket_size=5),
            "cadence_rpm": histogram(values(records, "cadence"), bucket_size=5),
            "speed_kmh": histogram([v * 3.6 for v in values(records, "speed")], bucket_size=2),
        }

    def _metric_presence(self, records: list[dict[str, Any]], intervals: list[dict[str, Any]], laps: list[dict[str, Any]], duration_curves: dict[str, Any]) -> dict[str, Any]:
        has_power = bool(values(records, "power"))
        has_hr = bool(values(records, "heart_rate"))
        has_cadence = bool(values(records, "cadence"))
        has_speed = bool(values(records, "speed"))
        has_alt = bool(values(records, "altitude"))
        has_pd = any(seg["has_pedaling_dynamics"] for seg in intervals + laps)
        return {
            "power": {
                "activity_mean": status(has_power),
                "activity_max": status(has_power),
                "weighted_power": status(has_power),
                "distribution": status(has_power),
                "duration_curve": status(duration_curves["power"]["representative_points"]),
                "auto_segments": status(intervals),
                "user_laps": status(laps),
            },
            "heart_rate": {
                "activity_mean": status(has_hr),
                "distribution": status(has_hr),
                "duration_curve": status(duration_curves["heart_rate"]["representative_points"]),
                "auto_segments": status(intervals),
                "user_laps": status(laps),
            },
            "cadence": {
                "activity_mean": status(has_cadence),
                "duration_curve": status(duration_curves["cadence"]["representative_points"]),
                "auto_segments": status(intervals),
                "user_laps": status(laps),
            },
            "speed": {
                "activity_distance": status(has_speed),
                "duration_curve": status(duration_curves["velocity"]["representative_points"]),
                "auto_segments": status(intervals),
                "user_laps": status(laps),
            },
            "altitude": {
                "activity_total_ascent": status(has_alt),
                "auto_segments": status(has_alt and intervals),
                "user_laps": status(has_alt and laps),
            },
            "terrain": {
                "activity_total_ascent": status(has_alt),
                "auto_segments": status(has_alt and intervals),
                "user_laps": status(has_alt and laps),
            },
            "w_prime": {
                "w_prime_balance": "available" if self.athlete.critical_power_w else "missing",
                "auto_segments": "not_applicable",
                "user_laps": "not_applicable",
            },
            "laps": {"user_lap_segments": status(laps), "count": len(laps)},
            "duration_curves": {
                "mode": "standard",
                "power": status(duration_curves["power"]["representative_points"]),
                "heart_rate": status(duration_curves["heart_rate"]["representative_points"]),
                "cadence": status(duration_curves["cadence"]["representative_points"]),
                "velocity": status(duration_curves["velocity"]["representative_points"]),
                "representative_points": "available",
                "full_power_points": "removed",
                "heart_rate_full_points": "removed",
                "cadence_full_points": "removed",
                "velocity_full_points": "removed",
            },
            "pedaling_dynamics": {
                "session": status(has_pd),
                "auto_segments": status(any(seg["has_pedaling_dynamics"] for seg in intervals)),
                "user_laps": status(any(seg["has_pedaling_dynamics"] for seg in laps)),
            },
        }

    def _data_presence_matrix(self, intervals: list[dict[str, Any]], laps: list[dict[str, Any]]) -> dict[str, Any]:
        def compact(seg: dict[str, Any]) -> dict[str, Any]:
            return {
                "segment_id": seg["segment_id"],
                "power": seg["metric_presence"]["power"]["overall"],
                "heart_rate": seg["metric_presence"]["heart_rate"]["overall"],
                "movement": "available" if seg["movement"]["distance_m"] is not None else "missing",
                "terrain": seg["metric_presence"]["terrain"]["overall"],
                "w_prime": "missing" if self.athlete.critical_power_w else "not_applicable",
                "pedaling_dynamics": seg["metric_presence"]["pedaling_dynamics"]["overall"],
                "has_pedaling_dynamics": seg["has_pedaling_dynamics"],
                "metric_presence": seg["metric_presence"],
            }

        return {
            "metric_status_values": ["available", "missing", "removed", "not_applicable"],
            "duration_curves": {
                "mode": "standard",
                "representative_points": "available",
                "full_power_points": "removed",
                "heart_rate_cadence_velocity_full_points": "removed",
            },
            "signals": {
                "power_distribution": "available",
                "heart_rate_distribution": "available",
                "session_pedaling_dynamics": "available" if any(seg["has_pedaling_dynamics"] for seg in intervals + laps) else "missing",
                "stream_processing": "available",
            },
            "auto_interval_segments": [compact(seg) for seg in intervals],
            "user_lap_segments": [compact(seg) for seg in laps],
        }

    def _physiology(self, records: list[dict[str, Any]], w_prime_series: dict[str, Any]) -> dict[str, Any]:
        cp = self.athlete.critical_power_w
        powers = values(records, "power")
        above = [p for p in powers if cp and p > cp]
        w_prime_summary = w_prime_series.get("summary") if isinstance(w_prime_series, dict) else {}
        return {
            "critical_power": {
                "critical_power_w": cp,
                "w_prime_kj": self.athlete.w_prime_kj,
                "time_above_cp_s": len(above) if cp else None,
                "work_above_cp_kj": round_or_none(sum(p - cp for p in above) / 1000, 1) if cp else None,
                "severe_domain_bout_count": count_bouts([p > cp for p in powers]) if cp else None,
                "estimated_min_reserve_balance_kj": w_prime_summary.get("min_balance_kj"),
                "notes": [] if self.athlete.w_prime_kj else ["w_prime_kj not provided"],
            },
            "w_prime_balance": {
                "schema_version": "w_prime_balance.v1",
                "method": w_prime_series.get("method", "critical_power_summary_only") if isinstance(w_prime_series, dict) else "critical_power_summary_only",
                "inputs": {"critical_power_w": cp, "w_prime_kj": self.athlete.w_prime_kj, "point_step_s": 1},
                "summary": {
                    "min_balance_kj": w_prime_summary.get("min_balance_kj"),
                    "min_balance_pct": w_prime_summary.get("min_balance_pct"),
                    "end_balance_kj": w_prime_summary.get("end_balance_kj"),
                    "end_balance_pct": w_prime_summary.get("end_balance_pct"),
                    "total_depletion_kj": w_prime_summary.get("total_depletion_kj"),
                    "total_recovery_kj": w_prime_summary.get("total_recovery_kj"),
                    "time_below_20pct_s": w_prime_summary.get("time_below_20pct_s"),
                },
                "points": "removed",
            },
        }

    def _w_prime_balance_series(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        cp = self.athlete.critical_power_w
        w_prime_kj = self.athlete.w_prime_kj
        powers = values(records, "power")
        if not cp or not w_prime_kj or not powers:
            return {
                "method": "critical_power_w_prime_balance_preliminary",
                "summary": {},
                "notes": ["critical_power_w or w_prime_kj not provided"],
            }

        balance = float(w_prime_kj)
        min_balance = balance
        total_depletion = 0.0
        total_recovery = 0.0
        below_20 = 0
        tau_s = 546.0
        for power in powers:
            if power > cp:
                delta = (power - cp) / 1000.0
                balance = max(0.0, balance - delta)
                total_depletion += delta
            else:
                recovery = (float(w_prime_kj) - balance) * (1.0 - math.exp(-1.0 / tau_s))
                balance = min(float(w_prime_kj), balance + recovery)
                total_recovery += recovery
            min_balance = min(min_balance, balance)
            if balance <= float(w_prime_kj) * 0.2:
                below_20 += 1

        return {
            "method": "critical_power_w_prime_balance_preliminary_exponential_recovery",
            "summary": {
                "capacity_kj": round_or_none(w_prime_kj, 1),
                "min_balance_kj": round_or_none(min_balance, 1),
                "min_balance_pct": round_or_none(min_balance / float(w_prime_kj) * 100, 1),
                "end_balance_kj": round_or_none(balance, 1),
                "end_balance_pct": round_or_none(balance / float(w_prime_kj) * 100, 1),
                "total_depletion_kj": round_or_none(total_depletion, 1),
                "total_recovery_kj": round_or_none(total_recovery, 1),
                "time_below_20pct_s": below_20,
            },
            "points": "removed",
        }

    def _coach_context(self, load: dict[str, Any], intervals: list[dict[str, Any]]) -> dict[str, Any]:
        intense = any((seg["power"]["cp_ratio"] or 0) >= 1 for seg in intervals)
        long_work = sum(seg["duration_s"] for seg in intervals if seg.get("selected", False))
        return {
            "session_type_guess": "high_intensity_or_race_like" if intense else "endurance_or_tempo",
            "main_stimulus": "severe_domain_power" if intense else "aerobic_power",
            "fatigue_signal": "substantial_work_above_cp" if intense or (load.get("session_load_score") or 0) > 80 else "moderate_session_load",
            "data_quality_notes": [],
            "scope_note": "single FIT file only; long-term fatigue, adaptation, and peaking decisions require history",
            "selected_work_duration_s": long_work,
        }

    def _data_quality(self, records: list[dict[str, Any]]) -> list[str]:
        notes = []
        if not values(records, "power"):
            notes.append("power missing")
        if not values(records, "heart_rate"):
            notes.append("heart_rate missing")
        if not values(records, "cadence"):
            notes.append("cadence missing")
        return notes

    def _llm_segment(self, seg: dict[str, Any]) -> dict[str, Any]:
        return {
            "segment_id": seg["segment_id"],
            "segment_role": seg["segment_role"],
            "has_pedaling_dynamics": seg["has_pedaling_dynamics"],
            "selected": seg.get("selected", False),
            "start_s": seg["start_s"],
            "end_s": seg["end_s"],
            "duration_s": seg["duration_s"],
            "mean_power_w": seg["power"]["mean_w"],
            "weighted_power_w": seg["power"]["weighted_w"],
            "cp_ratio": seg["power"]["cp_ratio"],
            "mean_heart_rate_bpm": seg["heart_rate"]["mean_bpm"],
            "effort_type": seg["classification"]["effort_type"],
            "execution_pattern": seg["classification"].get("execution_pattern"),
            "metric_presence": simple_presence(seg),
        }

    def _llm_lap(self, seg: dict[str, Any]) -> dict[str, Any]:
        data = self._llm_segment(seg)
        data.pop("selected", None)
        data.pop("execution_pattern", None)
        return data


def normalize_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "elapsed_s": row.get("elapsed_s"),
        "power": as_float(row.get("power")),
        "heart_rate": as_float(row.get("heart_rate")),
        "cadence": as_float(row.get("cadence")),
        "speed": as_float(row.get("enhanced_speed") or row.get("speed")),
        "distance": as_float(row.get("distance")),
        "altitude": as_float(row.get("enhanced_altitude") or row.get("altitude")),
        "temperature": as_float(row.get("temperature")),
    }


def normalize_lap(row: dict[str, Any], first_ts: Any = None) -> dict[str, Any]:
    start = row.get("start_time")
    timestamp = row.get("timestamp")
    duration = as_float(row.get("total_timer_time") or row.get("total_elapsed_time"))
    start_s = 0
    if first_ts and start:
        start_s = max(0, int((start - first_ts).total_seconds()))
    elif start and timestamp and duration:
        start_s = max(0, int((timestamp - start).total_seconds() - duration))
    return {
        "start_s": start_s,
        "duration_s": int(duration) if duration else None,
        "total_elapsed_time_s": as_float(row.get("total_elapsed_time")),
        "total_timer_time_s": as_float(row.get("total_timer_time")),
        "total_distance_m": as_float(row.get("total_distance")),
        "avg_power_w": as_float(row.get("avg_power")),
        "max_power_w": as_float(row.get("max_power")),
        "normalized_power_w": as_float(row.get("normalized_power")),
        "avg_heart_rate_bpm": as_float(row.get("avg_heart_rate")),
        "max_heart_rate_bpm": as_float(row.get("max_heart_rate")),
        "source_message": "lap",
    }


def values(records: list[dict[str, Any]], key: str) -> list[float]:
    return [row[key] for row in records if row.get(key) is not None]


def normalized_power(powers: list[float]) -> float | None:
    if not powers:
        return None
    if len(powers) < 30:
        return mean(powers)
    rolling = []
    window = sum(powers[:30])
    rolling.append(window / 30)
    for idx in range(30, len(powers)):
        window += powers[idx] - powers[idx - 30]
        rolling.append(window / 30)
    return sum(v**4 for v in rolling) ** 0.25 / (len(rolling) ** 0.25)


def best_average(values_: list[float], duration_s: int) -> float | None:
    if not values_:
        return None
    if len(values_) <= duration_s:
        return mean(values_)
    window = sum(values_[:duration_s])
    best = window
    for idx in range(duration_s, len(values_)):
        window += values_[idx] - values_[idx - duration_s]
        best = max(best, window)
    return best / duration_s


def curve(records: list[dict[str, Any]], key: str, durations: list[int]) -> dict[str, Any]:
    vals = values(records, key)
    points = []
    for duration in durations:
        best = best_average(vals, duration)
        if best is not None:
            points.append({"duration_s": duration, "best_average": round_or_none(best, 1)})
    return {"mode": "standard", "representative_points": points, "points": "removed"}


def histogram(vals: list[float], bucket_size: int) -> list[dict[str, Any]]:
    if not vals:
        return []
    buckets: dict[int, int] = {}
    for value in vals:
        bucket = int(value // bucket_size) * bucket_size
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return [{"from": key, "to": key + bucket_size, "seconds": buckets[key]} for key in sorted(buckets)]


def total_ascent(altitudes: list[float]) -> float | None:
    if len(altitudes) < 2:
        return None
    gain = 0.0
    for prev, cur in zip(altitudes, altitudes[1:]):
        if cur > prev:
            gain += cur - prev
    return gain


def estimated_torque(records: list[dict[str, Any]]) -> dict[str, Any]:
    vals = []
    for row in records:
        power = row.get("power")
        cadence = row.get("cadence")
        if power is None or not cadence:
            continue
        omega = cadence * 2 * math.pi / 60
        if omega > 0:
            vals.append(power / omega)
    return {
        "mean_nm": round_or_none(mean(vals), 1) if vals else None,
        "max_nm": round_or_none(max(vals), 1) if vals else None,
        "sample_count": len(vals),
    }


def empty_pedaling_dynamics(torque: dict[str, Any]) -> dict[str, Any]:
    return {
        "left_right_balance": {"mean_left_pct": None, "min_left_pct": None, "max_left_pct": None, "sample_count": 0},
        "platform_center_offset": {"left_mean_mm": None, "right_mean_mm": None, "left_sample_count": 0, "right_sample_count": 0},
        "power_phase": {
            "left_start_mean_deg": None,
            "left_end_mean_deg": None,
            "left_width_mean_deg": None,
            "right_start_mean_deg": None,
            "right_end_mean_deg": None,
            "right_width_mean_deg": None,
        },
        "peak_power_phase": {
            "left_start_mean_deg": None,
            "left_end_mean_deg": None,
            "left_width_mean_deg": None,
            "right_start_mean_deg": None,
            "right_end_mean_deg": None,
            "right_width_mean_deg": None,
        },
        "torque_effectiveness": {"left_mean_pct": None, "right_mean_pct": None, "left_sample_count": 0, "right_sample_count": 0},
        "pedal_smoothness": {"left_mean_pct": None, "right_mean_pct": None, "left_sample_count": 0, "right_sample_count": 0},
        "estimated_crank_torque": torque,
    }


def segment_metric_presence(powers, hrs, cadences, speeds, distance, torque_count, role) -> dict[str, Any]:
    return {
        "power": {
            "overall": status(powers),
            "mean_w": status(powers),
            "weighted_w": status(powers),
            "max_w": status(powers),
            "work_kj": status(powers),
            "cp_ratio": status(powers),
        },
        "heart_rate": {
            "overall": status(hrs),
            "mean_bpm": status(hrs),
            "efficiency_factor_w_per_bpm": status(powers and hrs),
            "decoupling_pct": status(powers and hrs),
        },
        "cadence": {"overall": status(cadences), "mean_rpm": status(cadences)},
        "speed": {"overall": status(speeds), "mean_kmh": status(speeds), "distance_m": status(distance)},
        "altitude": {"overall": "missing", "mean_m": "missing"},
        "terrain": {
            "overall": "missing",
            "ascent_m": "missing",
            "descent_m": "missing",
            "mean_grade_pct": "missing",
            "vam_m_per_h": "missing",
        },
        "w_prime": {"overall": "not_applicable", "start_pct": "not_applicable", "end_pct": "not_applicable", "min_pct": "not_applicable"},
        "laps": {"overall": "available" if role == "user_lap" else "not_applicable"},
        "pedaling_dynamics": {"overall": "available" if torque_count else "missing"},
    }


def simple_presence(seg: dict[str, Any]) -> dict[str, str]:
    mp = seg["metric_presence"]
    return {
        "power": mp["power"]["overall"],
        "heart_rate": mp["heart_rate"]["overall"],
        "cadence": mp["cadence"]["overall"],
        "speed": mp["speed"]["overall"],
        "altitude": mp["altitude"]["overall"],
        "terrain": mp["terrain"]["overall"],
        "w_prime": mp["w_prime"]["overall"],
        "laps": mp["laps"]["overall"],
        "pedaling_dynamics": mp["pedaling_dynamics"]["overall"],
    }


def merge_short_gaps(ranges, records, max_gap_s):
    if not ranges:
        return []
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        prev_start, prev_end = merged[-1]
        gap = records[start]["elapsed_s"] - records[prev_end]["elapsed_s"]
        if gap <= max_gap_s:
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    return merged


def nearest_index(records: list[dict[str, Any]], elapsed_s: float | int) -> int:
    return min(range(len(records)), key=lambda i: abs(records[i]["elapsed_s"] - elapsed_s))


def decoupling(records: list[dict[str, Any]]) -> float | None:
    if len(records) < 120:
        return None
    mid = len(records) // 2
    first = efficiency(records[:mid])
    second = efficiency(records[mid:])
    if not first or not second:
        return None
    return round_or_none((second - first) / first * 100, 2)


def efficiency(records: list[dict[str, Any]]) -> float | None:
    powers = values(records, "power")
    hrs = values(records, "heart_rate")
    if not powers or not hrs:
        return None
    return mean(powers) / mean(hrs)


def effort_type(cp_ratio: float | None) -> str:
    if cp_ratio is None:
        return "unknown"
    if cp_ratio >= 1.05:
        return "vo2_or_severe"
    if cp_ratio >= 0.95:
        return "threshold"
    if cp_ratio >= 0.82:
        return "sst_or_tempo"
    if cp_ratio >= 0.6:
        return "endurance"
    return "very_easy_recovery"


def execution_pattern(duration: int, cp_ratio: float | None) -> str | None:
    if cp_ratio is None:
        return None
    if duration >= 600 and cp_ratio >= 0.85:
        return "race_like_long_press"
    if duration < 180 and cp_ratio >= 1:
        return "short_high_power_bout"
    return "steady_segment"


def count_bouts(flags: list[bool]) -> int:
    count = 0
    in_bout = False
    for flag in flags:
        if flag and not in_bout:
            count += 1
            in_bout = True
        elif not flag:
            in_bout = False
    return count


def available(value: Any) -> str:
    return status(value)


def status(value: Any) -> str:
    return "available" if value else "missing"


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def round_or_none(value: Any, digits: int) -> Any:
    if value is None:
        return None
    rounded = round(float(value), digits)
    return int(rounded) if digits == 0 else rounded
