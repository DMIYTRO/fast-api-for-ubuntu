#!/usr/bin/env python3
"""Image Magic administrative commands."""

from __future__ import annotations

import argparse
import getpass
import sys

from argon2 import PasswordHasher


def set_password() -> int:
    password = getpass.getpass("Новый пароль: ")
    confirmation = getpass.getpass("Повторите пароль: ")
    if not password:
        print("Пароль не может быть пустым.", file=sys.stderr)
        return 2
    if password != confirmation:
        print("Пароли не совпадают.", file=sys.stderr)
        return 2
    password_hash = PasswordHasher().hash(password)
    print("\nУстановите переменную окружения перед запуском сервера:")
    print(f"IMAGE_MAGIC_PASSWORD_HASH='{password_hash}'")
    print("После перезапуска все ранее созданные сессии станут недействительными.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Управление Image Magic")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("set-password", help="создать Argon2-хеш пароля")
    args = parser.parse_args()
    if args.command == "set-password":
        return set_password()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
