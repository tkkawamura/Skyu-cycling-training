from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

from services.fit_context import AthleteInputs, FitActivityContextBuilder
from services.intervals import IntervalsClient, IntervalsConfig, IntervalsError


load_dotenv()


def main() -> None:
    st.set_page_config(page_title="Cycling Training Context", layout="wide")
    st.title("Cycling Training Context Generator")
    st.caption("Intervals.icu metrics and FIT activity context JSON for ChatGPT review.")

    st.info(
        "This app does not call the OpenAI API. It generates a JSON file that you can upload to your own ChatGPT account."
    )
    st.caption("For detailed ride analysis, download the original FIT file from Intervals.icu and upload it below.")

    with st.sidebar:
        st.header("Settings")
        lookback_days = st.number_input("Lookback days", min_value=1, max_value=180, value=int_setting("LOOKBACK_DAYS", 21))
        cp = st.number_input("CP W", min_value=0, max_value=800, value=int_setting("ATHLETE_CRITICAL_POWER_W", 250))
        body_mass = st.number_input("Body mass kg", min_value=0.0, max_value=200.0, value=float_setting("ATHLETE_BODY_MASS_KG", 63.0))
        max_hr = st.number_input("Max HR bpm", min_value=0, max_value=240, value=int_setting("ATHLETE_MAX_HEART_RATE_BPM", 178))
        w_prime = st.number_input("W' kJ", min_value=0.0, max_value=80.0, value=float_setting("ATHLETE_W_PRIME_KJ", 0.0))
        rpe = st.number_input("RPE", min_value=0.0, max_value=10.0, value=0.0, step=0.5)
        subjective_note = st.text_area("Memo", value="", placeholder="睡眠、疲労感、補給、脚の感覚など")
        include_prompt = st.checkbox("Include ChatGPT prompt", value=True)
        allow_manual_fit = st.checkbox("Allow manual FIT upload fallback", value=True)

    intervals_snapshot = load_intervals_snapshot(int(lookback_days))
    render_startup_intervals_snapshot(intervals_snapshot)

    uploaded_file = None
    if allow_manual_fit:
        st.markdown(
            "Intervals.icu activity page -> Data tab -> Original FIT file -> download, then upload that `.fit` file here."
        )
        uploaded_file = st.file_uploader("Original FIT file or generated JSON", type=["fit", "json"])

    if st.button("Generate JSON", type="primary", use_container_width=True):
        with st.spinner("Collecting Intervals.icu data and analyzing FIT..."):
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
                    "rpe_0_10": float(rpe) if rpe else None,
                    "subjective_note": subjective_note.strip() or None,
                },
                intervals_snapshot=intervals_snapshot,
                include_prompt=include_prompt,
            )
        render_context(context)


def load_intervals_snapshot(lookback_days: int) -> dict[str, Any]:
    today = date.today()
    oldest = today - timedelta(days=lookback_days)
    data_dir = "data"
    intervals_client = IntervalsClient(
        IntervalsConfig(
            api_key=secret_or_env("INTERVALS_API_KEY", ""),
            athlete_id=str(secret_or_env("INTERVALS_ATHLETE_ID", "0")),
            base_url=secret_or_env("INTERVALS_BASE_URL", "https://intervals.icu"),
            data_dir=data_dir,
            download_fit=bool_setting("FIT_DOWNLOAD", True),
        )
    )
    warnings = []
    try:
        payload = intervals_client.collect_dashboard(oldest=oldest, newest=today)
        source = "intervals"
    except IntervalsError as exc:
        payload = intervals_client.sample_dashboard(oldest=oldest, newest=today)
        source = "sample"
        warnings.append(str(exc))
    return {
        "payload": payload,
        "source": source,
        "warnings": warnings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetch_timing": "streamlit_app_session_start_or_rerun_before_json_generation",
    }


def render_startup_intervals_snapshot(snapshot: dict[str, Any]) -> None:
    metrics = (snapshot.get("payload") or {}).get("metrics") or {}
    if snapshot.get("source") == "sample":
        st.warning("Intervals.icu metrics are sample data because live fetch failed.")
    st.caption(f"Intervals.icu snapshot fetched at {snapshot.get('fetched_at')}")
    cols = st.columns(6)
    cols[0].metric("Fitness", display(metrics.get("fitness")))
    cols[1].metric("Fatigue", display(metrics.get("fatigue")))
    cols[2].metric("Form", display(metrics.get("form")))
    cols[3].metric("Weight", display(metrics.get("weight"), "kg"))
    cols[4].metric("FTP", display(metrics.get("ftp"), "W"))
    cols[5].metric("eFTP", display(metrics.get("eftp"), "W"))


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
    fit_path = intervals_payload.get("fit_path")
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

    intervals_context = {
        "source": intervals_source,
        "fetched_at": intervals_snapshot.get("fetched_at"),
        "fetch_timing": intervals_snapshot.get("fetch_timing"),
        "condition_metrics_purpose": "These Intervals.icu values are fetched automatically at app load/rerun and should be used as the condition baseline for the ride review.",
        "metrics": intervals_payload.get("metrics"),
        "latest_ride": sanitize_latest_ride(intervals_payload.get("latest_ride")),
        "trend": intervals_payload.get("trend"),
        "recent_activities": intervals_payload.get("recent_activities"),
        "fit_auto_downloaded": bool(fit_path),
        "fit_auto_download": intervals_payload.get("fit_download_info"),
        "fit_analysis_source": fit_source,
    }
    output = {
        "schema_version": "cycling_training_review_context.v2",
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "streamlit_community_cloud",
            "base_context": "fit_activity_context.v2",
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
        "llm_review_summary": build_review_summary(intervals_context, fit_context, manual_inputs),
        "fit_activity_context": fit_context,
        "uploaded_json_context": uploaded_json_context if uploaded_json_context and fit_context is None else None,
        "chatgpt_usage": {
            "instruction": "Copy this JSON to ChatGPT. Use review_prompt as the base instruction.",
            "recommended_prompt": build_chatgpt_prompt() if include_prompt else None,
        },
    }
    return output


def analyze_uploaded_fit(builder: FitActivityContextBuilder, uploaded_fit: Any) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".fit") as tmp:
        tmp.write(uploaded_fit.getbuffer())
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
            "weight": metrics.get("weight"),
            "ftp": metrics.get("ftp"),
            "eftp": metrics.get("eftp"),
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

    cols = st.columns(6)
    cols[0].metric("Fitness", display(metrics.get("fitness")))
    cols[1].metric("Fatigue", display(metrics.get("fatigue")))
    cols[2].metric("Form", display(metrics.get("form")))
    cols[3].metric("Weight", display(metrics.get("weight"), "kg"))
    cols[4].metric("FTP", display(metrics.get("ftp"), "W"))
    cols[5].metric("eFTP", display(metrics.get("eftp"), "W"))

    st.subheader("Latest Ride")
    st.json(latest, expanded=False)

    fit_context = context.get("fit_activity_context")
    if fit_context:
        st.subheader("FIT Activity Context")
        st.success("Detailed FIT context is included in the downloaded JSON.")
        st.json(fit_context.get("llm_summary", fit_context), expanded=False)
    else:
        st.warning("Detailed FIT context is not included. Upload a FIT file or generated JSON to include the attached-style analysis.")

    json_text = json.dumps(context, ensure_ascii=False, indent=2)
    paste_text = build_paste_text(context)
    st.subheader("Copy & Paste to ChatGPT")
    st.text_area(
        "Copy this text and paste it into ChatGPT",
        value=paste_text,
        height=360,
        help="iPhoneでは欄内を長押しして全選択、コピーしてChatGPTへ貼り付けてください。",
    )
    st.download_button(
        "Download JSON for ChatGPT",
        data=json_text,
        file_name=f"cycling_training_context_{date.today().isoformat()}.json",
        mime="application/json",
        use_container_width=True,
    )

    st.subheader("JSON Preview")
    st.json(context, expanded=False)


def build_paste_text(context: dict[str, Any]) -> str:
    compact_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    prompt = context.get("chatgpt_usage", {}).get("recommended_prompt") or build_chatgpt_prompt()
    return (
        f"{prompt}\n\n"
        "以下が評価対象のJSONです。\n"
        "```json\n"
        f"{compact_json}\n"
        "```"
    )


def build_chatgpt_prompt() -> str:
    return (
        "あなたは持久系パフォーマンスコーチです。\n"
        "このJSON内のFIT解析情報を主な根拠に、アスリート本人向けの日本語レビューを作成してください。\n"
        "このアプリのJSONでは、FIT解析JSONは `fit_activity_context` に格納されています。\n"
        "`intervals_icu.metrics` のフィットネス、ファティーグ、フォーム、体重、FTP、eFTPは、アプリ起動/再実行時に自動取得されたコンディション前提として必ず確認し、ライド結果の評価に反映してください。\n"
        "`manual_inputs` にRPEやメモがあれば、本人の主観情報として使ってください。\n\n"
        "共通の読み取りルール:\n"
        "- このJSONだけを根拠にし、JSON内にない事実は推測で補わないでください。\n"
        "- まず `intervals_icu.metrics` の Fitness / Fatigue / Form / weight / FTP / eFTP を確認し、コンディション前提を把握してください。\n"
        "- まず `fit_activity_context.llm_summary.metric_presence`, `fit_activity_context.llm_summary.data_presence_matrix`, `fit_activity_context.llm_summary.available_metrics` を確認し、評価に使える指標を確定してください。\n"
        "- 詳細値の真値は `fit_activity_context.activity`, `fit_activity_context.physiology`, `fit_activity_context.signals`, `fit_activity_context.segments` にあります。`llm_summary` は索引・要約として使ってください。\n"
        "- 主要work intervalは `fit_activity_context.segments.auto_interval_segments`、user/device lapは `fit_activity_context.segments.user_lap_segments` を確認してください。\n"
        "- auto interval と user lap の対応は `fit_activity_context.llm_summary.interval_lap_comparison` を参照してください。\n"
        "- `available` の値だけを評価に使ってください。`missing`, `removed`, `not_applicable`, `null`, `sample_count = 0` は推測で補わないでください。\n"
        "- `has_pedaling_dynamics` が true でも、個別指標が null / missing / sample_count 0 なら、その個別指標は使わないでください。\n"
        "- W′関連値はモデル推定として扱い、実測値のように断定しないでください。\n"
        "- CPは入力基準値であり、条件やポジションにより実効値が異なる可能性があります。\n"
        "- 単一アクティビティから、長期適応、疲労蓄積、ピーキングは断定しないでください。\n"
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
        "主要区間表には、区間ID、時間、duration、mean/weighted power、HR、cadence/speed、W′、pedaling dynamics有無を、利用可能な範囲で入れてください。\n"
        "簡単な図解が有効なら、ASCIIの時間軸や短い模式図で示してください。"
    )


def sanitize_latest_ride(ride: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ride:
        return None
    return {key: value for key, value in ride.items() if key not in {"fit_path", "file", "filename"}}


def secret_or_env(name: str, default: Any = None) -> Any:
    if name in st.secrets:
        return st.secrets[name]
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


if __name__ == "__main__":
    main()
