"""Verifies every case in _cases.py against a live database (guard-checks
it, executes it, must succeed) and writes eval/golden/{easy,medium,hard}.yml.
Run from the api/ virtualenv with the api package on the path:

    cd eval && PYTHONPATH=../api ../../.venv/bin/python -m golden._build

Never hand-edit the YAML output — edit _cases.py and re-run this.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from app.guards import ast_guard  # noqa: E402
from app.pipeline.execute import ExecutionError, execute  # noqa: E402

from _cases import ALL_TIERS  # noqa: E402


def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    # Multi-line SQL renders as a block literal (|) — readable, no line
    # wrapping — everything else (id, question) stays plain scalar style.
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


class _Dumper(yaml.SafeDumper):
    pass


# yaml.safe_dump uses SafeDumper specifically — a representer on the
# plain Dumper is silently ignored by it.
_Dumper.add_representer(str, _str_representer)


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    total_ok, total_fail = 0, 0

    for tier, cases in ALL_TIERS.items():
        entries = []
        for case_id, question, sql in cases:
            # Normalize the triple-quoted source's indentation for a
            # left-aligned YAML block literal.
            sql = "\n".join(line.strip() for line in sql.strip().splitlines())
            guard_result = ast_guard.check(sql)
            if not guard_result.ok:
                print(f"GUARD REJECTED [{tier}] {case_id}: {guard_result.reason} {guard_result.detail}")
                total_fail += 1
                continue
            try:
                exec_result = execute(guard_result.safe_sql, tenant_id=1)
            except ExecutionError as exc:
                print(f"EXECUTION FAILED [{tier}] {case_id}: {exc}")
                total_fail += 1
                continue

            print(f"OK  [{tier:6}] {case_id:35} rows={exec_result.row_count}")
            total_ok += 1
            entries.append({"id": case_id, "question": question, "sql": sql, "difficulty": tier})

        out_path = out_dir / f"{tier}.yml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(entries, f, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
        print(f"-> wrote {len(entries)} cases to {out_path}")

    print(f"\n{total_ok} ok, {total_fail} failed, {total_ok + total_fail} total")
    if total_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
