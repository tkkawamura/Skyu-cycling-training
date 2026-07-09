from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests


class IntervalsError(RuntimeError):
    pass


@dataclass
class IntervalsConfig:
    api_key: str
    athlete_id: str = "0"
    base_url: str = "https://intervals.icu"
    data_dir: str = "data"
    download_fit: bool = True


class IntervalsClient:
    def __init__(self, config: IntervalsConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.athlete_id = config.athlete_id or "0"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "cycling-dashboard/0.1 (+personal local app)",
                "Accept": "application/json,text/csv,*/*",
            }
        )
        self.fit_dir = Path(config.data_dir) / "fit_files"
        self.fit_dir.mkdir(parents=True, exist_ok=True)

    def collect_dashboard(self, oldest: date, newest: date) -> dict[str, Any]:
        if not self.config.api_key:
            raise IntervalsError("INTERVALS_API_KEY is not set. Showing sample data.")

        wellness = self.get_wellness(oldest, newest)
        activities = self.get_activities(oldest, newest)
        latest_ride = self._find_latest_ride(activities)

        activity_detail = None
        fit_path = None
        if latest_ride and latest_ride.get("id"):
            activity_detail = self.get_activity_detail(str(latest_ride["id"]))
            latest_ride = {**latest_ride, **self._summarize_activity_detail(activity_detail)}
            if self.config.download_fit:
                fit_path = self.download_fit(str(latest_ride["id"]), latest_ride.get("date"))

        metrics = self._latest_metrics(wellness, activities, latest_ride)
        trend = self._build_trend(wellness, activities)

        return {
            "metrics": metrics,
            "latest_ride": latest_ride,
            "activity_detail_available": bool(activity_detail),
            "fit_path": fit_path,
            "trend": trend,
            "recent_activities": activities[:12],
        }

    def get_wellness(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        data = self._get_json(
            f"/api/v1/athlete/{self.athlete_id}/wellness",
            params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("wellness") or data.get("items") or []
        return []

    def get_activities(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        try:
            data = self._get_json(
                f"/api/v1/athlete/{self.athlete_id}/activities",
                params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
            )
            activities = data if isinstance(data, list) else data.get("activities", [])
        except IntervalsError:
            activities = self._get_activities_csv(oldest, newest)

        normalized = [self._normalize_activity(item) for item in activities]
        normalized = [item for item in normalized if item.get("date")]
        return sorted(normalized, key=lambda item: item["date"], reverse=True)

    def get_activity_detail(self, activity_id: str) -> dict[str, Any] | None:
        try:
            data = self._get_json(f"/api/v1/activity/{activity_id}", params={"intervals": "true"})
            return data if isinstance(data, dict) else None
        except IntervalsError:
            return None

    def download_fit(self, activity_id: str, ride_date: str | None) -> str | None:
        target = self.fit_dir / f"{ride_date or 'activity'}-{activity_id}.fit"
        if target.exists() and target.stat().st_size > 0:
            return str(target)

        paths = [
            f"/api/v1/activity/{activity_id}/download.fit",
            f"/api/v1/activity/{activity_id}/download",
            f"/api/v1/activity/{activity_id}/file.fit",
        ]
        for path in paths:
            try:
                response = self._request("GET", path, params={"format": "fit"})
                content_type = response.headers.get("content-type", "")
                if response.content and ("json" not in content_type.lower()):
                    target.write_bytes(response.content)
                    return str(target)
            except IntervalsError:
                continue
        return None

    def sample_dashboard(self, oldest: date, newest: date) -> dict[str, Any]:
        trend = []
        for idx in range((newest - oldest).days + 1):
            current = oldest + timedelta(days=idx)
            trend.append(
                {
                    "date": current.isoformat(),
                    "fitness": 58 + idx * 0.25,
                    "fatigue": 64 + idx * 0.45,
                    "form": -6 - idx * 0.2,
                    "weight": 63.4,
                    "ftp": 250,
                    "eftp": 258,
                    "training_load": 0,
                }
            )
        latest = trend[-1]
        return {
            "metrics": latest,
            "latest_ride": {
                "id": "sample",
                "date": newest.isoformat(),
                "name": "Sample Endurance Ride",
                "type": "Ride",
                "moving_time": 5400,
                "distance": 47000,
                "training_load": 78,
                "average_watts": 178,
                "weighted_average_watts": 205,
                "average_heartrate": 142,
            },
            "activity_detail_available": False,
            "fit_path": None,
            "trend": trend,
            "recent_activities": [],
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.base_url}{path}"
        response = self.session.request(
            method,
            url,
            auth=("API_KEY", self.config.api_key),
            timeout=30,
            **kwargs,
        )
        if response.status_code >= 400:
            raise IntervalsError(f"Intervals.icu API error {response.status_code}: {path}")
        return response

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request("GET", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise IntervalsError(f"Intervals.icu did not return JSON for {path}") from exc

    def _get_activities_csv(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"/api/v1/athlete/{self.athlete_id}/activities.csv",
            params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        )
        return list(csv.DictReader(io.StringIO(response.text)))

    def _normalize_activity(self, item: dict[str, Any]) -> dict[str, Any]:
        date_value = first_value(item, "date", "start_date_local", "start_date", "Start Date")
        if date_value and "T" in str(date_value):
            date_value = str(date_value).split("T")[0]
        return {
            "id": first_value(item, "id", "activity_id", "Activity ID"),
            "date": date_value,
            "name": first_value(item, "name", "Name", "filename") or "Ride",
            "type": first_value(item, "type", "sport", "Type") or "Ride",
            "moving_time": number(first_value(item, "moving_time", "Moving Time")),
            "distance": number(first_value(item, "distance", "Distance")),
            "training_load": number(first_value(item, "icu_training_load", "training_load", "Load")),
            "average_watts": number(first_value(item, "average_watts", "Average Watts")),
            "weighted_average_watts": number(first_value(item, "weighted_average_watts", "Weighted Average Watts", "power")),
            "average_heartrate": number(first_value(item, "average_heartrate", "Average Heart Rate")),
        }

    def _find_latest_ride(self, activities: list[dict[str, Any]]) -> dict[str, Any] | None:
        for activity in activities:
            sport = str(activity.get("type", "")).lower()
            if "ride" in sport or "cycling" in sport or "bike" in sport:
                return activity
        return activities[0] if activities else None

    def _summarize_activity_detail(self, detail: dict[str, Any] | None) -> dict[str, Any]:
        if not detail:
            return {}
        return {
            "training_load": number(first_value(detail, "icu_training_load", "training_load")),
            "average_watts": number(first_value(detail, "average_watts")),
            "weighted_average_watts": number(first_value(detail, "weighted_average_watts")),
            "average_heartrate": number(first_value(detail, "average_heartrate")),
            "decoupling": number(first_value(detail, "decoupling", "icu_decoupling")),
            "interval_count": len(detail.get("icu_intervals") or []),
        }

    def _latest_metrics(
        self,
        wellness: list[dict[str, Any]],
        activities: list[dict[str, Any]],
        latest_ride: dict[str, Any] | None,
    ) -> dict[str, Any]:
        latest_wellness = sorted(wellness, key=lambda row: str(first_value(row, "id", "date") or ""))[-1] if wellness else {}
        latest_activity = latest_ride or (activities[0] if activities else {})
        return {
            "date": first_value(latest_wellness, "id", "date") or latest_activity.get("date"),
            "fitness": number(first_value(latest_wellness, "ctl", "fitness", "icu_ctl")),
            "fatigue": number(first_value(latest_wellness, "atl", "fatigue", "icu_atl")),
            "form": number(first_value(latest_wellness, "tsb", "form", "icu_tsb")),
            "weight": number(first_value(latest_wellness, "weight", "body_mass", "mass")),
            "ftp": number(first_value(latest_wellness, "ftp", "threshold_power")),
            "eftp": number(first_value(latest_wellness, "eftp", "eftp", "estimated_ftp")),
            "training_load": latest_activity.get("training_load"),
        }

    def _build_trend(self, wellness: list[dict[str, Any]], activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        load_by_date = {item.get("date"): item.get("training_load") for item in activities}
        trend = []
        for row in wellness:
            row_date = first_value(row, "id", "date")
            trend.append(
                {
                    "date": row_date,
                    "fitness": number(first_value(row, "ctl", "fitness", "icu_ctl")),
                    "fatigue": number(first_value(row, "atl", "fatigue", "icu_atl")),
                    "form": number(first_value(row, "tsb", "form", "icu_tsb")),
                    "weight": number(first_value(row, "weight", "body_mass", "mass")),
                    "ftp": number(first_value(row, "ftp", "threshold_power")),
                    "eftp": number(first_value(row, "eftp", "eftp", "estimated_ftp")),
                    "training_load": load_by_date.get(row_date, 0),
                }
            )
        return sorted([item for item in trend if item.get("date")], key=lambda item: item["date"])


def first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result.is_integer():
        return int(result)
    return round(result, 2)
