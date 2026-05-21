#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Generate TOTP secret for Employee Workstation Control authentication.

Run on the Mac:
    python3 setup_totp.py

Generates a TOTP secret, saves it to ~/.relay/totp_secret,
and prints the secret for adding to an authenticator app.
"""

import os
import sys

try:
    import pyotp
except ImportError:
    print("ERROR: pyotp not installed. Run: pip3 install pyotp")
    sys.exit(1)

RELAY_DIR = os.path.expanduser("~/.relay")
SECRET_FILE = os.path.join(RELAY_DIR, "totp_secret")

# Create relay directory
os.makedirs(RELAY_DIR, mode=0o700, exist_ok=True)

# Check if secret already exists
if os.path.isfile(SECRET_FILE):
    existing = open(SECRET_FILE).read().strip()
    print(f"TOTP secret already exists: {SECRET_FILE}")
    print(f"Current secret: {existing}")
    resp = input("Generate new secret? (y/N): ").strip().lower()
    if resp != "y":
        print("Keeping existing secret.")
        sys.exit(0)

# Generate new secret
secret = pyotp.random_base32()
uri = pyotp.totp.TOTP(secret).provisioning_uri(
    name="youruser@mac-desktop",
    issuer_name="DesktopRelay",
)

# Save
with open(SECRET_FILE, "w") as f:
    f.write(secret)
os.chmod(SECRET_FILE, 0o600)

print(f"\nTOTP Secret: {secret}")
print(f"Provisioning URI: {uri}")
print(f"\nSaved to: {SECRET_FILE}")
print("\nAdd to your authenticator app:")
print("  Google Authenticator / 1Password -> Add account -> Enter manually")
print(f"  Account: youruser@mac-desktop")
print(f"  Key: {secret}")
print(f"  Type: Time-based (TOTP)")
