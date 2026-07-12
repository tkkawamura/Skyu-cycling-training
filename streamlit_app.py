from __future__ import annotations

import json
import os
import gzip
import re
import tempfile
from html import escape
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from services.fit_context import AthleteInputs, FitActivityContextBuilder
from services.intervals import IntervalsClient, IntervalsConfig, IntervalsError


load_dotenv()

APP_VERSION = "2026-07-10-markdown-full-coach-context-v1"


def main() -> None:
    st.set_page_config(page_title="トレーニングコーチJSONジェネレータ", layout="wide")
    inject_styles()
    st.markdown('<h1 class="app-title">トレーニングコーチJSONジェネレータ</h1>', unsafe_allow_html=True)
    st.caption(f"アプリ版: {APP_VERSION}")

    lookback_days = int_setting("LOOKBACK_DAYS", 21)
    intervals_snapshot = load_intervals_snapshot(lookback_days)
    render_startup_intervals_snapshot(intervals_snapshot)
    metrics = (intervals_snapshot.get("payload") or {}).get("metrics") or {}

    cp = int(to_float(metrics.get("ftp")) or int_setting("ATHLETE_CRITICAL_POWER_W", 250))
    body_mass = float(to_float(metrics.get("weight")) or float_setting("ATHLETE_BODY_MASS_KG", 63.0))
    max_hr = int(to_float(metrics.get("max_heart_rate")) or int_setting("ATHLETE_MAX_HEART_RATE_BPM", 178))
    w_prime = float(to_float(metrics.get("w_prime_capacity")) or float_setting("ATHLETE_W_PRIME_KJ", 0.0))

    st.subheader("主観入力")
    rpe_cols = st.columns(2)
    pre_rpe = rpe_cols[0].number_input("ライド前RPE", min_value=1, max_value=10, value=None, step=1, placeholder="1〜10")
    post_rpe = rpe_cols[1].number_input("ライド後RPE", min_value=1, max_value=10, value=None, step=1, placeholder="1〜10")
    subjective_note = st.text_area("メモ", value="", placeholder="睡眠、疲労感、補給、脚の感覚など")

    uploaded_file = None
    uploaded_file = st.file_uploader("FITファイルまたは生成済みJSON（自動取得できない場合のみ）")

    if st.button("ChatGPT貼り付け用テキストを生成", type="primary", use_container_width=True):
        with st.spinner("Intervals.icu情報とFITを解析しています..."):
            context = generate_context(
                lookback_days=int(lookback_days),
                athlete=AthleteInputs(
                    critical_power_w=float(cp) if cp else None,
                    body_mass_kg=float(body_mass) if body_mass else None,
                    max_heart_rate_bpm=float(max_hr) if max_hr else None,
                    w_prime_kj=float(w_prime) if w_prime else None,
                ),
                uploaded_file=uploaded_file,
                manual_inputs={
                    "pre_ride_rpe_1_10": int(pre_rpe) if pre_rpe is not None else None,
                    "post_ride_rpe_1_10": int(post_rpe) if post_rpe is not None else None,
                    "subjective_note": subjective_note.strip() or None,
                },
                intervals_snapshot=intervals_snapshot,
                include_prompt=True,
            )
        render_context(context)


def load_intervals_snapshot(lookback_days: int) -> dict[str, Any]:
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    oldest = today - timedelta(days=lookback_days)
    newest = today + timedelta(days=1)
    data_dir = "data"
    intervals_client = IntervalsClient(
        IntervalsConfig(
            api_key=secret_or_env("INTERVALS_API_KEY", ""),
            athlete_id=str(secret_or_env("INTERVALS_ATHLETE_ID", "0")),
            base_url=secret_or_env("INTERVALS_BASE_URL", "https://intervals.icu"),
            data_dir=data_dir,
            download_fit=False,
            request_timeout_s=int_setting("INTERVALS_REQUEST_TIMEOUT_S", 8),
            max_fit_attempts=int_setting("FIT_MAX_ATTEMPTS", 24),
        )
    )
    warnings = []
    try:
        payload = intervals_client.collect_dashboard(oldest=oldest, newest=newest)
        source = "intervals"
    except Exception as exc:
        payload = intervals_client.sample_dashboard(oldest=oldest, newest=today)
        source = "sample"
        warnings.append(f"{type(exc).__name__}: {exc}")
    payload = apply_metric_defaults(payload)
    return {
        "payload": payload,
        "source": source,
        "warnings": warnings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetch_timing": "streamlit_app_session_start_or_rerun_before_json_generation",
        "fetch_window": {
            "timezone": "Asia/Tokyo",
            "today_jst": today.isoformat(),
            "oldest": oldest.isoformat(),
            "newest": newest.isoformat(),
        },
    }


def apply_metric_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(payload.get("metrics") or {})
    if metrics.get("ftp") is None:
        metrics["ftp"] = int_setting("ATHLETE_CRITICAL_POWER_W", 250)
    if metrics.get("max_heart_rate") is None:
        metrics["max_heart_rate"] = int_setting("ATHLETE_MAX_HEART_RATE_BPM", 178)
    payload = dict(payload)
    payload["metrics"] = metrics
    return payload


def render_startup_intervals_snapshot(snapshot: dict[str, Any]) -> None:
    if snapshot.get("source") == "sample":
        st.warning("Intervals.icuの取得に失敗したため、サンプル値を表示しています。")


def generate_context(
    lookback_days: int,
    athlete: AthleteInputs,
    uploaded_file: Any,
    manual_inputs: dict[str, Any],
    intervals_snapshot: dict[str, Any],
    include_prompt: bool,
) -> dict[str, Any]:
    builder = FitActivityContextBuilder(athlete)

    intervals_payload = intervals_snapshot.get("payload") or {}
    intervals_source = intervals_snapshot.get("source")
    warnings = list(intervals_snapshot.get("warnings") or [])

    fit_context = None
    fit_source = None
    uploaded_json_context = None
    fit_path = None
    fit_download_info = None
    if uploaded_file is None and intervals_source == "intervals":
        fit_path, fit_download_info = download_fit_on_demand(intervals_payload)
    if fit_path:
        try:
            fit_context = builder.build(fit_path)
            fit_source = "intervals_auto_download"
        except Exception as exc:
            warnings.append(f"FIT auto analysis failed: {exc}")

    if uploaded_file is not None:
        if str(uploaded_file.name).lower().endswith(".json"):
            uploaded_json_context = load_uploaded_json(uploaded_file)
            fit_context = extract_fit_context(uploaded_json_context)
            fit_source = "manual_json_upload"
        else:
            fit_context = analyze_uploaded_fit(builder, uploaded_file)
            fit_source = "manual_fit_upload"

    metrics = dict(intervals_payload.get("metrics") or {})
    if metrics.get("max_heart_rate") is None and athlete.max_heart_rate_bpm:
        metrics["max_heart_rate"] = athlete.max_heart_rate_bpm
    if metrics.get("ftp") is None and athlete.critical_power_w:
        metrics["ftp"] = athlete.critical_power_w
    if athlete.w_prime_kj:
        metrics["w_prime_capacity"] = athlete.w_prime_kj

    intervals_context = {
        "source": intervals_source,
        "fetched_at": intervals_snapshot.get("fetched_at"),
        "fetch_timing": intervals_snapshot.get("fetch_timing"),
        "fetch_window": intervals_snapshot.get("fetch_window"),
        "condition_metrics_purpose": "These Intervals.icu values are fetched automatically at app load/rerun and should be used as the condition baseline for the ride review.",
        "metrics": metrics,
        "latest_ride": sanitize_latest_ride(intervals_payload.get("latest_ride")),
        "recent_activities": [sanitize_latest_ride(item) for item in (intervals_payload.get("recent_activities") or [])[:5]],
        "fit_auto_downloaded": bool(fit_path),
        "fit_auto_download": compact_download_info(fit_download_info or intervals_payload.get("fit_download_info")),
        "fit_analysis_source": fit_source,
    }
    compact_fit_context = build_compact_fit_context(fit_context)
    output = {
        "schema_version": "cycling_training_review_context.v2",
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "app_version": APP_VERSION,
            "source": "streamlit_community_cloud",
            "base_context": "compact_fit_activity_context.v1",
            "privacy": {
                "raw_fit_records_included": False,
                "location_fields_included": False,
                "api_keys_included": False,
            },
            "openai_api_called": False,
            "warnings": warnings,
        },
        "review_prompt": build_chatgpt_prompt(),
        "athlete_inputs": {
            "critical_power_w": athlete.critical_power_w,
            "body_mass_kg": athlete.body_mass_kg,
            "max_heart_rate_bpm": athlete.max_heart_rate_bpm,
            "w_prime_kj": athlete.w_prime_kj,
        },
        "manual_inputs": manual_inputs,
        "intervals_icu": intervals_context,
        "llm_review_summary": build_review_summary(intervals_context, compact_fit_context, manual_inputs),
        "fit_activity_context": compact_fit_context,
        "uploaded_json_context": uploaded_json_context if uploaded_json_context and fit_context is None else None,
        "chatgpt_usage": {
            "instruction": "Copy the Markdown text generated by this app to ChatGPT.",
            "recommended_prompt_included_in_copy_text": bool(include_prompt),
        },
    }
    return output


def download_fit_on_demand(intervals_payload: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    latest_ride = intervals_payload.get("latest_ride") or {}
    activity_id = latest_ride.get("id")
    if not activity_id:
        return None, {"source": "skipped", "reason": "latest_ride_id_missing"}

    client = IntervalsClient(
        IntervalsConfig(
            api_key=secret_or_env("INTERVALS_API_KEY", ""),
            athlete_id=str(secret_or_env("INTERVALS_ATHLETE_ID", "0")),
            base_url=secret_or_env("INTERVALS_BASE_URL", "https://intervals.icu"),
            data_dir="data",
            download_fit=True,
            request_timeout_s=int_setting("FIT_REQUEST_TIMEOUT_S", 6),
            max_fit_attempts=int_setting("FIT_MAX_ATTEMPTS", 24),
        )
    )
    try:
        detail = client.get_activity_detail(str(activity_id))
        merged_ride = {**latest_ride, **client._summarize_activity_detail(detail)}
        return client.download_fit(merged_ride, detail)
    except Exception as exc:
        return None, {"source": "error", "error": str(exc)}


def analyze_uploaded_fit(builder: FitActivityContextBuilder, uploaded_fit: Any) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".fit") as tmp:
        content = uploaded_fit.getbuffer()
        if str(uploaded_fit.name).lower().endswith(".gz"):
            content = gzip.decompress(bytes(content))
        tmp.write(content)
        path = tmp.name
    try:
        return builder.build(path)
    finally:
        Path(path).unlink(missing_ok=True)


def load_uploaded_json(uploaded_file: Any) -> dict[str, Any]:
    return json.loads(uploaded_file.getvalue().decode("utf-8"))


def extract_fit_context(uploaded_json: dict[str, Any]) -> dict[str, Any]:
    if uploaded_json.get("schema_version") == "fit_activity_context.v2":
        return uploaded_json
    nested = uploaded_json.get("fit_activity_context")
    if isinstance(nested, dict):
        return nested
    return uploaded_json


def build_compact_fit_context(fit_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not fit_context:
        return None
    llm = fit_context.get("llm_summary") or {}
    segments = fit_context.get("segments") or {}
    return {
        "schema_version": "compact_fit_activity_context.v1",
        "activity": pick_nested(
            fit_context.get("activity") or {},
            {
                "summary": None,
                "load": None,
                "data_quality": None,
            },
        ),
        "physiology": pick_nested(
            fit_context.get("physiology") or {},
            {
                "critical_power": None,
                "w_prime_balance": None,
            },
        ),
        "signals": pick_nested(
            fit_context.get("signals") or {},
            {
                "duration_curves": None,
                "distributions": None,
            },
        ),
        "llm_summary": {
            "session_summary": llm.get("session_summary"),
            "key_intervals": limit_list(llm.get("key_intervals"), 8),
            "key_laps": limit_list(llm.get("key_laps"), 8),
            "metric_presence": llm.get("metric_presence"),
            "data_presence_matrix": llm.get("data_presence_matrix"),
            "interval_lap_comparison": limit_list(llm.get("interval_lap_comparison"), 8),
            "available_metrics": llm.get("available_metrics"),
        },
        "segments": {
            "auto_interval_segments": compact_segments(segments.get("auto_interval_segments"), 10),
            "user_lap_segments": compact_segments(segments.get("user_lap_segments"), 10),
        },
        "coach_context": fit_context.get("coach_context"),
        "method_notes": limit_list(fit_context.get("method_notes"), 8),
    }


def compact_download_info(info: Any) -> dict[str, Any] | None:
    if not isinstance(info, dict):
        return None
    compact = {key: value for key, value in info.items() if key != "attempted"}
    attempted = info.get("attempted")
    if isinstance(attempted, list):
        compact["attempted_count"] = len(attempted)
        compact["attempted_sample"] = attempted[:6]
    return compact


def compact_segments(items: Any, limit: int) -> list[dict[str, Any]]:
    output = []
    for item in limit_list(items, limit):
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "id": item.get("segment_id"),
                "role": item.get("segment_role"),
                "source": item.get("segment_source"),
                "selected": item.get("selected"),
                "start_s": item.get("start_s"),
                "duration_s": item.get("duration_s"),
                "has_pedaling_dynamics": item.get("has_pedaling_dynamics"),
                "power": item.get("power"),
                "heart_rate": item.get("heart_rate"),
                "movement": item.get("movement"),
                "w_prime": item.get("w_prime"),
                "w_prime_balance": item.get("w_prime_balance"),
                "wbal": item.get("wbal"),
                "pedaling_dynamics": item.get("pedaling_dynamics"),
                "classification": item.get("classification"),
                "metric_presence": item.get("metric_presence"),
            }
        )
    return output


def pick_nested(data: dict[str, Any], keys: dict[str, Any]) -> dict[str, Any]:
    picked = {}
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            picked[key] = value
    return picked


def limit_list(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def build_review_summary(
    intervals_context: dict[str, Any],
    fit_context: dict[str, Any] | None,
    manual_inputs: dict[str, Any],
) -> dict[str, Any]:
    metrics = intervals_context.get("metrics") or {}
    latest_ride = intervals_context.get("latest_ride") or {}
    fit_llm = (fit_context or {}).get("llm_summary") or {}
    return {
        "purpose": "ChatGPT cycling ride review context",
        "manual_inputs": manual_inputs,
        "intervals_today": {
            "date": metrics.get("date"),
            "fitness": metrics.get("fitness"),
            "fatigue": metrics.get("fatigue"),
            "form": metrics.get("form"),
            "condition_baseline": metrics.get("condition_baseline"),
            "condition_baseline_date": metrics.get("condition_baseline_date"),
            "weight": metrics.get("weight"),
            "resting_heart_rate": metrics.get("resting_heart_rate"),
            "sleep_score": metrics.get("sleep_score"),
            "hrv": metrics.get("hrv"),
            "ftp": metrics.get("ftp"),
            "max_heart_rate": metrics.get("max_heart_rate"),
            "w_prime_capacity": metrics.get("w_prime_capacity"),
            "w_prime_balance": metrics.get("w_prime_balance"),
            "w_prime_balance_drop": metrics.get("w_prime_balance_drop"),
            "training_load": metrics.get("training_load"),
        },
        "latest_ride": latest_ride,
        "fit_session_summary": fit_llm.get("session_summary"),
        "fit_key_intervals": fit_llm.get("key_intervals"),
        "fit_key_laps": fit_llm.get("key_laps"),
        "fit_metric_presence": fit_llm.get("metric_presence"),
        "fit_data_presence_matrix": fit_llm.get("data_presence_matrix"),
        "fit_available_metrics": fit_llm.get("available_metrics"),
    }


def render_context(context: dict[str, Any]) -> None:
    metrics = context["intervals_icu"].get("metrics") or {}
    latest = context["intervals_icu"].get("latest_ride") or {}

    render_metrics(metrics)

    st.subheader("最新ライド")
    st.dataframe(compact_latest_ride(latest), hide_index=True, use_container_width=True)
    render_fetch_diagnostics(context["intervals_icu"])

    fit_context = context.get("fit_activity_context")
    if fit_context:
        st.subheader("FIT解析")
        st.success("詳細なFIT解析情報をChatGPT貼り付け用テキストに含めました。")
        st.dataframe(compact_fit_summary(fit_context), hide_index=True, use_container_width=True)
    else:
        st.warning("詳細なFIT解析情報は含まれていません。自動取得に失敗した場合は、FITファイルをアップロードしてください。")

    json_text = json.dumps(context, ensure_ascii=False, indent=2)
    paste_text = build_paste_text(context)
    st.subheader("ChatGPTへ送るテキスト")
    st.caption(f"貼り付け文字数: 約{len(paste_text):,}文字")
    render_copy_box(paste_text)
    st.download_button(
        "JSONをダウンロード",
        data=json_text,
        file_name=f"cycling_training_context_{date.today().isoformat()}.json",
        mime="application/json",
        use_container_width=True,
    )

    with st.expander("詳細JSONを確認する"):
        st.json(context, expanded=False)


def render_metrics(metrics: dict[str, Any]) -> None:
    items = [
        ("フィットネス", display(metrics.get("fitness"))),
        ("ファティーグ", display(metrics.get("fatigue"))),
        ("フォーム", display(metrics.get("form"))),
        ("体重", display(metrics.get("weight"), "kg")),
        ("安静時心拍", display(metrics.get("resting_heart_rate"), "bpm")),
        ("睡眠スコア", display(metrics.get("sleep_score"))),
        ("HRV", display(metrics.get("hrv"), "ms")),
        ("FTP", display(metrics.get("ftp"), "W")),
        ("W′設定最大", display(metrics.get("w_prime_capacity"), "kJ")),
        ("W′bal", display_wbal(metrics)),
        ("最大心拍", display(metrics.get("max_heart_rate"), "bpm")),
    ]
    html = ['<div class="metric-grid">']
    for label, value in items:
        html.append(
            '<div class="metric-item">'
            f'<div class="metric-label">{escape(str(label))}</div>'
            f'<div class="metric-value">{escape(str(value))}</div>'
            '</div>'
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)
    if metrics.get("condition_baseline_date"):
        st.caption(f"コンディション基準日: {metrics.get('condition_baseline_date')} / {metrics.get('condition_baseline')}")


def render_fetch_diagnostics(intervals_context: dict[str, Any]) -> None:
    rows = []
    for activity in (intervals_context.get("recent_activities") or [])[:5]:
        rows.append(
            {
                "日付": activity.get("date"),
                "開始時刻": activity.get("start_time"),
                "名前": activity.get("name"),
                "ID": activity.get("id"),
            }
        )
    with st.expander("取得診断"):
        st.write(
            {
                "app_version": APP_VERSION,
                "fetch_window": intervals_context.get("fetch_window"),
                "metrics": intervals_context.get("metrics"),
                "wellness_debug": intervals_context.get("wellness_debug"),
                "fit_auto_download": intervals_context.get("fit_auto_download"),
            }
        )
        if rows:
            st.dataframe(rows, hide_index=True, use_container_width=True)
        else:
            st.info("Intervals.icu APIからアクティビティ候補を取得できていません。")


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.25rem;
        }
        .app-title {
            font-size: 1.8rem;
            line-height: 1.25;
            font-weight: 700;
            margin: 0 0 1.2rem;
        }
        .metric-grid {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 0.65rem;
            margin: 0.4rem 0 1.15rem;
        }
        .metric-item {
            min-width: 0;
        }
        .metric-label {
            color: #586174;
            font-size: 0.72rem;
            line-height: 1.2;
            margin-bottom: 0.18rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .metric-value {
            color: #313342;
            font-size: 1.08rem;
            line-height: 1.2;
            font-weight: 650;
            white-space: nowrap;
        }
        @media (max-width: 640px) {
            .block-container {
                padding-left: 0.85rem;
                padding-right: 0.85rem;
                padding-top: 0.8rem;
            }
            .app-title {
                font-size: 1.15rem;
                margin-bottom: 0.9rem;
            }
            .metric-grid {
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 0.55rem 0.7rem;
            }
            .metric-label {
                font-size: 0.64rem;
            }
            .metric-value {
                font-size: 0.86rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def compact_latest_ride(latest: dict[str, Any]) -> list[dict[str, Any]]:
    if not latest:
        return [{"項目": "状態", "値": "最新ライド情報なし"}]
    rows = [
        ("日付", format_date(latest.get("date"))),
        ("名前", latest.get("name")),
        ("移動時間", format_minutes(latest.get("moving_time"))),
        ("距離", format_km(latest.get("distance"))),
        ("トレーニング負荷", format_tss(latest.get("training_load"))),
        ("平均パワー", format_int_unit(latest.get("average_watts"), "W")),
        ("正規化/加重パワー", format_int_unit(latest.get("weighted_average_watts"), "W")),
        ("平均心拍", format_int_unit(latest.get("average_heartrate"), "bpm")),
    ]
    return [{"項目": label, "値": value} for label, value in rows if value not in (None, "")]


def compact_fit_summary(fit_context: dict[str, Any]) -> list[dict[str, Any]]:
    summary = ((fit_context or {}).get("llm_summary") or {}).get("session_summary") or {}
    activity = (fit_context or {}).get("activity") or {}
    load = activity.get("load") or {}
    activity_summary = activity.get("summary") or {}
    if not summary:
        return [{"項目": "状態", "値": "FIT解析サマリーなし"}]
    rows = [
        ("時間", format_minutes(summary.get("duration_s"))),
        ("距離", format_km(summary.get("distance_m"))),
        ("獲得標高", format_int_unit(activity_summary.get("total_ascent_m"), "m")),
        ("仕事量", format_int_unit(summary.get("total_work_kj"), "kJ")),
        ("平均パワー", format_int_unit(summary.get("mean_power_w"), "W")),
        ("最大パワー", format_int_unit(summary.get("max_power_w"), "W")),
        ("加重パワー", format_int_unit(summary.get("weighted_power_w"), "W")),
        ("強度比", format_decimal(summary.get("intensity_ratio"), 2)),
        ("負荷スコア", format_int(summary.get("session_load_score"))),
        ("20分パワー", format_int_unit(load.get("best_20min_power_w"), "W")),
        ("20分W/kg", format_decimal(load.get("best_20min_power_to_mass_wkg"), 2)),
        ("平均心拍", format_int_unit(summary.get("mean_heart_rate_bpm"), "bpm")),
        ("最大心拍", format_int_unit(activity_summary.get("max_heart_rate_bpm"), "bpm")),
        ("平均ケイデンス", format_int_unit(summary.get("mean_cadence_rpm"), "rpm")),
    ]
    return [{"項目": label, "値": value} for label, value in rows if value not in (None, "")]


def format_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).split("T")[0]
    try:
        return datetime.fromisoformat(text).strftime("%Y/%m/%d")
    except ValueError:
        return text.replace("-", "/")


def format_minutes(value: Any) -> str | None:
    number_value = to_float(value)
    if number_value is None:
        return None
    return f"{round(number_value / 60):.0f}分"


def format_duration_label(seconds: int | float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(round(seconds / 60))}分"
    return f"{seconds / 3600:.1f}h"


def format_time_range(start_s: Any, end_s: Any) -> str:
    start = to_float(start_s)
    end = to_float(end_s)
    if start is None or end is None:
        return "-"
    return f"{format_duration_label(start)}-{format_duration_label(end)}"


def format_km(value: Any) -> str | None:
    number_value = to_float(value)
    if number_value is None:
        return None
    return f"{number_value / 1000:.1f}km"


def format_tss(value: Any) -> str | None:
    number_value = to_float(value)
    if number_value is None:
        return None
    return f"{round(number_value):.0f} TSS"


def format_int_unit(value: Any, unit: str) -> str | None:
    number_value = to_float(value)
    if number_value is None:
        return None
    return f"{round(number_value):.0f}{unit}"


def format_int(value: Any) -> str | None:
    number_value = to_float(value)
    if number_value is None:
        return None
    return f"{round(number_value):.0f}"


def format_decimal(value: Any, digits: int) -> str | None:
    number_value = to_float(value)
    if number_value is None:
        return None
    return f"{number_value:.{digits}f}"


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        return float(match.group(0)) if match else None


def to_kmh_value(value: Any) -> float | None:
    number_value = to_float(value)
    if number_value is None:
        return None
    if number_value < 20:
        return number_value * 3.6
    return number_value


def summarize_histogram(rows: Any, unit: str, limit: int = 12) -> str | None:
    if not isinstance(rows, list) or not rows:
        return None
    total = sum(to_float(row.get("seconds")) or 0 for row in rows if isinstance(row, dict))
    parts = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        seconds = to_float(row.get("seconds"))
        if seconds is None:
            continue
        pct = seconds / total * 100 if total else None
        label = f"{format_int(row.get('from'))}-{format_int(row.get('to'))}{unit}"
        value = f"{round(seconds / 60):.0f}分"
        if pct is not None:
            value += f"/{pct:.0f}%"
        parts.append(f"{label}: {value}")
    if len(rows) > limit:
        parts.append(f"他{len(rows) - limit}区間")
    return ", ".join(parts) if parts else None


def render_copy_box(text: str) -> None:
    escaped_text = escape(text)
    components.html(
        f"""
        <div style="font-family: sans-serif;">
          <button id="copyButton" style="width:100%;padding:12px 16px;border:0;border-radius:6px;background:#ff4b4b;color:white;font-weight:700;font-size:16px;">
            ワンタップでコピー
          </button>
          <div id="copyStatus" style="margin:8px 0;color:#0a7f32;font-size:14px;"></div>
          <textarea id="copyText" readonly style="width:100%;height:280px;box-sizing:border-box;border:1px solid #ddd;border-radius:6px;padding:12px;font-size:13px;line-height:1.45;">{escaped_text}</textarea>
        </div>
        <script>
        const button = document.getElementById("copyButton");
        const status = document.getElementById("copyStatus");
        const textArea = document.getElementById("copyText");
        button.addEventListener("click", async () => {{
          textArea.focus();
          textArea.select();
          try {{
            await navigator.clipboard.writeText(textArea.value);
            status.textContent = "コピーしました。ChatGPTに貼り付けてください。";
          }} catch (err) {{
            document.execCommand("copy");
            status.textContent = "コピーしました。ChatGPTに貼り付けてください。";
          }}
        }});
        </script>
        """,
        height=390,
    )


def build_paste_text(context: dict[str, Any]) -> str:
    prompt = context.get("chatgpt_usage", {}).get("recommended_prompt") or build_chatgpt_prompt()
    return f"{prompt}\n\n{build_review_markdown(context)}"


def build_review_markdown(context: dict[str, Any]) -> str:
    intervals = context.get("intervals_icu") or {}
    metrics = intervals.get("metrics") or {}
    ride = intervals.get("latest_ride") or {}
    manual = context.get("manual_inputs") or {}
    athlete_inputs = context.get("athlete_inputs") or {}
    fit = context.get("fit_activity_context") or {}
    fit_llm = (fit.get("llm_summary") or {}) if isinstance(fit, dict) else {}
    session = fit_llm.get("session_summary") or {}

    lines = [
        "# 評価対象データ",
        "",
        "## 主観入力",
        f"- ライド前RPE: {display(manual.get('pre_ride_rpe_1_10'))}",
        f"- ライド後RPE: {display(manual.get('post_ride_rpe_1_10'))}",
        f"- メモ: {display(manual.get('subjective_note'))}",
        "",
        "## Intervals.icu コンディション",
        "|項目|値|",
        "|---|---:|",
        f"|フィットネス|{display(metrics.get('fitness'))}|",
        f"|ファティーグ|{display(metrics.get('fatigue'))}|",
        f"|フォーム|{display(metrics.get('form'))}|",
        f"|体重|{display(metrics.get('weight'), 'kg')}|",
        f"|安静時心拍|{display(metrics.get('resting_heart_rate'), 'bpm')}|",
        f"|睡眠スコア|{display(metrics.get('sleep_score'))}|",
        f"|HRV|{display(metrics.get('hrv'), 'ms')}|",
        f"|FTP|{display(metrics.get('ftp'), 'W')}|",
        f"|W′設定最大|{display(athlete_inputs.get('w_prime_kj'), 'kJ')}|",
        f"|W′bal|{display_wbal(metrics)}|",
        f"|最大心拍|{display(metrics.get('max_heart_rate'), 'bpm')}|",
        f"|基準|{display(metrics.get('condition_baseline'))} / {display(metrics.get('condition_baseline_date'))}|",
        "",
        "## 最新ライド",
        "|項目|値|",
        "|---|---:|",
    ]
    for row in compact_latest_ride(ride):
        lines.append(f"|{row['項目']}|{row['値']}|")

    lines.extend(
        [
            "",
            "## FIT セッション要約",
            "|項目|値|",
            "|---|---:|",
        ]
    )
    for row in compact_fit_summary(fit):
        lines.append(f"|{row['項目']}|{row['値']}|")

    physiology_lines = markdown_physiology(fit)
    if physiology_lines:
        lines.extend(["", "## CP / W′"])
        lines.extend(physiology_lines)

    curve_lines = markdown_duration_curves(fit)
    if curve_lines:
        lines.extend(["", "## Duration Curve 代表点"])
        lines.extend(curve_lines)

    distribution_lines = markdown_distributions(fit)
    if distribution_lines:
        lines.extend(["", "## 分布"])
        lines.extend(distribution_lines)

    fit_segments = (fit.get("segments") or {}) if isinstance(fit, dict) else {}
    lines.extend(["", "## 主要区間"])
    lines.extend(markdown_interval_table(fit_segments.get("auto_interval_segments") or fit_llm.get("key_intervals")))

    lap_lines = markdown_lap_table(fit_segments.get("user_lap_segments") or fit_llm.get("key_laps"))
    if lap_lines:
        lines.extend(["", "## User Lap"])
        lines.extend(lap_lines)

    comparison = fit_llm.get("interval_lap_comparison") or []
    if comparison:
        lines.extend(["", "## 区間とLapの対応"])
        lines.extend(markdown_interval_lap_comparison(comparison))

    coach_lines = markdown_coach_context(fit.get("coach_context") if isinstance(fit, dict) else None)
    if coach_lines:
        lines.extend(["", "## Coach Context"])
        lines.extend(coach_lines)

    available = fit_llm.get("available_metrics") or []
    if available:
        lines.extend(["", "## 利用可能な指標", ", ".join(format_available_metrics(available)[:40])])

    presence = fit_llm.get("metric_presence")
    if presence:
        lines.extend(["", "## 指標の有無", "```json", json.dumps(presence, ensure_ascii=False, separators=(",", ":")), "```"])

    if session:
        lines.extend(["", "## FIT session_summary JSON", "```json", json.dumps(session, ensure_ascii=False, separators=(",", ":")), "```"])

    return "\n".join(lines)


def markdown_physiology(fit: dict[str, Any] | None) -> list[str]:
    physiology = ((fit or {}).get("physiology") or {}) if isinstance(fit, dict) else {}
    critical = physiology.get("critical_power") or {}
    wbal = (physiology.get("w_prime_balance") or {}).get("summary") or {}
    rows = [
        "|項目|値|",
        "|---|---:|",
        f"|CP|{display(critical.get('critical_power_w'), 'W')}|",
        f"|W′設定最大|{display(critical.get('w_prime_kj'), 'kJ')}|",
        f"|CP超過時間|{display(critical.get('time_above_cp_s'), '秒')}|",
        f"|CP超過仕事量|{display(critical.get('work_above_cp_kj'), 'kJ')}|",
        f"|Severe bout数|{display(critical.get('severe_domain_bout_count'))}|",
        f"|最低W′bal|{display(wbal.get('min_balance_kj'), 'kJ')} / {display(wbal.get('min_balance_pct'), '%')}|",
        f"|終了時W′bal|{display(wbal.get('end_balance_kj'), 'kJ')} / {display(wbal.get('end_balance_pct'), '%')}|",
        f"|W′消費|{display(wbal.get('total_depletion_kj'), 'kJ')}|",
        f"|W′回復|{display(wbal.get('total_recovery_kj'), 'kJ')}|",
        f"|W′20%以下時間|{display(wbal.get('time_below_20pct_s'), '秒')}|",
    ]
    useful = [row for row in rows[2:] if not row.endswith("|-|") and "|- / -|" not in row]
    return rows[:2] + useful if useful else []


def markdown_duration_curves(fit: dict[str, Any] | None) -> list[str]:
    curves = (((fit or {}).get("signals") or {}).get("duration_curves") or {}) if isinstance(fit, dict) else {}
    if not curves:
        return []
    metric_labels = {
        "power": ("パワー", "W"),
        "heart_rate": ("心拍", "bpm"),
        "cadence": ("ケイデンス", "rpm"),
        "velocity": ("速度", "km/h"),
    }
    durations = [1, 5, 10, 30, 60, 300, 1200, 3600]
    rows = ["|時間|パワー|心拍|ケイデンス|速度|", "|---:|---:|---:|---:|---:|"]
    point_maps = {}
    for key in metric_labels:
        points = (curves.get(key) or {}).get("representative_points") or []
        point_maps[key] = {int(point.get("duration_s")): point.get("best_average") for point in points if point.get("duration_s") is not None}
    for duration in durations:
        if not any(duration in point_maps[key] for key in metric_labels):
            continue
        rows.append(
            "|{duration}|{power}|{hr}|{cadence}|{velocity}|".format(
                duration=format_duration_label(duration),
                power=format_int_unit(point_maps["power"].get(duration), "W") or "-",
                hr=format_int_unit(point_maps["heart_rate"].get(duration), "bpm") or "-",
                cadence=format_int_unit(point_maps["cadence"].get(duration), "rpm") or "-",
                velocity=format_unit(format_decimal(to_kmh_value(point_maps["velocity"].get(duration)), 1), "km/h") or "-",
            )
        )
    return rows if len(rows) > 2 else []


def markdown_distributions(fit: dict[str, Any] | None) -> list[str]:
    distributions = (((fit or {}).get("signals") or {}).get("distributions") or {}) if isinstance(fit, dict) else {}
    if not distributions:
        return []
    lines = []
    power = summarize_histogram(distributions.get("power_w"), "W")
    heart_rate = summarize_histogram(distributions.get("heart_rate_bpm"), "bpm")
    cadence = summarize_histogram(distributions.get("cadence_rpm"), "rpm")
    speed = summarize_histogram(distributions.get("speed_kmh"), "km/h")
    if power:
        lines.extend(["### パワー分布", power])
    if heart_rate:
        lines.extend(["### 心拍分布", heart_rate])
    if cadence:
        lines.extend(["### ケイデンス分布", cadence])
    if speed:
        lines.extend(["### 速度分布", speed])
    return lines


def markdown_lap_table(laps: Any) -> list[str]:
    if not isinstance(laps, list) or not laps:
        return []
    rows = [
        "|ID|区間|時間|平均/加重P|CP比|仕事量|HR|ケイデンス/速度|EF/デカップリング|PD|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in laps[:8]:
        if not isinstance(item, dict):
            continue
        power = item.get("power") or {}
        hr = item.get("heart_rate") or {}
        move = item.get("movement") or {}
        rows.append(format_segment_row(item, power, hr, move, include_role=False, include_wbal=False))
    return rows if len(rows) > 2 else []


def markdown_interval_lap_comparison(comparison: Any) -> list[str]:
    if not isinstance(comparison, list) or not comparison:
        return []
    rows = ["|区間|Lap|重複|区間平均P|Lap平均P|差|", "|---|---|---:|---:|---:|---:|"]
    for item in comparison[:8]:
        if not isinstance(item, dict):
            continue
        rows.append(
            "|{interval}|{lap}|{overlap}|{ip}|{lp}|{delta}|".format(
                interval=display(item.get("interval_id")),
                lap=display(item.get("lap_id")),
                overlap=format_minutes(item.get("overlap_s")) or display(item.get("overlap_s")),
                ip=format_int_unit(item.get("interval_mean_power_w"), "W") or "-",
                lp=format_int_unit(item.get("lap_mean_power_w"), "W") or "-",
                delta=format_int_unit(item.get("mean_power_delta_w"), "W") or "-",
            )
        )
    return rows


def markdown_coach_context(coach_context: Any) -> list[str]:
    if not isinstance(coach_context, dict) or not coach_context:
        return []
    labels = {
        "session_type_guess": "セッション推定",
        "main_stimulus": "主刺激",
        "fatigue_signal": "疲労シグナル",
        "selected_work_duration_s": "選択work合計",
        "scope_note": "スコープ",
    }
    rows = []
    for key, label in labels.items():
        value = coach_context.get(key)
        if value in (None, "", [], {}):
            continue
        if key.endswith("_s"):
            value = format_minutes(value) or value
        rows.append(f"- {label}: {value}")
    return rows


def markdown_interval_table(intervals: Any) -> list[str]:
    rows = [
        "|ID|役割|区間|時間|平均/加重P|CP比|仕事量|HR|ケイデンス/速度|EF/デカップリング|W'|分類|PD|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    if not isinstance(intervals, list) or not intervals:
        rows.append("|-|主要区間なし|-|-|-|-|-|-|-|-|-|-|-|")
        return rows
    for item in intervals[:10]:
        if not isinstance(item, dict):
            continue
        power = item.get("power") or {}
        hr = item.get("heart_rate") or {}
        move = item.get("movement") or {}
        rows.append(format_segment_row(item, power, hr, move, include_role=True, include_wbal=True))
    return rows


def format_segment_row(
    item: dict[str, Any],
    power: dict[str, Any],
    heart_rate: dict[str, Any],
    movement: dict[str, Any],
    include_role: bool,
    include_wbal: bool,
) -> str:
    row = {
        "id": display(item.get("segment_id") or item.get("id")),
        "role": display(item.get("segment_role") or item.get("role")),
        "range": format_time_range(item.get("start_s"), item.get("end_s")),
        "dur": format_minutes(item.get("duration_s")) or display(item.get("duration_s")),
        "power": "/".join(
            value
            for value in [
                format_int_unit(first_existing(power, "mean_w", "mean_power_w", "avg_w") or first_existing(item, "mean_power_w", "avg_power_w"), "W"),
                format_int_unit(first_existing(power, "weighted_w", "weighted_power_w", "normalized_w") or first_existing(item, "weighted_power_w", "normalized_power_w"), "W"),
            ]
            if value
        )
        or "-",
        "cp": format_decimal(first_existing(power, "cp_ratio") or first_existing(item, "cp_ratio"), 2) or "-",
        "work": format_int_unit(first_existing(power, "work_kj"), "kJ") or "-",
        "hr": "/".join(
            value
            for value in [
                format_int_unit(first_existing(heart_rate, "mean_bpm", "mean_heart_rate_bpm", "avg_bpm") or first_existing(item, "mean_heart_rate_bpm", "avg_heart_rate_bpm"), "bpm"),
                format_int_unit(first_existing(heart_rate, "max_bpm", "max_heart_rate_bpm") or first_existing(item, "max_heart_rate_bpm"), "bpm"),
            ]
            if value
        )
        or "-",
        "move": "/".join(
            value
            for value in [
                format_int_unit(first_existing(movement, "mean_cadence_rpm", "cadence_rpm") or first_existing(item, "mean_cadence_rpm"), "rpm"),
                format_unit(format_decimal(first_existing(movement, "mean_speed_kmh") or first_existing(item, "mean_speed_kmh"), 1), "km/h"),
            ]
            if value
        )
        or "-",
        "decoupling": format_efficiency_decoupling(item, heart_rate),
        "wbal": display_interval_wbal(item),
        "class": "/".join(
            value
            for value in [
                display(first_existing(item.get("classification") or {}, "effort_type") or item.get("effort_type")),
                display(first_existing(item.get("classification") or {}, "execution_pattern") or item.get("execution_pattern")),
            ]
            if value != "-"
        )
        or "-",
        "pd": "あり" if item.get("has_pedaling_dynamics") else "なし",
    }
    if include_role:
        return "|{id}|{role}|{range}|{dur}|{power}|{cp}|{work}|{hr}|{move}|{decoupling}|{wbal}|{class}|{pd}|".format(**row)
    if include_wbal:
        return "|{id}|{range}|{dur}|{power}|{cp}|{work}|{hr}|{move}|{decoupling}|{wbal}|{pd}|".format(**row)
    return "|{id}|{range}|{dur}|{power}|{cp}|{work}|{hr}|{move}|{decoupling}|{pd}|".format(**row)


def format_available_metrics(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        output = []
        for key, item in value.items():
            if isinstance(item, list):
                output.extend(f"{key}.{child}" for child in item)
            elif isinstance(item, dict):
                output.extend(f"{key}.{child}" for child, present in item.items() if present)
            elif item:
                output.append(str(key))
        return output
    return [str(value)]


def first_existing(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def format_unit(value: Any, unit: str) -> str | None:
    if value in (None, ""):
        return None
    return f"{value}{unit}"


def display_wbal(metrics: dict[str, Any]) -> str:
    parts = []
    balance = format_int_unit(metrics.get("w_prime_balance"), "kJ")
    drop = format_int_unit(metrics.get("w_prime_balance_drop"), "kJ")
    if balance:
        parts.append(balance)
    if drop:
        parts.append(f"drop {drop}")
    return " / ".join(parts) if parts else "-"


def display_interval_wbal(item: dict[str, Any]) -> str:
    w_prime = item.get("w_prime") or {}
    if not isinstance(w_prime, dict):
        w_prime = {}
    values = [
        format_int_unit(first_existing(w_prime, "min_kj", "minimum_kj", "min_balance_kj"), "kJ"),
        format_int_unit(first_existing(w_prime, "end_kj", "ending_kj", "end_balance_kj"), "kJ"),
        format_int_unit(first_existing(item, "w_prime_balance", "wbal", "wbal_kj"), "kJ"),
    ]
    values = [value for value in values if value]
    return "/".join(values) if values else "-"


def format_efficiency_decoupling(item: dict[str, Any], heart_rate: dict[str, Any]) -> str:
    ef = format_decimal(
        first_existing(heart_rate, "efficiency_factor_w_per_bpm", "efficiency_factor", "ef")
        or first_existing(item, "efficiency_factor_w_per_bpm", "efficiency_factor", "ef"),
        2,
    )
    decoupling = format_percent(
        first_existing(heart_rate, "decoupling_pct", "decoupling_percent", "decoupling")
        or first_existing(item, "decoupling_pct", "decoupling_percent", "decoupling"),
        1,
    )
    parts = []
    if ef:
        parts.append(f"EF {ef}")
    if decoupling:
        parts.append(f"Dec {decoupling}")
    return " / ".join(parts) if parts else "-"


def format_percent(value: Any, digits: int) -> str | None:
    number_value = to_float(value)
    if number_value is None:
        return None
    return f"{number_value:.{digits}f}%"


def build_chatgpt_prompt() -> str:
    return (
        "あなたは持久系パフォーマンスコーチです。\n"
        "以下のMarkdownデータだけを根拠に、アスリート本人向けの日本語レビューを作成してください。\n"
        "Intervals.icuのフィットネス、ファティーグ、フォームは、アクティビティ値または当日ライド後値とTSSから差し戻したライド直前推定値です。\n"
        "RPEとメモは本人の主観情報として扱ってください。\n\n"
        "読み取りルール:\n"
        "- データ内にない事実は推測で補わないでください。\n"
        "- 体重、安静時心拍、睡眠スコア、HRV、フィットネス、ファティーグ、フォーム、W′balを評価の前提に利用してください。\n"
        "- W′は設定最大値、最低W′bal、W′ドロップ、残量を比較し、高強度でどこまで掘れたか、どれだけ余ったかを評価してください。\n"
        "- 利用可能な指標と指標の有無を先に確認し、使える値だけで評価してください。\n"
        "- missing / removed / not_applicable / null / sample_count=0 の指標は使わないでください。\n"
        "- W′関連値はモデル推定として扱い、実測値のように断定しないでください。\n"
        "- CPは入力基準値であり、条件やポジションにより実効値が異なる可能性があります。\n"
        "- 医療・診断ではなく、トレーニング上の示唆に限定してください。\n"
        "- 表と短い箇条書きを使い、要点を短く伝えてください。\n\n"
        "出力順:\n"
        "1. 結論\n"
        "2. セッション構造\n"
        "3. 主要区間表\n"
        "4. 指標別評価\n"
        "5. 良かった点\n"
        "6. 改善点\n"
        "7. 次回提案\n"
        "8. 判断できないこと\n"
        "9. 追加で必要な情報\n\n"
        "主要区間表には、区間ID、時間、mean/weighted power、HR、cadence/speed、EF/デカップリング、W′、pedaling dynamics有無を、利用可能な範囲で入れてください。"
    )


def sanitize_latest_ride(ride: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ride:
        return None
    return {key: value for key, value in ride.items() if key not in {"fit_path", "file", "filename"}}


def secret_or_env(name: str, default: Any = None) -> Any:
    try:
        if name in st.secrets:
            return st.secrets[name]
    except FileNotFoundError:
        pass
    return os.getenv(name, default)


def int_setting(name: str, default: int) -> int:
    try:
        return int(secret_or_env(name, default))
    except (TypeError, ValueError):
        return default


def float_setting(name: str, default: float) -> float:
    try:
        return float(secret_or_env(name, default))
    except (TypeError, ValueError):
        return default


def bool_setting(name: str, default: bool) -> bool:
    value = secret_or_env(name, default)
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def display(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "-"
    return f"{value} {suffix}".strip()


def run_app() -> None:
    try:
        main()
    except Exception as exc:
        try:
            st.set_page_config(page_title="トレーニングコーチJSONジェネレータ", layout="wide")
        except Exception:
            pass
        st.error("アプリ起動時にエラーが発生しました。")
        st.write({"error_type": type(exc).__name__, "message": str(exc)})
        st.info("この画面が出ている場合は、表示された error_type と message を共有してください。")


if __name__ == "__main__":
    run_app()
