"""Long-run stability verification against the running backend (http://localhost:8000).

Measures three things the FGO practical-use case cares about:
  1. ready rate over the window (is the WDA session staying up?)
  2. lock -> auto-reconnect events (ready flips false -> true while the device
     idle-locks at the 5 min Auto-Lock cap and the backend restarts WDA only)
  3. stream fps (is the MJPEG stream actually delivering frames?)

This script talks HTTP only to the local backend, so it does NOT need sudo or a
direct usbmux/usbmux-tunnel handle -- run it from a normal (non-admin) terminal.

Prerequisite: start the backend first with sudo + an iPhone connected:
    sudo .venv313/bin/python -m app.main > backend.log 2>&1 &

Then run this (defaults: 10 min, backend at http://127.0.0.1:8000):
    .venv313/bin/python scripts/verify_stability.py --duration 600

Output is teed to stability_result.txt as UTF-8.

autolock 実効検証 recipe:
    Unlock the iPhone, start the backend, run this with --duration 600, and leave
    the device untouched. Around the 5 min mark the screen should auto-lock, ready
    should drop to false, and (once you unlock again) the backend should reconnect
    and ready should return true. The reported "reconnect events" count captures
    that false -> true transition.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Make the repo root importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

# Console Unicode safety.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_LOG_PATH = Path("stability_result.txt")
_LOG = open(_LOG_PATH, "w", encoding="utf-8")

# /stream frame boundary (see app/api/routes_stream.py: _frame_chunk).
_FRAME_BOUNDARY = b"--frame\r\n"


def _print(*args, **kwargs) -> None:
    line = " ".join(str(a) for a in args)
    print(line, **kwargs)
    _LOG.write(line + "\n")
    _LOG.flush()


async def _status(client: httpx.AsyncClient) -> dict:
    r = await client.get("/api/status", timeout=5.0)
    r.raise_for_status()
    return r.json()


async def _measure_fps(client: httpx.AsyncClient, window_s: float) -> tuple[int, float]:
    """Open /stream for window_s seconds, count frame boundaries -> (frames, fps)."""
    frames = 0
    deadline = time.monotonic() + window_s
    try:
        async with client.stream("GET", "/stream", timeout=httpx.Timeout(window_s + 5.0)) as resp:
            async for chunk in resp.aiter_raw():
                frames += chunk.count(_FRAME_BOUNDARY)
                if time.monotonic() >= deadline:
                    break
    except Exception as e:
        _print(f"  [fps] stream read failed: {e}")
        return frames, 0.0
    elapsed = max(0.1, time.monotonic() - (deadline - window_s))
    return frames, frames / elapsed


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000",
                    help="backend base URL (default: http://127.0.0.1:8000)")
    ap.add_argument("--duration", type=float, default=600.0,
                    help="total measurement window in seconds (default: 600)")
    ap.add_argument("--poll", type=float, default=3.0,
                    help="/api/status poll interval in seconds (default: 3)")
    ap.add_argument("--fps-window", type=float, default=5.0,
                    help="seconds to read /stream per fps sample (default: 5)")
    ap.add_argument("--fps-every", type=float, default=60.0,
                    help="take an fps sample every N seconds (default: 60)")
    args = ap.parse_args()

    _print(f"=== stability verification ===")
    _print(f"backend: {args.base}  duration: {args.duration:.0f}s  poll: {args.poll:.0f}s")
    _print(f"fps sample: {args.fps_window:.0f}s every {args.fps_every:.0f}s")
    _print()

    samples = 0
    ready_count = 0
    reconnect_events = 0
    prev_ready = None
    fps_samples: list[float] = []
    sign_days_last = None
    errors_seen: list[str] = []
    t0 = time.monotonic()
    next_fps = t0 + args.fps_every

    async with httpx.AsyncClient(base_url=args.base) as client:
        while time.monotonic() - t0 < args.duration:
            # Poll status.
            try:
                d = await _status(client)
                ready = bool(d.get("ready"))
                error = d.get("error")
                wda_alive = d.get("wda_alive")
                sign_days = d.get("sign_remaining_days")
            except Exception as e:
                ready, error, wda_alive, sign_days = False, f"<status fetch failed: {e}>", False, None

            samples += 1
            if ready:
                ready_count += 1
            if prev_ready is False and ready:
                reconnect_events += 1
                _print(f"  [reconnect] ready false -> true at +{time.monotonic()-t0:.0f}s")
            prev_ready = ready
            if sign_days is not None:
                sign_days_last = sign_days
            if error and error not in errors_seen:
                errors_seen.append(error)
            _print(f"+{time.monotonic()-t0:5.0f}s ready={ready} wda_alive={wda_alive} "
                   f"sign_days={sign_days} error={error}")

            # fps sample on schedule.
            if time.monotonic() >= next_fps:
                if ready:
                    n, fps = await _measure_fps(client, args.fps_window)
                    fps_samples.append(fps)
                    _print(f"  [fps] {n} frames in {args.fps_window:.0f}s -> {fps:.1f} fps")
                else:
                    _print(f"  [fps] skipped (not ready)")
                next_fps = time.monotonic() + args.fps_every
            else:
                await asyncio.sleep(args.poll)

    elapsed = time.monotonic() - t0
    ready_rate = (ready_count / samples * 100) if samples else 0.0
    avg_fps = (sum(fps_samples) / len(fps_samples)) if fps_samples else 0.0

    _print()
    _print(f"=== summary ===")
    _print(f"elapsed:        {elapsed:.0f}s")
    _print(f"samples:        {samples}  ready_rate: {ready_rate:.1f}%")
    _print(f"reconnect events (ready false->true): {reconnect_events}")
    _print(f"fps samples:    {len(fps_samples)}  avg: {avg_fps:.1f} fps"
          + (f"  min: {min(fps_samples):.1f}  max: {max(fps_samples):.1f}" if fps_samples else ""))
    _print(f"sign remaining: {sign_days_last}")
    if errors_seen:
        _print(f"errors seen:    {len(errors_seen)} distinct")
        for e in errors_seen[:5]:
            _print(f"  - {e}")

    _print()
    _print("interpretation:")
    _print("  - ready_rate near 100% = WDA session stable (no idle-lock drops).")
    _print("  - reconnect events > 0 during an idle test = autolock fired and the")
    _print("    backend auto-reconnected WDA (expected ~1 per 5 min if untouched).")
    _print("  - avg fps > 0 = MJPEG stream is live; 0 = stream not delivering frames.")

    _LOG.close()
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())