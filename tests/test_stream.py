from app.stream.screenshot import ScreenshotStreamer


def test_screenshot_streamer_mime_and_interval():
    s = ScreenshotStreamer(udid="x", target_fps=12)
    assert ScreenshotStreamer.MIME == "image/png"
    assert abs(s.interval - 1 / 12) < 1e-6


def test_screenshot_streamer_interval_floor():
    # target_fps <= 0 must not divide by zero
    s = ScreenshotStreamer(udid="x", target_fps=0)
    assert s.interval == 1.0