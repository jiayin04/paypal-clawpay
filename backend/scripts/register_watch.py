"""Register the ClawPay watch app's approver key as its own device (operator step).

The watch app creates its P-256 key on first launch (Secure Enclave on a real
watch, a software key in the simulator) and writes the public key to
``Documents/approver_public_key.pem`` in its container. This script registers
that key with the existing admin endpoint, ``POST /api/v1/devices/register``,
and hands the returned ``cpd_`` token to the watch:

    # simulator: reads the key from the app container, then relaunches the app with the token
    uv run python -m scripts.register_watch --user-id <uuid> --simulator <watch-udid>

    # real watch: export the key from the app's Settings (Share), then paste the printed token there
    uv run python -m scripts.register_watch --user-id <uuid> --public-key-file watch_key.pem

``--admin-token`` defaults to INTERNAL_SERVICE_TOKEN from the environment or
``.env`` (never printed). Orders the watch creates with this token belong to the
watch and are approved on the watch; the claw keeps its own token and button.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values


BUNDLE_ID = "com.clawpay.watch"
KEY_FILE = "Documents/approver_public_key.pem"


def simulator_public_key(udid: str) -> str:
    container = subprocess.run(["xcrun", "simctl", "get_app_container", udid, BUNDLE_ID, "data"],
                               check=True, capture_output=True, text=True).stdout.strip()
    path = Path(container) / KEY_FILE
    if not path.exists():
        sys.exit(f"No {KEY_FILE} yet: launch the watch app once on {udid}, then rerun.")
    return path.read_text()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", required=True, help="owner's Supabase user id (the demo server prints it)")
    parser.add_argument("--admin-token", help="prefer leaving this out: it defaults to INTERNAL_SERVICE_TOKEN from "
                        "env/.env, which keeps the token out of shell history and process lists")
    parser.add_argument("--device-name", default="apple-watch")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--simulator", metavar="UDID", help="read the key from this watch simulator and relaunch the app")
    source.add_argument("--public-key-file", type=Path)
    args = parser.parse_args()

    admin = args.admin_token or os.environ.get("INTERNAL_SERVICE_TOKEN") or dotenv_values(".env").get("INTERNAL_SERVICE_TOKEN")
    if not admin:
        sys.exit("No admin token: pass --admin-token or set INTERNAL_SERVICE_TOKEN.")
    pem = simulator_public_key(args.simulator) if args.simulator else args.public_key_file.read_text()

    response = httpx.post(f"{args.base_url.rstrip('/')}/api/v1/devices/register",
                          headers={"Authorization": f"Bearer {admin.strip()}"},
                          json={"user_id": args.user_id, "device_name": args.device_name, "public_key_pem": pem},
                          timeout=15)
    if response.status_code != 201:
        sys.exit(f"Registration failed: {response.status_code} {response.text[:300]}")
    device = response.json()
    print(f"Registered watch device {device['device_id']}")

    if args.simulator:
        env = {**os.environ, "SIMCTL_CHILD_CLAWPAY_BASE_URL": args.base_url,
               "SIMCTL_CHILD_CLAWPAY_TOKEN": device["device_token"], "SIMCTL_CHILD_CLAWPAY_APPROVE_ON_WATCH": "1"}
        subprocess.run(["xcrun", "simctl", "launch", "--terminate-running-process", args.simulator, BUNDLE_ID],
                       check=True, env=env, capture_output=True)
        print("Relaunched the watch app with its own token; approvals now happen on the watch.")
    else:
        # Shown once (the server stores only a hash). Paste it into the watch app's Settings.
        print(f"Device token (paste into the watch app's Settings, then turn on 'Approve on this watch'):\n"
              f"{device['device_token']}")


if __name__ == "__main__":
    main()
