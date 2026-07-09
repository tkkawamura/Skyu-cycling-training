from __future__ import annotations

import json
import os
import tempfile
from html import escape
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from services.fit_context import AthleteInputs, FitActivityContextBuilder
from services.intervals import IntervalsClient, IntervalsConfig, IntervalsError


load_dotenv()


def main() -> None:
    st.set_page_config(page_title="自転車トレーニング評価JSON", layout="wide")
    st.title("自転車トレーニング評価JSONジェネレーター")

    lookback_days = int_setting("LOOKBACK_DAYS", 21)
    intervals_snapshot = load_intervals_snapshot(lookback_days)
    render_startup_intervals_snapshot(intervals_snapshot)
    metrics = (intervals_snapshot.get("payload") or {}).get("metrics") or {}

    cp = int(metrics.get("ftp") or int_setting("ATHLETE_CRITICAL_POWER_W", 250))
    body_mass = float(metrics.get("weight") or float_setting("ATHLETE_BODY_MASS_KG", 63.0))
    max_hr = int(metrics.get("max_heart_rate") or int_setting("ATHLETE_MAX_HEART_RATE_BPM", 178))
    w_prime = float_setting("ATHLETE_W_PRIME_KJ", 0.0)

    st.subheader("主観入力")
    rpe_options = ["未入力"] + list(range(1, 11))
    rpe_cols = st.columns(2)
    pre_rpe = rpe_cols[0].selectbox("ライド前RPE", rpe_options, index=0)
    post_rpe = rpe_cols[1].selectbox("ライド後RPE", rpe_options, index=0)
    subjective_note = st.text_area("メモ", value="", placeholder="睡眠、疲労感、補給、脚の感覚など")

    uploaded_file = None
    uploaded_file = st.file_uploader("FITファイルまたは生成済みJSON（自動取得できない場合のみ）", type=["fit", "json"])

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
                    "pre_ride_rpe_1_10": None if pre_rpe == "未入力" else int(pre_rpe),
                    "post_ride_rpe_1_10": None if post_rpe == "未入力" else int(post_rpe),
                    "subjective_note": subjective_note.strip() or None,
                },
                intervals_snapshot=intervals_snapshot,
                include_prompt=True,
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
    payload = apply_metric_defaults(payload)
    return {
        "payload": payload,
        "source": source,
        "warnings": warnings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetch_timing": "streamlit_app_session_start_or_rerun_before_json_generation",
    }


def apply_metric_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(payload.get("metrics") or {})
    if metrics.get("ftp") is None:
        metrics["ftp"] = int_setting("ATHLETE_CRITICAL_POWER_W", 250)
    if metrics.get("eftp") is None:
        configured_eftp = int_setting("ATHLETE_EFTP_W", 0)
        metrics["eftp"] = configured_eftp or None
    if metrics.get("max_heart_rate") is None:
        metrics["max_heart_rate"] = int_setting("ATHLETE_MAX_HEART_RATE_BPM", 178)
    payload = dict(payload)
    payload["metrics"] = metrics
    return payload


def render_startup_intervals_snapshot(snapshot: dict[str, Any]) -> None:
    metrics = (snapshot.get("payload") or {}).get("metrics") or {}
    if snapshot.get("source") == "sample":
        st.warning("Intervals.icuの取得に失敗したため、サンプル値を表示しています。")
    st.caption(f"Intervals.icu取得時刻: {snapshot.get('fetched_at')}")
    render_metrics(metrics)


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

    metrics = dict(intervals_payload.get("metrics") or {})
    if metrics.get("max_heart_rate") is None and athlete.max_heart_rate_bpm:
        metrics["max_heart_rate"] = athlete.max_heart_rate_bpm
    if metrics.get("ftp") is None and athlete.critical_power_w:
        metrics["ftp"] = athlete.critical_power_w

    intervals_context = {
        "source": intervals_source,
        "fetched_at": intervals_snapshot.get("fetched_at"),
        "fetch_timing": intervals_snapshot.get("fetch_timing"),
        "condition_metrics_purpose": "These Intervals.icu values are fetched automatically at app load/rerun and should be used as the condition baseline for the ride review.",
        "metrics": metrics,
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
            "max_heart_rate": metrics.get("max_heart_rate"),
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
    cols = st.columns(7)
    cols[0].metric("フィットネス", display(metrics.get("fitness")))
    cols[1].metric("ファティーグ", display(metrics.get("fatigue")))
    cols[2].metric("フォーム", display(metrics.get("form"), "%"))
    cols[3].metric("体重", display(metrics.get("weight"), "kg"))
    cols[4].metric("FTP", display(metrics.get("ftp"), "W"))
    cols[5].metric("eFTP", display(metrics.get("eftp"), "W"))
    cols[6].metric("最大心拍", display(metrics.get("max_heart_rate"), "bpm"))


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
    if not summary:
        return [{"項目": "状態", "値": "FIT解析サマリーなし"}]
    rows = [
        ("時間", format_minutes(summary.get("duration_s"))),
        ("距離", format_km(summary.get("distance_m"))),
        ("仕事量", format_int_unit(summary.get("total_work_kj"), "kJ")),
        ("平均パワー", format_int_unit(summary.get("mean_power_w"), "W")),
        ("加重パワー", format_int_unit(summary.get("weighted_power_w"), "W")),
        ("強度比", format_decimal(summary.get("intensity_ratio"), 2)),
        ("負荷スコア", format_int(summary.get("session_load_score"))),
        ("平均心拍", format_int_unit(summary.get("mean_heart_rate_bpm"), "bpm")),
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
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        "`intervals_icu.metrics` のフィットネス、ファティーグ、フォーム、体重、FTP、eFTP、最大心拍数は、アプリ起動/再実行時に自動取得されたコンディション前提として必ず確認し、ライド結果の評価に反映してください。\n"
        "`manual_inputs` にRPEやメモがあれば、本人の主観情報として使ってください。\n\n"
        "共通の読み取りルール:\n"
        "- このJSONだけを根拠にし、JSON内にない事実は推測で補わないでください。\n"
        "- まず `intervals_icu.metrics` の Fitness / Fatigue / Form / weight / FTP / eFTP / max_heart_rate を確認し、コンディション前提を把握してください。\n"
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
