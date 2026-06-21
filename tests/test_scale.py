from app.utils.scale import DisplaySize, ScreenInfo, device_points, fit_display


def test_device_points_basic():
    screen = ScreenInfo(width=390, height=844, scale=3.0)
    display = DisplaySize(width=195, height=422)
    # 2倍縮小表示 → 座標は2倍
    dx, dy = device_points(100, 200, display, screen)
    assert dx == 200
    assert dy == round(200 * (844 / 422))  # = 400


def test_device_points_zero_display_passthrough():
    screen = ScreenInfo(width=390, height=844, scale=3.0)
    display = DisplaySize(width=0, height=0)
    dx, dy = device_points(123, 456, display, screen)
    assert (dx, dy) == (123, 456)


def test_fit_display_keeps_aspect():
    screen = ScreenInfo(width=390, height=844, scale=3.0)
    d = fit_display(screen, max_w=200, max_h=500)
    # 幅がボトルネック: 390→200 なら高さは 844*(200/390)
    assert abs(d.width - 200) < 0.01
    assert abs(d.height - 844 * (200 / 390)) < 0.01


def test_fit_display_no_upscale():
    screen = ScreenInfo(width=100, height=100, scale=2.0)
    d = fit_display(screen, max_w=500, max_h=500)
    assert d.width == 100 and d.height == 100