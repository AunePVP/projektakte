"""``python -m projektakte.passwd NAME`` – create a user entry for AUTH_USERS / AUTH_USERS_FILE."""
from __future__ import annotations

import argparse
import getpass
import sys

from .auth import hash_password


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Benutzereintrag (Name:Passwort-Hash) erzeugen")
    ap.add_argument("name")
    ap.add_argument("--stdin", action="store_true", help="Passwort aus der Standardeingabe lesen")
    a = ap.parse_args(argv)
    if a.stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Passwort: ")
        if password != getpass.getpass("Passwort wiederholen: "):
            sys.exit("Die Passwörter stimmen nicht überein.")
    if len(password) < 10:
        sys.exit("Das Passwort muss mindestens 10 Zeichen haben.")
    print(f"{a.name}:{hash_password(password)}")


if __name__ == "__main__":
    main()
