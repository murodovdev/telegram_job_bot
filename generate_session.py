#!/usr/bin/env python3
"""
generate_session.py – One-time script to create a Telethon string session.

Run this ONCE on your local machine:
    python generate_session.py

It will print a long string like:
    1BVtsOKABu3q7...

Copy that string and paste it into Railway as:
    SESSION_STRING = <the string>

Then delete this file and never run it again.
"""

import asyncio
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

API_ID   = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE    = os.environ["PHONE_NUMBER"]


async def main():
    print("\n🔐 Generating Telethon string session...")
    print("You will be asked to enter your Telegram verification code.\n")

    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        await client.start(phone=PHONE)
        session_string = client.session.save()

    print("\n" + "=" * 60)
    print("✅ YOUR SESSION STRING (copy everything between the lines):")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print("\n📋 Next steps:")
    print("  1. Copy the string above")
    print("  2. In Railway → Variables → add:  SESSION_STRING = <paste here>")
    print("  3. Remove SESSION_NAME from Railway if it exists")
    print("  4. Delete this generate_session.py file")
    print("  5. Deploy\n")


asyncio.run(main())