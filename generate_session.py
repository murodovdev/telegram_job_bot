#!/usr/bin/env python3
"""
generate_session.py – Interactive Telethon string session generator.

Supports both Account 1 and Account 2.
Offers two login methods:

  QR CODE  (recommended) — scan with Telegram app, no phone code needed.
  PHONE    — traditional phone number + verification code + optional 2FA.

Run LOCALLY, never on Railway.

Usage:
    python generate_session.py              # interactive menu
    python generate_session.py --acct 2     # jump to account 2
    python generate_session.py --acct 2 --qr    # QR login for account 2
    python generate_session.py --acct 1 --phone # phone login for account 1

Requirements:
    pip install -r requirements.txt   (includes qrcode==7.4.2)
"""

import asyncio
import argparse
import getpass
import os
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    PasswordHashInvalidError,
)
from telethon.tl.functions.auth import ResendCodeRequest


# ─────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────

def _get_credentials() -> tuple:
    api_id   = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    if not api_id or not api_hash:
        print("❌  API_ID and API_HASH not found in .env file.")
        print("    Copy .env.example to .env and fill them in first.")
        sys.exit(1)
    return int(api_id), api_hash


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Telethon StringSession",
        add_help=True,
    )
    parser.add_argument(
        "--acct", type=int, choices=[1, 2], default=None,
        help="Account number to generate session for (1 or 2)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--qr",    action="store_true", help="Use QR code login")
    group.add_argument("--phone", action="store_true", help="Use phone code login")
    return parser.parse_args()


def _pick_account(args) -> int:
    if args.acct:
        return args.acct
    print("\nWhich account do you want to generate a session for?")
    print("  1 — Account 1  (SESSION_STRING)")
    print("  2 — Account 2  (SESSION_STRING_2)")
    while True:
        choice = input("\nEnter 1 or 2: ").strip()
        if choice in ("1", "2"):
            return int(choice)
        print("Please enter 1 or 2.")


def _pick_method(args) -> str:
    if args.qr:
        return "qr"
    if args.phone:
        return "phone"
    print("\nChoose login method:")
    print("  1 — 📷  QR Code  (recommended — scan with Telegram app, no code needed)")
    print("  2 — 📱  Phone    (verification code sent to your phone or app)")
    while True:
        choice = input("\nEnter 1 or 2: ").strip()
        if choice == "1":
            return "qr"
        if choice == "2":
            return "phone"
        print("Please enter 1 or 2.")


async def _handle_2fa(client) -> bool:
    """Prompt for 2FA cloud password. Returns True on success."""
    print("\n🔒 This account has Two-Factor Authentication (2FA) enabled.")
    print("   Telegram → Settings → Privacy & Security → Two-Step Verification\n")
    for attempt in range(1, 4):
        password = getpass.getpass(
            f"   2FA password (attempt {attempt}/3, input hidden): "
        )
        if not password:
            print("   ❌ Password cannot be empty.")
            continue
        try:
            await client.sign_in(password=password)
            return True
        except PasswordHashInvalidError:
            if attempt < 3:
                print("   ❌ Wrong password. Try again.")
            else:
                print("   ❌ Wrong password three times. Aborting.")
    return False


def _print_result(session_string: str, session_var: str, me, acct: int, phone: str = ""):
    print("\n" + "=" * 64)
    print("✅  SESSION GENERATED SUCCESSFULLY")
    if me:
        name  = f"{me.first_name or ''} {me.last_name or ''}".strip()
        uname = f"@{me.username}" if me.username else "no username"
        print(f"    Logged in as: {name} ({uname})  |  ID: {me.id}")
    print("=" * 64)
    print(f"\n{session_var}={session_string}\n")
    print("=" * 64)
    print("\n📋 Next steps:")
    print(f"  1. Copy the long string above (everything after {session_var}=)")
    print(f"  2. Railway → Variables → set:  {session_var} = <paste>")
    if acct == 2 and phone:
        print(f"  3. Also set:  PHONE_NUMBER_2 = {phone}")
    print("  4. Redeploy the bot")
    print("  5. Use 🔎 Check Groups in the admin bot to verify the account is active\n")


# ─────────────────────────────────────────────────────────────────
# QR login
# ─────────────────────────────────────────────────────────────────

def _render_qr(url: str) -> None:
    """Print a QR code to the terminal as ASCII art."""
    try:
        import qrcode
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(f"\n   ⚠️  'qrcode' package not installed.")
        print(f"   Run:  pip install qrcode")
        print(f"\n   QR URL (paste into https://qr-code-generator.com if needed):")
        print(f"   {url}\n")
    except Exception as e:
        print(f"\n   Could not render QR ({e})")
        print(f"   URL: {url}\n")


async def _login_qr(client) -> bool:
    """
    QR code login loop — identical to Telegram Desktop's 'Link Device'.
    Returns True on success.
    Raises SessionPasswordNeededError if the account has 2FA.
    """
    print("\n📷  QR CODE LOGIN")
    print("─" * 50)
    print("Steps:")
    print("  1. Open Telegram on your phone")
    print("  2. Go to  Settings → Devices → Link Desktop Device")
    print("  3. Point your camera at the QR code below\n")
    print("  ⚠️  Each QR code expires in ~30 seconds.")
    print("      A new one appears automatically when it expires.\n")

    qr = await client.qr_login()

    while True:
        _render_qr(qr.url)
        print("⏳ Waiting for scan…")

        try:
            await qr.wait(timeout=30)
            print("\n✅ QR scanned successfully!")
            return True

        except asyncio.TimeoutError:
            print("\n🔄 QR expired — generating a new one…\n")
            await qr.recreate()

        except SessionPasswordNeededError:
            raise   # handled by caller

        except Exception as e:
            print(f"\n❌ QR login error: {e}")
            return False


# ─────────────────────────────────────────────────────────────────
# Phone login
# ─────────────────────────────────────────────────────────────────

def _describe_sent_code(sent) -> str:
    """
    Describe where Telegram sent the code without importing any
    SentCodeType* classes — works across all Telethon versions.
    """
    type_name = type(sent.type).__name__.lower()   # e.g. 'sentcodetypeapp'
    if "app" in type_name:
        return (
            "📲 Code sent to the TELEGRAM APP.\n"
            "   Open Telegram on any device logged in to this account.\n"
            "   Look for a message from 'Telegram' containing the code."
        )
    if "sms" in type_name:
        return (
            "💬 Code sent via SMS.\n"
            "   Check your text messages."
        )
    if "flash" in type_name:
        return (
            "⚡ Code sent via FLASH CALL.\n"
            "   You will receive a missed call — the code is the last digits of the number."
        )
    if "call" in type_name:
        return (
            "📞 Code sent via VOICE CALL.\n"
            "   Telegram will call your number and read the code aloud."
        )
    return f"📨 Code sent (type: {type(sent.type).__name__}). Check your Telegram app or SMS."


def _is_app_delivery(sent) -> bool:
    return "app" in type(sent.type).__name__.lower()


def _get_phone(acct: int) -> str:
    env_key = "PHONE_NUMBER" if acct == 1 else "PHONE_NUMBER_2"
    phone   = os.getenv(env_key, "").strip()

    if phone:
        print(f"\n📱 Using {env_key} from .env: {phone}")
        confirm = input("   Is this the correct number? [Y/n]: ").strip().lower()
        if confirm not in ("", "y", "yes"):
            phone = ""

    if not phone:
        print(f"\n📱 Enter the phone number for Account {acct}.")
        print("   International format — examples:")
        print("     Korea:      +821012345678")
        print("     Uzbekistan: +998901234567")
        while True:
            phone = input("   Phone number: ").strip()
            if phone.startswith("+") and len(phone) > 7:
                break
            print("   ❌ Must start with + and include the country code. Try again.")
    return phone


async def _login_phone(client, acct: int) -> tuple:
    """
    Phone code login.
    Returns (True, phone) on success.
    Raises SessionPasswordNeededError if 2FA is needed.
    """
    phone = _get_phone(acct)
    print(f"\n🔐 Requesting verification code for {phone} …\n")

    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        mins, secs = divmod(e.seconds, 60)
        print("⏳ Telegram is rate-limiting code requests for this number.")
        print("   Too many attempts were made recently.")
        print(f"   You must wait {mins} min {secs} sec before trying again.")
        print("   ⚠️  Do NOT keep rerunning the script — each attempt resets the timer.")
        return False, phone
    except PhoneNumberInvalidError:
        print(f"❌ '{phone}' is not a valid Telegram phone number.")
        print("   Check the country code. No spaces or dashes.")
        return False, phone
    except Exception as e:
        print(f"❌ Failed to request code: {e}")
        return False, phone

    print(_describe_sent_code(sent))

    # Offer SMS fallback if the code went to the Telegram app
    if _is_app_delivery(sent):
        print()
        print("   ⚠️  If you can't access the Telegram app on this account,")
        choice = input("   request the code via SMS instead? [y/N]: ").strip().lower()
        if choice in ("y", "yes"):
            try:
                resent = await client(ResendCodeRequest(phone, sent.phone_code_hash))
                sent   = resent
                print(f"\n{_describe_sent_code(sent)}")
            except Exception as e:
                print(f"   ⚠️  Could not request SMS ({e}). Using original code.")

    print()

    # Enter code — up to 3 attempts
    for attempt in range(1, 4):
        code = input(f"Enter the verification code (attempt {attempt}/3): ").strip()
        code = code.replace(" ", "").replace("-", "")   # tolerate pasted formatting

        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            return True, phone

        except PhoneCodeInvalidError:
            if attempt < 3:
                print("❌ Incorrect code. Try again.\n")
            else:
                print("❌ Incorrect code three times. Restart the script for a fresh code.")

        except PhoneCodeExpiredError:
            print("❌ The code has expired (valid ~2 minutes).")
            print("   Restart the script to request a new one.")
            return False, phone

        except SessionPasswordNeededError:
            raise   # handled by caller

        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            return False, phone

    return False, phone


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────

async def main():
    api_id, api_hash = _get_credentials()
    args             = _parse_args()
    acct             = _pick_account(args)
    method           = _pick_method(args)
    session_var      = "SESSION_STRING" if acct == 1 else "SESSION_STRING_2"
    phone            = ""

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    signed_in = False
    try:
        if method == "qr":
            try:
                signed_in = await _login_qr(client)
            except SessionPasswordNeededError:
                signed_in = await _handle_2fa(client)
        else:
            try:
                signed_in, phone = await _login_phone(client, acct)
            except SessionPasswordNeededError:
                signed_in = await _handle_2fa(client)

    except KeyboardInterrupt:
        print("\n\nAborted.")
        await client.disconnect()
        sys.exit(0)

    if not signed_in:
        await client.disconnect()
        sys.exit(1)

    session_string = client.session.save()
    await client.disconnect()

    # Verify by reconnecting once to confirm identity
    me = None
    try:
        async with TelegramClient(
            StringSession(session_string), api_id, api_hash
        ) as verify:
            me = await verify.get_me()
    except Exception:
        pass

    _print_result(session_string, session_var, me, acct, phone)


asyncio.run(main())