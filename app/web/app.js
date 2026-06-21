// ブラウザUI: iPhone画面表示 ＋ マウス/タッチで操作
// 座標スケーリング: imgの表示サイズ(CSS px) → デバイス points
const screenImg = document.getElementById("screen");
const overlay = document.getElementById("overlay");
const hint = document.getElementById("hint");
const statusEl = document.getElementById("status");

let screenInfo = { width: 1, height: 1 }; // デバイス points
let ready = false;

async function pollStatus() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    ready = !!d.ready;
    if (d.screen && d.screen.width) screenInfo = d.screen;
    statusEl.textContent = ready
      ? `接続中 ${Math.round(screenInfo.width)}x${Math.round(screenInfo.height)}`
      : (d.error ? `エラー: ${d.error}` : "未接続");
    if (d.sign_remaining_days != null && d.sign_remaining_days < 3) {
      statusEl.textContent += ` (署名残り${Math.ceil(d.sign_remaining_days)}日)`;
    }
    overlay.classList.toggle("hidden", ready);
    hint.classList.toggle("hidden", ready);
    if (ready && !screenImg.src) screenImg.src = "/stream";
  } catch (e) {
    statusEl.textContent = "状態取得失敗";
  }
}
setInterval(pollStatus, 3000);
pollStatus();

// 表示上の座標 → デバイス points
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

// ポインタ操作（マウス＋タッチ共通）
let pointer = null; // { startX, startY, t0, moved, lastX, lastY }
const LONG_PRESS_MS = 500;
const MOVE_THRESHOLD = 8; // px以上動いたらスワイプ扱い

function pointerDown(x, y) {
  pointer = { startX: x, startY: y, t0: Date.now(), moved: false, lastX: x, lastY: y };
}
async function pointerMove(x, y) {
  if (!pointer) return;
  if (Math.hypot(x - pointer.lastX, y - pointer.lastY) > 2) pointer.moved = true;
  pointer.lastX = x;
  pointer.lastY = y;
}
async function pointerUp(x, y) {
  if (!pointer) return;
  const dt = Date.now() - pointer.t0;
  const dist = Math.hypot(x - pointer.startX, y - pointer.startY);
  const start = toDevice(pointer.startX, pointer.startY);
  const end = toDevice(x, y);
  if (dist < MOVE_THRESHOLD) {
    // タップ or 長押し
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

// ボタン
document.querySelectorAll("[data-button]").forEach((btn) => {
  btn.addEventListener("click", () => post("/api/button", { button: btn.dataset.button }));
});

// テキスト入力
document.getElementById("send-text").addEventListener("click", () => {
  const v = document.getElementById("text-input").value;
  if (v) post("/api/keys", { text: v });
});

// キーボード入力を転送（画面フォーカス時）
window.addEventListener("keydown", (e) => {
  if (document.activeElement && document.activeElement.tagName === "INPUT") return;
  if (e.key === "Enter") { post("/api/button", { button: "home" }); return; }
  post("/api/keys", { text: e.key.length === 1 ? e.key : "" });
});