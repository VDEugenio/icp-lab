"""Generate the DASHBOARD_PASSWORD_HASH value for your .env.

Usage:
    python backend/hash_password.py            # prompts (input hidden)
    python backend/hash_password.py <password> # non-interactive
"""
import sys

from auth import hash_password

if __name__ == "__main__":
    if len(sys.argv) > 1:
        pw = sys.argv[1]
    else:
        import getpass

        pw = getpass.getpass("Password: ")
    if not pw:
        sys.exit("Empty password refused.")
    print(hash_password(pw))
