"""Create and list accounts from the command line.

    python -m app.auth.cli create-user you@example.com --role operator
    python -m app.auth.cli create-user "+91 98765 43210"
    python -m app.auth.cli list-users

The password is prompted for rather than passed as an argument, so it
doesn't end up in shell history or the process table.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import store


def _create(args) -> int:
    password = args.password or getpass.getpass("Password: ")
    if not args.password:
        if password != getpass.getpass("Confirm password: "):
            print("passwords didn't match", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1

    from .identifiers import IdentifierError, split

    try:
        email, phone = split(args.identifier)
    except IdentifierError as exc:
        print(exc, file=sys.stderr)
        return 1

    try:
        user = store.create_user(
            email=email,
            phone=phone,
            password=password, tenant_id=args.tenant_id,
            role=args.role, display_name=args.display_name,
        )
    except store.DuplicateEmailError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"created user {user.id}: {user.label} (tenant {user.tenant_id}, role {user.role})")
    return 0


def _list(_args) -> int:
    import psycopg

    with psycopg.connect(store.app_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, coalesce(email, phone), tenant_id, role, created_at
                FROM app.users ORDER BY id
                """
            )
            rows = cur.fetchall()

    if not rows:
        print("no users yet — create one with `create-user`")
        return 0
    print(f"{'id':>4}  {'email / phone':<32} {'tenant':>6}  {'role':<9} created")
    for r in rows:
        print(f"{r[0]:>4}  {r[1]:<32} {r[2]:>6}  {r[3]:<9} {r[4]:%Y-%m-%d %H:%M}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.auth.cli", description="Query Warden account management")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="create an account")
    create.add_argument("identifier", help="an email address or a phone number")
    create.add_argument("--password", help="prompted for if omitted (preferred)")
    create.add_argument("--tenant-id", type=int, default=1, dest="tenant_id")
    create.add_argument("--role", choices=["member", "operator"], default="member")
    create.add_argument("--display-name", dest="display_name")
    create.set_defaults(func=_create)

    listing = sub.add_parser("list-users", help="list accounts")
    listing.set_defaults(func=_list)


    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
