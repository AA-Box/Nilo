"""The development dashboard: one HTML file, no build step, no dependencies.

    GET /            the page
    GET /api/...     everything it reads and posts

This is a **development** surface and it looks like one. It is deliberately not a product:
no framework, no bundler, no package.json, no CDN. One file, served as a string, so the
dashboard cannot rot separately from the API it reads — and so running it needs nothing
that is not already in the repository.

Two decisions worth their reasons:

* **The token is never in a URL.** The event stream is read with ``fetch`` and a
  ``ReadableStream`` rather than ``EventSource``, because ``EventSource`` cannot set an
  ``Authorization`` header and the alternative is a token in a query string, in the
  browser history, and in every access log between here and there.
* **The page reads only what the API serves.** Every panel renders a response from
  :mod:`robot.api.views`. There is no second model of a robot in JavaScript, so a field
  that stops existing stops being shown rather than showing stale nonsense.
"""

from __future__ import annotations

#: The whole dashboard. One string, because a second file is a second thing to deploy.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nilo robot dashboard</title>
<style>
  :root {
    --bg: #12141a; --panel: #1b1e26; --line: #2b303c; --ink: #e7e9ee;
    --dim: #939bad; --accent: #6fa8ff; --good: #57c98b; --warn: #e0b341; --bad: #e2685f;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
  header { display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
           padding: 10px 16px; border-bottom: 1px solid var(--line); background: var(--panel); }
  header h1 { font-size: 14px; margin: 0 8px 0 0; font-weight: 600; letter-spacing: .04em; }
  header .grow { flex: 1; }
  select, input, button { background: #0e1015; color: var(--ink); border: 1px solid var(--line);
           border-radius: 4px; padding: 4px 7px; font: inherit; }
  button { cursor: pointer; }
  button:hover { border-color: var(--accent); }
  button.danger { border-color: var(--bad); color: var(--bad); }
  button.danger:hover { background: var(--bad); color: #14161c; }
  .pill { padding: 2px 7px; border-radius: 10px; border: 1px solid var(--line); color: var(--dim); }
  .pill.on { color: var(--good); border-color: var(--good); }
  .pill.off { color: var(--bad); border-color: var(--bad); }
  main { display: grid; gap: 10px; padding: 12px;
         grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); align-items: start; }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }
  section > h2 { margin: 0; padding: 7px 10px; font-size: 11px; letter-spacing: .10em;
                 text-transform: uppercase; color: var(--dim); border-bottom: 1px solid var(--line); }
  section > div { padding: 9px 10px; max-height: 320px; overflow: auto; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 1px 0; vertical-align: top; }
  td.k { color: var(--dim); padding-right: 10px; white-space: nowrap; width: 1%; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-bottom: 6px; }
  .row input { width: 76px; }
  .muted { color: var(--dim); }
  .bar { height: 6px; background: #0e1015; border-radius: 3px; overflow: hidden; margin-top: 3px; }
  .bar > i { display: block; height: 100%; background: var(--accent); }
  ul { margin: 0; padding-left: 16px; }
  li { margin: 1px 0; word-break: break-word; }
  .ev { display: flex; gap: 8px; }
  .ev b { color: var(--accent); font-weight: 500; min-width: 88px; }
  .bad { color: var(--bad); } .good { color: var(--good); } .warn { color: var(--warn); }
  #toast { position: fixed; right: 14px; bottom: 14px; max-width: 420px; padding: 8px 12px;
           border-radius: 5px; border: 1px solid var(--line); background: var(--panel);
           display: none; white-space: pre-wrap; }
</style>
</head>
<body>
<header>
  <h1>NILO ROBOT DASHBOARD</h1>
  <select id="robot"></select>
  <span id="link" class="pill">offline</span>
  <span id="audio" class="pill">idle</span>
  <span id="estop" class="pill">e-stop clear</span>
  <span class="grow"></span>
  <span id="binding" class="muted"></span>
  <input id="token" type="password" placeholder="admin token" size="18">
  <button id="save">use</button>
  <a class="muted" href="/api/openapi.json" target="_blank" rel="noreferrer">openapi</a>
</header>

<main>
  <section><h2>Robot</h2><div id="p-robot" class="muted">-</div></section>
  <section><h2>Battery &amp; pose</h2><div id="p-power" class="muted">-</div></section>
  <section><h2>Sensors</h2><div id="p-sensors" class="muted">-</div></section>
  <section><h2>Current action</h2><div id="p-action" class="muted">-</div></section>
  <section><h2>Behaviour</h2><div id="p-behavior" class="muted">-</div></section>
  <section><h2>Candidate scores</h2><div id="p-scores" class="muted">-</div></section>
  <section><h2>Internal state</h2><div id="p-personality" class="muted">-</div></section>
  <section><h2>World entities</h2><div id="p-world" class="muted">-</div></section>
  <section><h2>Known people</h2><div id="p-people" class="muted">-</div></section>
  <section><h2>Recent memories</h2><div id="p-memory" class="muted">-</div></section>
  <section><h2>Robot tools</h2><div id="p-tools" class="muted">-</div></section>
  <section><h2>Conversation</h2><div id="p-chat" class="muted">-</div></section>

  <section><h2>Controls</h2><div id="p-controls">
    <div class="row">
      <input id="c-distance" type="number" value="200" step="50"> mm
      <button data-post="actions/move" data-from="c-distance" data-field="distance_mm">move</button>
      <input id="c-angle" type="number" value="45" step="15"> deg
      <button data-post="actions/turn" data-from="c-angle" data-field="angle_deg">turn</button>
      <button class="danger" data-post="actions/stop">stop</button>
    </div>
    <div class="row">
      <input id="c-pitch" type="number" value="0"> pitch
      <input id="c-yaw" type="number" value="0"> yaw
      <button id="c-head">head</button>
      <input id="c-lift" type="number" value="0" min="0" max="100"> %
      <button data-post="actions/lift" data-from="c-lift" data-field="height_pct">lift</button>
    </div>
    <div class="row">
      <select id="c-animation"></select><button id="c-play">play</button>
      <select id="c-expression"></select><button id="c-face">express</button>
    </div>
    <div class="row">
      <select id="c-autonomy">
        <option>off</option><option>passive</option><option selected>normal</option><option>full</option>
      </select>
      <button id="c-mode">autonomy</button>
      <button class="danger" id="c-estop">emergency stop</button>
      <button id="c-clear">clear</button>
    </div>
  </div></section>

  <section><h2>Simulator</h2><div id="p-sim">
    <div class="row">
      <button data-sim="person">person detected</button>
      <button data-sim="object">object detected</button>
    </div>
    <div class="row">
      <button data-sim="obstacle" data-on="1">obstacle</button>
      <button data-sim="obstacle" data-on="0">clear obstacle</button>
    </div>
    <div class="row">
      <button data-sim="cliff" data-on="1">cliff</button>
      <button data-sim="cliff" data-on="0">clear cliff</button>
    </div>
    <div class="row">
      <button data-sim="touch" data-on="1">touch</button>
      <button data-sim="touch" data-on="0">release</button>
      <button data-sim="low_battery">low battery</button>
    </div>
    <p class="muted">Injections go in through the same doors real perception uses.</p>
  </div></section>

  <section style="grid-column: 1 / -1"><h2>Recent events</h2><div id="p-events" class="muted">-</div></section>
</main>
<div id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
const state = { robot: null, token: localStorage.getItem("nilo-token") || "", events: [], stream: null };
$("token").value = state.token;

function headers() {
  return state.token ? { "Authorization": "Bearer " + state.token } : {};
}
function toast(message, bad) {
  const box = $("toast");
  box.textContent = message;
  box.style.display = "block";
  box.style.borderColor = bad ? "var(--bad)" : "var(--line)";
  clearTimeout(box._t);
  box._t = setTimeout(() => { box.style.display = "none"; }, 5000);
}
async function api(path, options) {
  const response = await fetch(path, Object.assign({ headers: headers() }, options || {}));
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch (_) { body = { raw: text }; }
  if (!response.ok) {
    const detail = (body && (body.error || body.raw)) || response.statusText;
    throw new Error(response.status + " " + detail + (body && body.reason ? " [" + body.reason + "]" : ""));
  }
  return body;
}
async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, headers()),
    body: JSON.stringify(body || {}),
  });
}
const esc = (value) => String(value === undefined || value === null ? "-" : value)
  .replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const rows = (pairs) => "<table>" + pairs
  .map(([k, v]) => "<tr><td class='k'>" + esc(k) + "</td><td>" + v + "</td></tr>").join("") + "</table>";
const flag = (on, yes, no) => on ? "<span class='bad'>" + yes + "</span>" : "<span class='muted'>" + no + "</span>";

async function refreshRobots() {
  const payload = await api("/api/robots");
  const select = $("robot");
  const ids = payload.robots.map((r) => r.robot_id);
  if (JSON.stringify(ids) !== select._ids) {
    select._ids = JSON.stringify(ids);
    select.innerHTML = ids.map((id) => "<option>" + esc(id) + "</option>").join("");
  }
  if (!state.robot || !ids.includes(state.robot)) state.robot = ids[0] || null;
  if (state.robot) select.value = state.robot;
  return payload.robots;
}

async function refresh() {
  if (!state.token) { $("binding").textContent = "no token"; return; }
  const robots = await refreshRobots();
  const id = state.robot;
  if (!id) { $("p-robot").textContent = "no robot is connected"; return; }
  const base = "/api/robots/" + encodeURIComponent(id);
  const [detail, robotState, behavior, world, memory, tools, chat, personality, animations] =
    await Promise.all([
      api(base), api(base + "/state"), api(base + "/behavior"), api(base + "/world"),
      api(base + "/memory?limit=8"), api(base + "/tools"), api(base + "/conversation"),
      api(base + "/personality"), api(base + "/animations"),
    ]);

  const summary = robots.find((r) => r.robot_id === id) || {};
  $("link").textContent = summary.connected ? "connected" : "offline";
  $("link").className = "pill " + (summary.connected ? "on" : "off");
  $("estop").textContent = detail.emergency_stopped ? "E-STOP ENGAGED" : "e-stop clear";
  $("estop").className = "pill " + (detail.emergency_stopped ? "off" : "");
  $("audio").textContent = chat.audio_state || "idle";

  $("p-robot").innerHTML = rows([
    ["id", esc(summary.robot_id)], ["name", esc(summary.name)],
    ["hardware", esc(summary.hardware_model)], ["firmware", esc(summary.firmware_version)],
    ["session", esc(summary.session_id)], ["address", esc(summary.remote_address)],
    ["reconnects", esc(summary.reconnect_count)], ["autonomy", esc(behavior.mode)],
  ]);

  const battery = robotState.battery || {};
  const pose = robotState.pose || {};
  const motion = robotState.motion || {};
  $("p-power").innerHTML = rows([
    ["battery", esc(battery.percent) + "%" + (battery.charging ? " (charging)" : "") +
      "<div class='bar'><i style='width:" + (battery.percent || 0) + "%'></i></div>"],
    ["pose", esc(pose.x_m) + ", " + esc(pose.y_m) + " @ " + esc(pose.theta_rad) + " rad"],
    ["moving", flag(motion.moving, "yes", "no")],
    ["speed", esc(motion.linear_speed_mps) + " m/s"],
    ["activity", esc((robotState.activity || {}).activity)],
    ["face", esc((robotState.expression || {}).emotion)],
  ]);

  const sensors = robotState.sensors || {};
  $("p-sensors").innerHTML = rows([
    ["cliff", flag(sensors.cliff_detected, "DETECTED", "clear")],
    ["bump", flag(sensors.bump_detected, "DETECTED", "clear")],
    ["picked up", flag(sensors.picked_up, "YES", "no")],
    ["touch", flag(sensors.touch_detected, "TOUCHED", "no")],
  ].concat(Object.entries(sensors.readings || {}).map(([k, v]) => [k, esc(v)])));

  const actions = await api(base + "/actions?limit=6");
  const live = actions.actions.filter((a) => ["pending", "starting", "running"].includes(a.status));
  $("p-action").innerHTML = (live.length ? live : actions.actions.slice(0, 3)).map((a) =>
    "<div>" + esc(a.type) + " <span class='muted'>" + esc(a.source) + "</span> " +
    "<span class='" + (a.status === "succeeded" ? "good" : a.error ? "bad" : "") + "'>" + esc(a.status) + "</span>" +
    (a.error ? " <span class='bad'>" + esc(a.error.reason || a.error.code) + "</span>" : "") +
    " <span class='muted'>" + esc(JSON.stringify(a.parameters)) + "</span></div>"
  ).join("") || "<span class='muted'>nothing yet</span>";

  $("p-behavior").innerHTML = rows([
    ["running", esc(behavior.running)], ["selected", esc(behavior.selected)],
    ["mode", esc(behavior.mode)], ["tick", esc(behavior.tick)],
  ]);
  $("p-scores").innerHTML = (behavior.scores || []).map(([name, score]) =>
    "<div>" + esc(name) + " <span class='muted'>" + Number(score).toFixed(3) + "</span>" +
    "<div class='bar'><i style='width:" + Math.round(score * 100) + "%'></i></div></div>"
  ).join("") || "<span class='muted'>no decision yet</span>";

  $("p-personality").innerHTML = rows(Object.entries(personality.drives || {}).map(([k, v]) =>
    [k, v + "<div class='bar'><i style='width:" + Math.round(v * 100) + "%'></i></div>"]));

  $("p-world").innerHTML = (world.entities || []).map((e) =>
    "<div>" + esc(e.type) + " <b>" + esc(e.id) + "</b> " +
    "<span class='muted'>" + esc(e.age_s) + "s ago, conf " + esc(e.confidence) + "</span></div>"
  ).join("") || "<span class='muted'>nothing visible</span>";

  $("p-people").innerHTML = (memory.people || []).map((p) =>
    "<div>" + esc(p.display_name || p.person_id) + " <span class='muted'>seen " +
    esc(p.interaction_count) + "x, familiarity " + esc(p.familiarity) + "</span></div>"
  ).join("") || "<span class='muted'>nobody yet</span>";

  $("p-memory").innerHTML = "<ul>" + (memory.episodes || []).map((m) =>
    "<li>" + esc(m.summary) + " <span class='muted'>" + esc(m.event_type) + "</span></li>"
  ).join("") + "</ul>";

  $("p-tools").innerHTML = (tools.tools || []).map((t) =>
    "<div><span class='" + (t.available ? "good" : "muted") + "'>" + esc(t.name) + "</span> " +
    "<span class='muted'>" + esc(t.permission) + "</span>" +
    (t.available ? "" : " <span class='muted'>" + esc(t.refused_because) + "</span>") + "</div>"
  ).join("");

  $("p-chat").innerHTML = (chat.messages || []).map((m) =>
    "<div><b class='muted'>" + esc(m.role) + "</b> " + esc(m.content) + "</div>"
  ).join("") || "<span class='muted'>nothing said yet</span>";

  fill("c-animation", animations.animations || []);
  fill("c-expression", tools.expressions || []);
}

function fill(id, values) {
  const select = $(id);
  if (select._values === JSON.stringify(values)) return;
  select._values = JSON.stringify(values);
  select.innerHTML = values.map((v) => "<option>" + esc(v) + "</option>").join("");
}

function record(event) {
  state.events.unshift(event);
  state.events = state.events.slice(0, 60);
  $("p-events").innerHTML = state.events.map((e) =>
    "<div class='ev'><b>" + esc(e.category) + "</b><span class='muted'>" +
    esc((e.occurred_at || "").slice(11, 23)) + "</span><span class='" +
    (e.category === "error" ? "bad" : "") + "'>" + esc(e.event) + "</span>" +
    "<span class='muted'>" + esc(e.robot_id) + "</span></div>"
  ).join("");
}

async function stream() {
  // fetch + ReadableStream rather than EventSource: EventSource cannot set an
  // Authorization header, and the alternative is the token in a query string.
  if (!state.token) return;
  try {
    const response = await fetch("/api/events", { headers: headers() });
    if (!response.ok || !response.body) throw new Error("stream " + response.status);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\\n\\n");
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.split("\\n").find((l) => l.startsWith("data: "));
        if (line) { try { record(JSON.parse(line.slice(6))); } catch (_) {} }
      }
    }
  } catch (error) {
    toast("event stream: " + error.message, true);
  }
  setTimeout(stream, 3000);
}

function robotBase() { return "/api/robots/" + encodeURIComponent(state.robot); }
async function guarded(action) {
  try { const result = await action(); if (result) toast(JSON.stringify(result)); await refresh(); }
  catch (error) { toast(error.message, true); }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button || !state.robot) return;
  if (button.dataset.post) {
    const body = {};
    if (button.dataset.from) body[button.dataset.field] = Number($(button.dataset.from).value);
    guarded(() => post(robotBase() + "/" + button.dataset.post, body));
  } else if (button.dataset.sim) {
    const body = button.dataset.on === undefined ? {} : { detected: button.dataset.on === "1" };
    guarded(() => post(robotBase() + "/simulate/" + button.dataset.sim, body));
  }
});
$("c-head").onclick = () => guarded(() => post(robotBase() + "/actions/head",
  { pitch_deg: Number($("c-pitch").value), yaw_deg: Number($("c-yaw").value) }));
$("c-play").onclick = () => guarded(() => post(robotBase() + "/animations/" + encodeURIComponent($("c-animation").value), {}));
$("c-face").onclick = () => guarded(() => post(robotBase() + "/expression", { emotion: $("c-expression").value }));
$("c-mode").onclick = () => guarded(() => post(robotBase() + "/autonomy-mode", { mode: $("c-autonomy").value }));
$("c-estop").onclick = () => guarded(() => post(robotBase() + "/emergency-stop", {}));
$("c-clear").onclick = () => guarded(() => api(robotBase() + "/emergency-stop", { method: "DELETE" }));
$("robot").onchange = (event) => { state.robot = event.target.value; guarded(() => null); };
$("save").onclick = () => {
  state.token = $("token").value.trim();
  localStorage.setItem("nilo-token", state.token);
  if (state.stream === null) { state.stream = 1; stream(); }
  guarded(() => null);
};

api("/api/meta").then((meta) => {
  $("binding").textContent = meta.host + (meta.control_enabled ? " · control on" : " · control off");
}).catch(() => { $("binding").textContent = ""; });

setInterval(() => { refresh().catch((error) => toast(error.message, true)); }, 2000);
refresh().catch(() => {});
if (state.token) { state.stream = 1; stream(); }
</script>
</body>
</html>
"""


def page() -> str:
    """The dashboard, as HTML."""
    return PAGE


__all__ = ["PAGE", "page"]
