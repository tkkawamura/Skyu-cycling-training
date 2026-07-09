from __future__ import annotations

import csv
import gzip
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
        athlete_profile = self.get_athlete_profile()
        latest_ride = self._find_latest_ride(activities)

        activity_detail = None
        fit_path = None
        fit_download_info = None
        if latest_ride and latest_ride.get("id"):
            activity_detail = self.get_activity_detail(str(latest_ride["id"]))
            latest_ride = {**latest_ride, **self._summarize_activity_detail(activity_detail)}
            if self.config.download_fit:
                fit_path, fit_download_info = self.download_fit(latest_ride, activity_detail)

        metrics = self._latest_metrics(wellness, activities, latest_ride, athlete_profile)
        trend = self._build_trend(wellness, activities)

        return {
            "metrics": metrics,
            "athlete_profile_available": bool(athlete_profile),
            "latest_ride": latest_ride,
            "activity_detail_available": bool(activity_detail),
            "fit_path": fit_path,
            "fit_download_info": fit_download_info,
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

    def get_athlete_profile(self) -> dict[str, Any]:
        for path in (
            f"/api/v1/athlete/{self.athlete_id}",
            f"/api/v1/athlete/{self.athlete_id}/profile",
            f"/api/v1/athlete/{self.athlete_id}/settings",
        ):
            try:
                data = self._get_json(path)
                if isinstance(data, dict):
                    return data
            except IntervalsError:
                continue
        return {}

    def download_fit(self, activity: dict[str, Any], detail: dict[str, Any] | None = None) -> tuple[str | None, dict[str, Any]]:
        activity_id = str(activity.get("id") or "")
        ride_date = activity.get("date")
        target = self.fit_dir / f"{ride_date or 'activity'}-{activity_id}.fit"
        if target.exists() and target.stat().st_size > 0:
            return str(target), {"source": "cache", "path": str(target)}

        paths = self._fit_download_paths(activity, detail)
        attempted = []
        for path in paths:
            try:
                attempted.append(path)
                response = self._request("GET", path, params={"format": "fit", "original": "true"})
                content = self._extract_fit_bytes(response)
                if content:
                    target.write_bytes(content)
                    return str(target), {"source": "intervals_auto_download", "path": path, "attempted": attempted}
            except IntervalsError as exc:
                attempted.append(f"{path} -> {exc}")
                continue
        return None, {"source": "not_found", "attempted": attempted}

    def _fit_download_paths(self, activity: dict[str, Any], detail: dict[str, Any] | None) -> list[str]:
        ids = []
        for key in ("id", "activity_id", "external_id"):
            value = activity.get(key)
            if value:
                ids.append(str(value))
        if detail:
            for key in ("id", "activity_id", "external_id", "file_id", "filename"):
                value = detail.get(key)
                if value:
                    ids.append(str(value))
        ids = list(dict.fromkeys(ids))

        direct_paths = []
        for data in (activity, detail or {}):
            direct_paths.extend(find_fit_paths(data))

        suffixes = [
            "original.fit",
            "original",
            "original-file",
            "fit-file",
            "file.fit",
            "file",
            "download.fit",
            "download",
            "export.fit",
        ]
        generated = []
        for activity_id in ids:
            for root in ("/api/v1/activity", "/api/activity", "/api/activities"):
                generated.extend(f"{root}/{activity_id}/{suffix}" for suffix in suffixes)
            generated.extend(
                [
                    f"/api/v1/activity/{activity_id}.fit",
                    f"/api/activity/{activity_id}.fit",
                    f"/api/activities/{activity_id}.fit",
                ]
            )
        return list(dict.fromkeys(direct_paths + generated))

    def _extract_fit_bytes(self, response: requests.Response) -> bytes | None:
        content_type = response.headers.get("content-type", "").lower()
        content = response.content or b""
        if not content or "json" in content_type or content.lstrip().startswith((b"{", b"[")):
            return None
        if content.startswith(b"\x1f\x8b"):
            content = gzip.decompress(content)
        if is_fit_file(content):
            return content
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
                    "training_load": 0,
                }
            )
        latest = trend[-1]
        return {
            "metrics": latest,
            "athlete_profile_available": False,
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
            "external_id": first_value(item, "external_id", "External ID", "strava_id", "garmin_id"),
            "date": date_value,
            "name": first_value(item, "name", "Name", "filename") or "Ride",
            "type": first_value(item, "type", "sport", "Type") or "Ride",
            "moving_time": number(first_value(item, "moving_time", "Moving Time")),
            "distance": number(first_value(item, "distance", "Distance")),
            "training_load": number(first_value(item, "icu_training_load", "training_load", "Load")),
            "average_watts": number(first_value(item, "average_watts", "Average Watts")),
            "weighted_average_watts": number(first_value(item, "weighted_average_watts", "Weighted Average Watts", "power")),
            "average_heartrate": number(first_value(item, "average_heartrate", "Average Heart Rate")),
            "ftp": number(first_value(item, "ftp", "athlete_ftp", "icu_ftp", "threshold_power")),
            "max_heart_rate": number(first_value(item, "max_hr", "max_heartrate", "max_heart_rate", "Max Heart Rate")),
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
            "external_id": first_value(detail, "external_id", "strava_id", "garmin_id"),
            "form": number(first_value(detail, "form", "tsb", "icu_tsb", "freshness")),
            "ftp": number(first_value(detail, "ftp", "athlete_ftp", "icu_ftp", "threshold_power")),
            "max_heart_rate": number(first_value(detail, "max_hr", "max_heartrate", "max_heart_rate")),
        }

    def _latest_metrics(
        self,
        wellness: list[dict[str, Any]],
        activities: list[dict[str, Any]],
        latest_ride: dict[str, Any] | None,
        athlete_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        latest_wellness = sorted(wellness, key=lambda row: str(first_value(row, "id", "date") or ""))[-1] if wellness else {}
        latest_activity = latest_ride or (activities[0] if activities else {})
        athlete_profile = athlete_profile or {}
        fitness = number(first_value(latest_wellness, "ctl", "fitness", "icu_ctl"))
        fatigue = number(first_value(latest_wellness, "atl", "fatigue", "icu_atl"))
        native_form = number(
            first_value(latest_wellness, "form", "tsb", "icu_tsb", "freshness")
            or first_value(latest_activity, "form", "tsb", "icu_tsb", "freshness")
            or deep_first_value(athlete_profile, "form", "tsb", "icu_tsb", "freshness")
        )
        form = native_form
        form_source = "intervals_native" if native_form is not None else None
        if form is None and fitness is not None and fatigue is not None:
            form = round(fitness - fatigue, 2)
            form_source = "computed_fitness_minus_fatigue"
        return {
            "date": first_value(latest_wellness, "id", "date") or latest_activity.get("date"),
            "fitness": fitness,
            "fatigue": fatigue,
            "form": form,
            "form_source": form_source,
            "weight": number(first_value(latest_wellness, "weight", "body_mass", "mass") or first_value(athlete_profile, "weight", "body_mass", "mass")),
            "ftp": number(
                first_value(latest_wellness, "ftp", "threshold_power", "power_threshold", "icu_ftp")
                or deep_first_value(athlete_profile, "ftp", "threshold_power", "power_threshold", "athlete_ftp", "icu_ftp", "cp", "critical_power")
                or first_value(latest_activity, "ftp", "athlete_ftp", "icu_ftp", "threshold_power")
            ),
            "max_heart_rate": number(
                first_value(latest_wellness, "max_hr", "max_heart_rate", "max_heartrate", "hr_max")
                or deep_first_value(athlete_profile, "max_hr", "max_heart_rate", "max_heartrate", "hr_max")
            ),
            "training_load": latest_activity.get("training_load"),
        }

    def _build_trend(self, wellness: list[dict[str, Any]], activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        load_by_date = {item.get("date"): item.get("training_load") for item in activities}
        trend = []
        for row in wellness:
            row_date = first_value(row, "id", "date")
            fitness = number(first_value(row, "ctl", "fitness", "icu_ctl"))
            fatigue = number(first_value(row, "atl", "fatigue", "icu_atl"))
            native_form = number(first_value(row, "form", "tsb", "icu_tsb", "freshness"))
            form = native_form
            form_source = "intervals_native" if native_form is not None else None
            if form is None and fitness is not None and fatigue is not None:
                form = round(fitness - fatigue, 2)
                form_source = "computed_fitness_minus_fatigue"
            trend.append(
                {
                    "date": row_date,
                    "fitness": fitness,
                    "fatigue": fatigue,
                    "form": form,
                    "form_source": form_source,
                    "weight": number(first_value(row, "weight", "body_mass", "mass")),
                    "ftp": number(first_value(row, "ftp", "threshold_power")),
                    "training_load": load_by_date.get(row_date, 0),
                }
            )
        return sorted([item for item in trend if item.get("date")], key=lambda item: item["date"])


def first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def deep_first_value(data: Any, *keys: str) -> Any:
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        for value in data.values():
            found = deep_first_value(value, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data:
            found = deep_first_value(value, *keys)
            if found not in (None, ""):
                return found
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


def find_fit_paths(data: Any) -> list[str]:
    paths = []
    if isinstance(data, dict):
        for value in data.values():
            paths.extend(find_fit_paths(value))
    elif isinstance(data, list):
        for value in data:
            paths.extend(find_fit_paths(value))
    elif isinstance(data, str):
        lowered = data.lower()
        if "fit" in lowered and (lowered.startswith("/api/") or lowered.startswith("api/")):
            paths.append(data if data.startswith("/") else f"/{data}")
        elif "fit" in lowered and data.startswith("https://intervals.icu"):
            paths.append(data.removeprefix("https://intervals.icu"))
    return paths


def is_fit_file(content: bytes) -> bool:
    if len(content) < 14:
        return False
    return content[8:12] == b".FIT" or b".FIT" in content[:32]
