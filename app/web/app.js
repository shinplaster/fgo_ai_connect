// Browser UI: show iPhone screen + control with mouse/touch.
// Coordinate scaling: img display size (CSS px) -> device points.
const screenImg = document.getElementById("screen");
const overlay = document.getElementById("overlay");
const hint = document.getElementById("hint");
const statusEl = document.getElementById("status");
const ripple = document.getElementById("ripple");
const wrap = document.getElementById("screen-wrap");

let screenInfo = { width: 1, height: 1 }; // device points
let ready = false;
let wasReady = false;

// --- stream liveness: track <img> frame loads (multipart/x-mixed-replace) ---
let loadTimes = []; // recent frame load timestamps (ms)
const STREAM_STALE_MS = 3000;

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
  ready = !!d.ready;
  if (d.screen && d.screen.width) screenInfo = d.screen;

  // status line: connection + screen + sign + stream
  let parts = [];
  if (ready) {
    parts.push(`接続中 ${Math.round(screenInfo.width)}x${Math.round(screenInfo.height)}`);
    if (!wasReady) parts.push("（再接続完了）");
    const fps = streamFps();
    if (fps != null) parts.push(`${fps.toFixed(1)}fps`);
    else if (streamStale()) parts.push("ストリーム停止");
  } else {
    parts.push(d.error ? `エラー: ${d.error}` : "未接続・再接続中…");
  }
  if (d.sign_remaining_days != null && d.sign_remaining_days < 3) {
    parts.push(`署名残り${Math.ceil(d.sign_remaining_days)}日`);
  }
  statusEl.textContent = parts.join(" / ");

  // overlay: show only when not ready (reconnect banner)
  overlay.classList.toggle("hidden", ready);
  hint.classList.toggle("hidden", ready);
  if (ready && !screenImg.src) screenImg.src = "/stream";
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

// buttons
document.querySelectorAll("[data-button]").forEach((btn) => {
  btn.addEventListener("click", () => post("/api/button", { button: btn.dataset.button }));
});

// text input
document.getElementById("send-text").addEventListener("click", () => {
  const v = document.getElementById("text-input").value;
  if (v) post("/api/keys", { text: v });
});

// forward keyboard input when not focused in the text box.
// Enter -> home removed (accidental home risk during FGO play; use the button).
window.addEventListener("keydown", (e) => {
  if (document.activeElement && document.activeElement.tagName === "INPUT") return;
  if (e.key.length === 1) post("/api/keys", { text: e.key });
});