#!/usr/bin/env python3
"""Dummy CAN connectivity probe for the HIL bench."""

import time


def check_can_connect(channel: str = "can0") -> bool:
    """Pretend to check CAN connectivity and return True for now."""
    print(f"[can_probe] Checking connectivity on {channel}...")
    time.sleep(0.2)
    # Always succeed in the placeholder implementation.
    return True


if __name__ == "__main__":
    ok = check_can_connect()
    print(f"Connectivity check passed: {ok}")
