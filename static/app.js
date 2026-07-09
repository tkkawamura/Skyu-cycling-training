const state = {
  dashboard: null,
};

const $ = (id) => document.getElementById(id);

async function loadDashboard() {
  $("refresh").disabled = true;
  try {
    const res = await fetch("/api/dashboard");
    const data = await res.json();
    state.dashboard = data;
    render(data);
  } finally {
    $("refresh").disabled = false;
  }
}

function render(data) {
  const metrics = data.metrics || {};
  const ride = data.latest_ride || {};
  const assessment = data.assessment || {};

  setText("fitness", fmt(metrics.fitness));
  setText("fatigue", fmt(metrics.fatigue));
  setText("form", fmt(metrics.form));
  setText("weight", metrics.weight ? `${fmt(metrics.weight)} kg` : "-");
  setText("ftp", metrics.ftp ? `${fmt(metrics.ftp)} W` : "-");
  setText("eftp", metrics.eftp ? `${fmt(metrics.eftp)} W` : "-");

  setText("ride-name", ride.name || "No ride found");
  setText("ride-date", ride.date || "-");
  setText("ride-load", fmt(ride.training_load || ride.load));
  setText("ride-time", ride.moving_time ? minutes(ride.moving_time) : "-");
  setText("ride-power", ride.weighted_average_watts ? `${fmt(ride.weighted_average_watts)} W` : "-");
  setText("ride-hr", ride.average_heartrate ? `${fmt(ride.average_heartrate)} bpm` : "-");

  $("rpe").value = data.rpe || 5;
  $("rpe-value").textContent = data.rpe ? String(data.rpe) : "Not set";

  setText("score", fmt(assessment.score));
  setText("headline", assessment.headline || "-");
  setText("good", assessment.good || "-");
  setText("concern", assessment.concern || "-");
  setText("next-action", assessment.next_action || "-");
  setText("tomorrow", assessment.tomorrow || "-");
  setText("note", assessment.note || "");
  setText("source", data.source === "intervals" ? "Live" : "Sample");

  if (data.warning || data.fit_analysis_warning) {
    $("warning").textContent = data.warning || data.fit_analysis_warning;
    $("warning").classList.remove("hidden");
  } else {
    $("warning").classList.add("hidden");
  }

  drawTrend(data.trend || []);
}

async function saveRpe() {
  const ride = (state.dashboard && state.dashboard.latest_ride) || {};
  const date = ride.date || new Date().toISOString().slice(0, 10);
  await fetch("/api/rpe", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({date, rpe: Number($("rpe").value)}),
  });
  await loadDashboard();
}

async function analyzeFit(event) {
  event.preventDefault();
  const file = $("fit-file").files[0];
  if (!file) return;
  $("fit-status").textContent = "Analyzing";
  $("fit-output").textContent = "";

  const form = new FormData();
  form.append("fit", file);
  const res = await fetch("/api/fit/analyze", {method: "POST", body: form});
  const data = await res.json();
  if (!data.ok) {
    $("fit-status").textContent = "Error";
    $("fit-output").textContent = data.error || "Failed";
    return;
  }

  $("fit-status").textContent = "Done";
  $("fit-output").textContent = JSON.stringify(
    {
      llm_summary: data.context.llm_summary,
      activity: data.context.activity,
      physiology: data.context.physiology,
      segments: data.context.segments,
      coach_context: data.context.coach_context,
      assessment: data.assessment,
    },
    null,
    2
  );
}

function drawTrend(rows) {
  const svg = $("trend-chart");
  svg.innerHTML = "";
  svg.setAttribute("viewBox", "0 0 900 260");
  if (!rows.length) return;

  const series = [
    ["fitness", "#1f8a58"],
    ["fatigue", "#c94a3a"],
    ["form", "#286fb4"],
  ];
  const chartValues = rows.flatMap((row) => series.map(([key]) => Number(row[key])).filter(Number.isFinite));
  const min = Math.min(...chartValues, -30);
  const max = Math.max(...chartValues, 100);
  const left = 42;
  const right = 20;
  const top = 18;
  const bottom = 34;
  const width = 900 - left - right;
  const height = 260 - top - bottom;

  [0, 0.25, 0.5, 0.75, 1].forEach((pct) => {
    const y = top + height * pct;
    svg.appendChild(line(left, y, 900 - right, y, "#dce4df", 1));
  });

  series.forEach(([key, color]) => {
    const points = rows
      .map((row, i) => {
        const value = Number(row[key]);
        if (!Number.isFinite(value)) return null;
        const x = left + (width * i) / Math.max(1, rows.length - 1);
        const y = top + height - ((value - min) / Math.max(1, max - min)) * height;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .filter(Boolean)
      .join(" ");
    const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    polyline.setAttribute("points", points);
    polyline.setAttribute("fill", "none");
    polyline.setAttribute("stroke", color);
    polyline.setAttribute("stroke-width", "4");
    polyline.setAttribute("stroke-linecap", "round");
    polyline.setAttribute("stroke-linejoin", "round");
    svg.appendChild(polyline);
  });
}

function line(x1, y1, x2, y2, color, width) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "line");
  node.setAttribute("x1", x1);
  node.setAttribute("y1", y1);
  node.setAttribute("x2", x2);
  node.setAttribute("y2", y2);
  node.setAttribute("stroke", color);
  node.setAttribute("stroke-width", width);
  return node;
}

function setText(id, value) {
  $(id).textContent = value ?? "-";
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return Number.isInteger(num) ? String(num) : num.toFixed(1);
}

function minutes(seconds) {
  const total = Math.round(Number(seconds) / 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h ? `${h}h ${m}m` : `${m}m`;
}

$("refresh").addEventListener("click", loadDashboard);
$("save-rpe").addEventListener("click", saveRpe);
$("rpe").addEventListener("input", () => {
  $("rpe-value").textContent = $("rpe").value;
});
$("fit-form").addEventListener("submit", analyzeFit);

loadDashboard();
