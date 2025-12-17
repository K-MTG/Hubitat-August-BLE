#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# August / Yale Offline Key CLI
#
# Retrieve offline keys and device metadata for August / Yale smart locks
# using the yalexs async API.
#
# This tool authenticates once per session and allows retrieving metadata
# for multiple locks without re-authentication.
#
# USAGE
# -----
# Interactive (recommended):
#
#   python august_cli.py
#
#   - Prompts for brand, email, and password
#   - Handles two-factor authentication (2FA) if required
#   - Lists all locks on the account
#   - Allows selecting a lock by index or device_id
#   - Supports retrieving metadata for multiple locks in the same session
#
#
# Semi-interactive:
#
#   python august_cli.py --brand AUGUST --email user@example.com
#
#   - Prompts only for missing fields (password, lock selection)
#
#
# Fully non-interactive (not recommended for passwords):
#
#   python august_cli.py \
#       --brand AUGUST \
#       --email user@example.com \
#       --password 'your-password' \
#       --lock-id abcdef12-3456-7890
#
#   NOTE:
#   --lock-id expects the device_id shown in the lock list.
#   If provided, it is used once, then interactive selection resumes.
#
#
# JSON output (script / automation friendly):
#
#   python august_cli.py --json
#
#
# OPTIONS
# -------
# --brand        Brand to use:
#                AUGUST | YALE_ACCESS | YALE_HOME | YALE_GLOBAL | YALE_AUGUST
#
# --email        Account email address
# --password     Account password (unsafe to pass via CLI; will prompt if omitted)
# --lock-id      Lock device_id (skip interactive selection once)
# --json         Output lock metadata and offline keys as JSON
# --timeout      API timeout in seconds (default: 20)
# --auth-cache   Access token cache file (default: auth.txt)
#
#
# NOTES
# -----
# - install_id is intentionally fixed to "UUID"
# - Access tokens are cached to avoid repeated logins
# - Two-factor authentication (2FA) is supported
# - Offline keys and metadata are sensitive — handle and store securely
#
# ---------------------------------------------------------------------------

import argparse
import asyncio
import getpass
import json
import sys
from typing import Optional

from aiohttp import ClientSession

from yalexs.api_async import ApiAsync
from yalexs.authenticator_async import AuthenticatorAsync
from yalexs.authenticator_common import AuthenticationState
from yalexs.const import Brand


BRAND_MAP = {
    "AUGUST": Brand.AUGUST,
    "YALE_ACCESS": Brand.YALE_ACCESS,
    "YALE_HOME": Brand.YALE_HOME,
    "YALE_GLOBAL": Brand.YALE_GLOBAL,
    "YALE_AUGUST": Brand.YALE_AUGUST,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="August / Yale CLI – Retrieve offline keys and lock metadata"
    )

    parser.add_argument(
        "--brand",
        choices=BRAND_MAP.keys(),
        help="Brand (AUGUST, YALE_ACCESS, YALE_HOME, YALE_GLOBAL, YALE_AUGUST)",
    )
    parser.add_argument("--email", help="Account email")
    parser.add_argument(
        "--password",
        help="Account password (NOT recommended, will prompt if omitted)",
    )
    parser.add_argument("--lock-id", help="Lock ID (skip selection prompt)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--timeout", type=int, default=20, help="API timeout (seconds)")
    parser.add_argument(
        "--auth-cache",
        default="auth.txt",
        help="Access token cache file (default: auth.txt)",
    )

    return parser.parse_args()


async def authenticate(api: ApiAsync, email: str, password: str, cache_file: str):
    authenticator = AuthenticatorAsync(
        api,
        "email",
        email,
        password,
        access_token_cache_file=cache_file,
        install_id="UUID",  # <-- hard-coded as requested
    )

    await authenticator.async_setup_authentication()
    authentication = await authenticator.async_authenticate()

    if authentication.state == AuthenticationState.REQUIRES_VALIDATION:
        print("\n🔐 Two-factor authentication required")
        await authenticator.async_send_verification_code()
        code = input("Enter verification code: ").strip()
        await authenticator.async_validate_verification_code(code)
        authentication = await authenticator.async_authenticate()

    return authentication.access_token


def print_lock_list(locks):
    print("\n🔒 Your Locks")
    print("-" * 60)
    for idx, lock in enumerate(locks):
        print(f"[{idx}] {lock.device_name}  |  id={lock.device_id}")
    print("-" * 60)


def select_lock(locks, lock_id: Optional[str]):
    if lock_id:
        for lock in locks:
            if lock.device_id == lock_id:
                return lock.device_id
        raise ValueError(f"Lock ID not found: {lock_id}")

    choice = input("Select lock (index or device_id): ").strip()

    if choice.isdigit():
        return locks[int(choice)].device_id

    return choice


def output_result(lock_detail, as_json: bool):
    # NOTE: In yalexs, the lock identifier is exposed as device_id (not lock_id)
    result = {
        "device_name": lock_detail.device_name,
        "lock_id": lock_detail.device_id,  # keep JSON key name stable, but use correct attribute
        "serial_number": lock_detail.serial_number,
        "mac_address": lock_detail.mac_address,
        "offline_key": lock_detail.offline_key,
        "offline_slot": lock_detail.offline_slot,
    }

    if as_json:
        print(json.dumps(result, indent=2))
        return

    print("\n📦 Lock Metadata")
    print("-" * 60)
    print(f"Name          : {lock_detail.device_name}")
    print(f"Lock ID       : {lock_detail.device_id}")
    print(f"Serial        : {lock_detail.serial_number}")
    print(f"MAC Address   : {lock_detail.mac_address}")
    print("\n🔑 Offline Access")
    print("-" * 60)
    print(f"Offline Key   : {lock_detail.offline_key}")
    print(f"Offline Slot : {lock_detail.offline_slot}")
    print("-" * 60)


async def main():
    args = parse_args()

    brand_name = args.brand or input(
        "Enter brand (AUGUST / YALE_ACCESS / YALE_HOME / YALE_GLOBAL / YALE_AUGUST): "
    ).strip().upper()

    brand = BRAND_MAP.get(brand_name)
    if not brand:
        raise ValueError(f"Unknown brand: {brand_name}")

    email = args.email or input("Email: ").strip()
    password = args.password or getpass.getpass("Password: ")

    async with ClientSession() as session:
        api = ApiAsync(session, timeout=args.timeout, brand=brand)

        access_token = await authenticate(
            api,
            email,
            password,
            args.auth_cache,
        )

        locks = await api.async_get_locks(access_token)
        if not locks:
            print("No locks found.")
            return

        while True:
            print_lock_list(locks)

            lock_id = select_lock(locks, args.lock_id)

            lock_detail = await api.async_get_lock_detail(
                access_token=access_token,
                lock_id=lock_id,
            )

            output_result(lock_detail, args.json)

            # Only allow --lock-id to be used once
            args.lock_id = None

            again = input("\nGet another lock? [y/N]: ").strip().lower()
            if again not in ("y", "yes"):
                break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
