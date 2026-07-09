# Cycling Dashboard

Personal cycling training dashboard for this flow:

```text
Garmin records the ride
  -> Garmin syncs to Intervals.icu
  -> this app reads Intervals.icu
  -> you only enter RPE
  -> OpenAI reviews the ride
```

## Features

- Reads Fitness / Fatigue / Form / weight / FTP / eFTP from Intervals.icu.
- Reads recent rides from Intervals.icu.
- Tries to download the latest ride FIT file into `data/fit_files`.
- Analyzes uploaded `.fit` files into `fit_activity_context.v2`, shaped like the sample JSON.
- Generates `cycling_training_context.v1` JSON for manual ChatGPT upload.
- Stores RPE locally in SQLite.
- Generates a ride review when the app opens.
- Shows sample data when API keys are not configured.

## Streamlit Community Cloud mode

Use `streamlit_app.py` when publishing on Streamlit Community Cloud.

This mode does not call the OpenAI API. It only generates a JSON file containing:

- Intervals.icu Fitness / Fatigue / Form / weight / FTP / eFTP.
- Latest ride summary.
- Auto-downloaded FIT analysis when available.
- Manual FIT upload fallback.
- A recommended ChatGPT prompt.

Run locally:

```bash
source .venv/bin/activate
streamlit run streamlit_app.py
```

For Streamlit Community Cloud, add these values in the app's Secrets screen:

```toml
INTERVALS_API_KEY = "your_intervals_api_key"
INTERVALS_BASE_URL = "https://intervals.icu"
APP_PASSWORD = "your_private_password"
ATHLETE_CRITICAL_POWER_W = 250
ATHLETE_BODY_MASS_KG = 63
ATHLETE_MAX_HEART_RATE_BPM = 178
ATHLETE_W_PRIME_KJ = 0
LOOKBACK_DAYS = 21
FIT_DOWNLOAD = true
```

Then set the main file to:

```text
cycling-dashboard/streamlit_app.py
```

## Current machine note

This machine currently appears to have WSL enabled but no Linux distribution installed.
Install Ubuntu first, then run the setup below inside WSL.

## WSL setup

```bash
cd "/mnt/c/Users/tkkawamura/OneDrive - 株式会社ネットワールド/2.code work/021.codex/cycling-dashboard"
bash scripts/setup_wsl.sh
```

Edit `.env`:

```bash
INTERVALS_API_KEY=your_intervals_api_key
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-5.5
ATHLETE_CRITICAL_POWER_W=250
ATHLETE_BODY_MASS_KG=63
ATHLETE_MAX_HEART_RATE_BPM=178
```

Run:

```bash
source .venv/bin/activate
flask --app app run --host 0.0.0.0 --port 5050
```

Open on the PC:

```text
http://localhost:5050
```

Open on iPhone from the same Wi-Fi:

```text
http://<PC-IP-address>:5050
```

Then add it to the iPhone Home Screen from Safari.

## FIT analysis

The FIT import panel uploads a `.fit` file and returns compact JSON with:

- `llm_summary`: session summary, key intervals, key laps, metric presence.
- `activity`: summary and load metrics.
- `physiology`: critical-power based summary.
- `signals`: distributions and representative duration curves.
- `segments`: auto interval segments and FIT lap segments.
- `coach_context`: short context for the coaching review.

The JSON intentionally excludes:

- GPS location fields.
- Raw 1 Hz records.
- Full dense duration curves.

## Intervals.icu API key

In Intervals.icu, open Settings, then Developer Settings, and create an API key.
For personal API key access, Intervals.icu uses Basic auth with username `API_KEY`
and the API key as the password.

## Notes

The public Intervals.icu docs are not explicit about a single stable FIT download
endpoint. This app tries several likely endpoints. If FIT download fails, the app
still evaluates the ride from Intervals.icu summary data. You can also upload a
FIT file manually.
