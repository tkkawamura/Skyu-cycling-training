from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from services.analysis import RideAnalyzer
from services.fit_context import AthleteInputs, FitActivityContextBuilder
from services.intervals import IntervalsClient, IntervalsConfig, IntervalsError
from services.storage import LocalStore


load_dotenv()


def _float_env(name: str) -> float | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _secret_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("replace_with_"):
        return ""
    return value


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("APP_SECRET_KEY", "local-cycling-dashboard")

    data_dir = os.getenv("DATA_DIR", "data")
    store = LocalStore(data_dir=data_dir)
    intervals = IntervalsClient(
        IntervalsConfig(
            api_key=_secret_env("INTERVALS_API_KEY"),
            base_url=os.getenv("INTERVALS_BASE_URL", "https://intervals.icu"),
            data_dir=data_dir,
            download_fit=os.getenv("FIT_DOWNLOAD", "true").lower() == "true",
        )
    )
    analyzer = RideAnalyzer(
        api_key=_secret_env("OPENAI_API_KEY"),
        model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
    )
    fit_builder = FitActivityContextBuilder(
        AthleteInputs(
            critical_power_w=_float_env("ATHLETE_CRITICAL_POWER_W"),
            body_mass_kg=_float_env("ATHLETE_BODY_MASS_KG"),
            max_heart_rate_bpm=_float_env("ATHLETE_MAX_HEART_RATE_BPM"),
        )
    )

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/dashboard")
    def dashboard():
        lookback_days = int(request.args.get("days", os.getenv("LOOKBACK_DAYS", "21")))
        today = date.today()
        oldest = today - timedelta(days=lookback_days)

        rpe_by_date = store.list_rpe(oldest.isoformat(), today.isoformat())

        try:
            payload = intervals.collect_dashboard(oldest=oldest, newest=today)
            if payload.get("fit_path"):
                try:
                    payload["fit_activity_context"] = fit_builder.build(payload["fit_path"])
                except Exception as exc:
                    payload["fit_analysis_warning"] = str(exc)
            source = "intervals"
        except IntervalsError as exc:
            payload = intervals.sample_dashboard(oldest=oldest, newest=today)
            payload["warning"] = str(exc)
            source = "sample"

        latest_ride = payload.get("latest_ride")
        latest_date = (latest_ride or {}).get("date") or today.isoformat()
        rpe = rpe_by_date.get(latest_date)
        previous_assessment = store.get_assessment(latest_date)

        if previous_assessment and previous_assessment.get("rpe") == rpe:
            assessment = previous_assessment["assessment"]
        else:
            assessment = analyzer.assess(payload=payload, rpe=rpe)
            store.save_assessment(latest_date, rpe, assessment)

        payload.update(
            {
                "source": source,
                "rpe": rpe,
                "rpe_by_date": rpe_by_date,
                "assessment": assessment,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return jsonify(payload)

    @app.post("/api/rpe")
    def save_rpe():
        body = request.get_json(force=True)
        ride_date = body.get("date") or date.today().isoformat()
        rpe = body.get("rpe")
        if rpe is None:
            store.delete_rpe(ride_date)
            return jsonify({"ok": True, "date": ride_date, "rpe": None})
        rpe_int = int(rpe)
        if rpe_int < 1 or rpe_int > 10:
            return jsonify({"ok": False, "error": "RPE must be between 1 and 10."}), 400
        note = body.get("note", "")
        store.save_rpe(ride_date, rpe_int, note)
        return jsonify({"ok": True, "date": ride_date, "rpe": rpe_int})

    @app.post("/api/fit/analyze")
    def analyze_fit():
        uploaded = request.files.get("fit")
        if not uploaded or not uploaded.filename:
            return jsonify({"ok": False, "error": "FIT file is required."}), 400

        upload_dir = Path(data_dir) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        filename = secure_filename(uploaded.filename)
        if not filename.lower().endswith(".fit"):
            return jsonify({"ok": False, "error": "Only .fit files are supported."}), 400

        target = upload_dir / filename
        uploaded.save(target)
        context = fit_builder.build(target)
        assessment = analyzer.assess({"fit_activity_context": context}, rpe=None)
        return jsonify({"ok": True, "context": context, "assessment": assessment})

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "intervals_configured": bool(_secret_env("INTERVALS_API_KEY")),
                "openai_configured": bool(_secret_env("OPENAI_API_KEY")),
            }
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
