from app.device.wda_client import WdaClient


def test_move_action_structure():
    a = WdaClient._move(120, 240, duration=300)
    assert a["type"] == "pointerMove"
    assert a["x"] == 120 and a["y"] == 240
    assert a["duration"] == 300


def test_pointer_actions_envelope():
    c = WdaClient(udid="x")
    env = c._pointer_actions([WdaClient._move(1, 2)])
    assert env["actions"][0]["type"] == "pointer"
    assert env["actions"][0]["parameters"]["pointerType"] == "touch"
    assert env["actions"][0]["actions"] == [{"type": "pointerMove", "duration": 0, "x": 1, "y": 2}]


def test_down_up_pause():
    assert WdaClient._down()["type"] == "pointerDown"
    assert WdaClient._up()["type"] == "pointerUp"
    assert WdaClient._pause(100)["duration"] == 100