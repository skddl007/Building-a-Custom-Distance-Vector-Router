"""
Simple test cases for router.py

How to run:
    python simple_tests.py
or:
    python3 simple_tests.py
"""

from unittest.mock import patch

import router


passed = 0
failed = 0


def reset_state():
    """Reset shared router state before each test."""
    router.routing_table.clear()
    router.DIRECT_SUBNETS = []


def run_test(name, test_func):
    global passed, failed
    print(f"\n[INFO] Running: {name}")
    reset_state()
    try:
        test_func()
        print(f"[PASS] {name}")
        passed += 1
    except AssertionError as err:
        print(f"[FAIL] {name} -> {err}")
        failed += 1
    except Exception as err:  # Keep output friendly for beginners
        print(f"[FAIL] {name} -> Unexpected error: {err}")
        failed += 1


def test_1_valid_packet_accepted():
    packet = router.parse_packet(
        b'{"router_id":"10.0.1.10","version":1.0,"routes":[{"subnet":"10.0.2.0/24","distance":1}]}'
    )
    assert packet is not None, "Valid packet should be accepted"
    assert packet["version"] == 1.0, "Version should be 1.0"


def test_2_wrong_version_rejected():
    packet = router.parse_packet(
        b'{"router_id":"10.0.1.10","version":2.0,"routes":[]}'
    )
    assert packet is None, "Packet with wrong version must be rejected"


def test_3_direct_routes_initialized():
    router.DIRECT_SUBNETS = ["10.0.1.0/24", "10.0.3.0/24"]
    with patch("router.os.system"):
        router.init_direct_routes()

    assert router.routing_table["10.0.1.0/24"]["distance"] == 0, "Direct route must be distance 0"
    assert router.routing_table["10.0.3.0/24"]["learned_from"] == "self", "Direct route source must be self"


def test_4_learn_new_route_from_neighbor():
    changed = router.apply_bellman_ford(
        "10.0.1.11",
        [{"subnet": "10.0.2.0/24", "distance": 1}],
    )

    assert changed is True, "Route table should change for new subnet"
    assert router.routing_table["10.0.2.0/24"]["distance"] == 2, "Distance should be neighbor + 1"
    assert router.routing_table["10.0.2.0/24"]["next_hop"] == "10.0.1.11", "Next hop should be neighbor"


def test_5_keep_direct_route_unchanged():
    ts = router.now()
    router.routing_table["10.0.1.0/24"] = router.make_route(0, "0.0.0.0", "self", ts)

    changed = router.apply_bellman_ford(
        "10.0.1.11",
        [{"subnet": "10.0.1.0/24", "distance": 5}],
    )

    assert changed is False, "Direct route should not be replaced"
    assert router.routing_table["10.0.1.0/24"]["distance"] == 0, "Direct route distance must stay 0"


def test_6_switch_to_better_neighbor():
    ts = router.now()
    router.routing_table["10.0.2.0/24"] = router.make_route(4, "10.0.3.12", "10.0.3.12", ts)

    changed = router.apply_bellman_ford(
        "10.0.1.11",
        [{"subnet": "10.0.2.0/24", "distance": 1}],
    )

    assert changed is True, "Should switch when better path appears"
    assert router.routing_table["10.0.2.0/24"]["distance"] == 2, "Better distance should be chosen"
    assert router.routing_table["10.0.2.0/24"]["learned_from"] == "10.0.1.11", "New source should be neighbor"


def test_7_split_horizon_poisoned_reverse():
    ts = router.now()
    router.routing_table["10.0.2.0/24"] = router.make_route(1, "10.0.1.11", "10.0.1.11", ts)
    router.routing_table["10.0.3.0/24"] = router.make_route(0, "0.0.0.0", "self", ts)

    packet = router.build_packet_for_neighbor("10.0.1.11")
    advertised = {r["subnet"]: r["distance"] for r in packet["routes"]}

    assert advertised["10.0.2.0/24"] == router.INFINITY, "Route learned from this neighbor must be poisoned"
    assert advertised["10.0.3.0/24"] == 0, "Direct route should not be poisoned"


def test_8_timeout_marks_route_unreachable():
    old_time = router.now() - (router.ROUTE_TIMEOUT + 1)
    router.routing_table["10.0.2.0/24"] = router.make_route(2, "10.0.1.11", "10.0.1.11", old_time)

    with patch("router.apply_routes_to_kernel"):
        router.expire_stale_routes()

    assert router.routing_table["10.0.2.0/24"]["distance"] == router.INFINITY, "Stale route should become INFINITY"
    assert router.routing_table["10.0.2.0/24"]["next_hop"] == "0.0.0.0", "Next hop should be cleared"


if __name__ == "__main__":
    print("[INFO] Starting simple router tests...")

    run_test("1. Valid packet accepted", test_1_valid_packet_accepted)
    run_test("2. Wrong version rejected", test_2_wrong_version_rejected)
    run_test("3. Direct routes initialized", test_3_direct_routes_initialized)
    run_test("4. Learn new route from neighbor", test_4_learn_new_route_from_neighbor)
    run_test("5. Keep direct route unchanged", test_5_keep_direct_route_unchanged)
    run_test("6. Switch to better neighbor", test_6_switch_to_better_neighbor)
    run_test("7. Split horizon poisoned reverse", test_7_split_horizon_poisoned_reverse)
    run_test("8. Timeout marks route unreachable", test_8_timeout_marks_route_unreachable)

    print("\n############ Test Summary #############")
    print(f"[INFO] Total Passed: {passed}")
    print(f"[INFO] Total Failed: {failed}")
    print("#############################################")

    if failed > 0:
        raise SystemExit(1)

    print("[INFO] All simple tests passed.")
