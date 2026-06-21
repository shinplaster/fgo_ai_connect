// Browser UI: show iPhone screen + control with mouse/touch.
// Coordinate scaling: img display size (CSS px) -> device points.
const screenImg = document.getElementById("screen");
const overlay = document.getElementById("overlay");
const hint = document.getElementById("hint");
const statusEl = document.getElementById("status");
const ripple = document.getElementById("ripple");
const wrap = document.getElementById("screen-wrap");
const connectBtn = document.getElementById("connect-btn");
const disconnectBtn = document.getElementById("disconnect-btn");

let screenInfo = { width: 1, height: 1 }; // device points
let ready = false;
let wasReady = false;
let desired = false;
let busy = false;
let userAction = null; // null | "unlock" | "connect_usb"

// User-facing hint text for each user_action (shown in the overlay, prioritized
// over the generic status line so the user knows what to do).
const USER_ACTION_HINT = {
  unlock: "iPhone をアンロックしてください",
  connect_usb: "iPhone を USB 接続してください",
};

// --- stream liveness: track <img> frame loads (multipart/x-mixed-replace) ---
let loadTimes = []; // recent frame load timestamps (ms)
const STREAM_STALE_MS = 3000;
// Cooldown for forcing a /stream reload when stale, to avoid a reload loop
// when the stream is genuinely unable to deliver frames.
const STREAM_RELOAD_COOLDOWN_MS = 5000;
let lastStreamReload = 0;

function streamFps() {
  const now = Date.now();
  loadTimes = loadTimes.filter((t) => now - t < STREAM_STALE_MS);
  if (loadTimes.length < 2) return null;
  const span = (loadTimes[loadTimes.length - 1] - loadTimes[0]) / 1000;
  return span > 0 ? (loadTimes.length - 1) / span : 0;
}
function streamStale() {
  return loadTimes.length === 0 || Date.now() - loadTimes[loadTimes.length - 1] > STREAM_STALE_MS;
}

screenImg.addEventListener("load", () => { loadTimes.push(Date.now()); });

async function pollStatus() {
  let d = {};
  try {
    const r = await fetch("/api/status");
    d = await r.json();
  } catch (e) {
    statusEl.textContent = "状態取得失敗";
    return;
  }
  const prevReady = ready;
  ready = !!d.ready;
  desired = !!d.desired;
  busy = !!d.busy;
  userAction = d.user_action || null;
  if (d.screen && d.screen.width) screenInfo = d.screen;

  // Reset the stream <img> when we lose the connection so a stale frame
  // doesn't linger (symmetric to the ready && !src assignment below).
  if (prevReady && !ready) {
    screenImg.removeAttribute("src");
    loadTimes = [];
  }

  // Status line. user_action takes priority (it tells the user what to DO);
  // otherwise fall back to busy/error/ready framing.
  let parts = [];
  if (userAction && USER_ACTION_HINT[userAction]) {
    parts.push(USER_ACTION_HINT[userAction]);
  } else if (ready) {
    parts.push(`接続中 ${Math.round(screenInfo.width)}x${Math.round(screenInfo.height)}`);
    if (!wasReady) parts.push("（再接続完了）");
    const fps = streamFps();
    if (fps != null) parts.push(`${fps.toFixed(1)}fps`);
    else if (streamStale()) parts.push("ストリーム停止");
  } else if (busy) {
    parts.push(desired ? "接続中…" : "切断中…");
  } else if (desired) {
    // Not ready, not busy, but want to be connected -> an error is gating us.
    parts.push(d.error ? `エラー: ${d.error}` : "再接続待ち…");
  } else {
    parts.push(d.error ? `エラー: ${d.error}` : "未接続");
  }
  if (d.sign_remaining_days != null && d.sign_remaining_days < 3) {
    parts.push(`署名残り${Math.ceil(d.sign_remaining_days)}日`);
  }
  statusEl.textContent = parts.join(" / ");

  // Overlay + hint: visible whenever we don't have a live screen.
  const showOverlay = !ready;
  overlay.classList.toggle("hidden", !showOverlay);
  hint.classList.toggle("hidden", !showOverlay);
  if (showOverlay) {
    if (userAction && USER_ACTION_HINT[userAction]) {
      hint.textContent = USER_ACTION_HINT[userAction];
    } else if (ready) {
      // unreachable (overlay hidden when ready) -- keep a sane default
      hint.textContent = "";
    } else if (desired && !busy && d.error) {
      hint.textContent = d.error;
    } else if (desired && busy) {
      hint.textContent = "接続中…（「切断」で中止できます）";
    } else if (desired) {
      hint.textContent = "接続中…";
    } else {
      hint.textContent = "右側の「接続」ボタンを押してください。";
    }
  }

  // Connect/Disconnect button visibility + disabled state.
  // Show 接続 when idle/disconnected, 切断 when connected or wanting to connect.
  const showConnect = !(desired || ready);
  const showDisconnect = desired || ready;
  connectBtn.classList.toggle("hidden", !showConnect);
  disconnectBtn.classList.toggle("hidden", !showDisconnect);
  // Connect is disabled while a connect is mid-flight. Disconnect stays
  // enabled whenever shown so the user can abort a connect — the lifecycle
  // loop applies the desired flip after the current step (tunnel bringup can
  // run up to 120s, during which the button must remain clickable).
  connectBtn.disabled = busy;
  disconnectBtn.disabled = false;

  if (ready && !screenImg.getAttribute("src")) screenImg.src = "/stream";

  // Auto-reload the MJPEG stream when it goes stale while still "ready".
  // The multipart/x-mixed-replace connection can stall mid-stream (WDA's
  // MJPEG server serializes/hogs connections, TCP half-close, etc.) and the
  // <img> then freezes on the last frame — taps still work (separate HTTP)
  // but the screen stops updating. Force a fresh connection with a
  // cache-buster, rate-limited so a genuinely dead stream doesn't spin.
  if (ready && screenImg.getAttribute("src") && streamStale()) {
    const now = Date.now();
    if (now - lastStreamReload > STREAM_RELOAD_COOLDOWN_MS) {
      lastStreamReload = now;
      loadTimes = [];
      screenImg.src = "/stream?r=" + now;
    }
  }
  wasReady = ready;
}
setInterval(pollStatus, 3000);
pollStatus();

// display coords -> device points
function toDevice(clientX, clientY) {
  const rect = screenImg.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  const sx = screenInfo.width / rect.width;
  const sy = screenInfo.height / rect.height;
  return { x: Math.round(x * sx), y: Math.round(y * sy) };
}

async function post(url, body) {
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    console.warn("post failed", e);
  }
}

// tap feedback ripple at the pointer location
function showRipple(clientX, clientY) {
  const rect = wrap.getBoundingClientRect();
  ripple.style.left = (clientX - rect.left) + "px";
  ripple.style.top = (clientY - rect.top) + "px";
  ripple.classList.remove("hidden");
  // restart animation
  ripple.style.animation = "none";
  void ripple.offsetWidth;
  ripple.style.animation = "";
}
function hideRipple() {
  ripple.classList.add("hidden");
}

// pointer ops (mouse + touch share)
let pointer = null; // { startX, startY, t0, moved, lastX, lastY }
const LONG_PRESS_MS = 500;
const MOVE_THRESHOLD = 8; // px

function pointerDown(x, y) {
  pointer = { startX: x, startY: y, t0: Date.now(), moved: false, lastX: x, lastY: y };
  showRipple(x, y);
}
async function pointerMove(x, y) {
  if (!pointer) return;
  if (Math.hypot(x - pointer.lastX, y - pointer.lastY) > 2) pointer.moved = true;
  pointer.lastX = x;
  pointer.lastY = y;
  if (pointer.moved) showRipple(x, y);
}
async function pointerUp(x, y) {
  if (!pointer) return;
  hideRipple();
  const dt = Date.now() - pointer.t0;
  const dist = Math.hypot(x - pointer.startX, y - pointer.startY);
  const start = toDevice(pointer.startX, pointer.startY);
  const end = toDevice(x, y);
  if (dist < MOVE_THRESHOLD) {
    if (dt >= LONG_PRESS_MS) {
      await post("/api/hold", { x: start.x, y: start.y, duration_ms: dt });
    } else {
      await post("/api/tap", { x: start.x, y: start.y });
    }
  } else {
    await post("/api/swipe", {
      fromX: start.x, fromY: start.y, toX: end.x, toY: end.y, duration_ms: Math.max(200, dt),
    });
  }
  pointer = null;
}

screenImg.addEventListener("mousedown", (e) => { e.preventDefault(); pointerDown(e.clientX, e.clientY); });
window.addEventListener("mousemove", (e) => { if (pointer) pointerMove(e.clientX, e.clientY); });
window.addEventListener("mouseup", (e) => { if (pointer) pointerUp(e.clientX, e.clientY); });

screenImg.addEventListener("touchstart", (e) => {
  e.preventDefault();
  const t = e.touches[0]; pointerDown(t.clientX, t.clientY);
}, { passive: false });
screenImg.addEventListener("touchmove", (e) => {
  e.preventDefault();
  const t = e.touches[0]; pointerMove(t.clientX, t.clientY);
}, { passive: false });
screenImg.addEventListener("touchend", (e) => {
  e.preventDefault();
  const t = e.changedTouches[0]; pointerUp(t.clientX, t.clientY);
}, { passive: false });

// connection buttons (page-driven connect/disconnect). fire-and-forget; the
// status poll picks up busy/ready/user_action transitions.
connectBtn.addEventListener("click", () => post("/api/connect", {}));
disconnectBtn.addEventListener("click", () => post("/api/disconnect", {}));