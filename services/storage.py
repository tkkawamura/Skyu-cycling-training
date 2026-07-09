from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


class LocalStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "cycling_dashboard.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists rpe_entries (
                    ride_date text primary key,
                    rpe integer not null,
                    note text not null default '',
                    updated_at text not null default current_timestamp
                )
                """
            )
            conn.execute(
                """
                create table if not exists assessments (
                    ride_date text primary key,
                    rpe integer,
                    assessment_json text not null,
                    updated_at text not null default current_timestamp
                )
                """
            )

    def list_rpe(self, oldest: str, newest: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select ride_date, rpe
                from rpe_entries
                where ride_date between ? and ?
                order by ride_date
                """,
                (oldest, newest),
            ).fetchall()
        return {row["ride_date"]: row["rpe"] for row in rows}

    def save_rpe(self, ride_date: str, rpe: int, note: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into rpe_entries (ride_date, rpe, note, updated_at)
                values (?, ?, ?, current_timestamp)
                on conflict(ride_date) do update set
                    rpe = excluded.rpe,
                    note = excluded.note,
                    updated_at = current_timestamp
                """,
                (ride_date, rpe, note),
            )

    def delete_rpe(self, ride_date: str) -> None:
        with self._connect() as conn:
            conn.execute("delete from rpe_entries where ride_date = ?", (ride_date,))

    def get_assessment(self, ride_date: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select rpe, assessment_json from assessments where ride_date = ?",
                (ride_date,),
            ).fetchone()
        if not row:
            return None
        return {"rpe": row["rpe"], "assessment": json.loads(row["assessment_json"])}

    def save_assessment(self, ride_date: str, rpe: int | None, assessment: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into assessments (ride_date, rpe, assessment_json, updated_at)
                values (?, ?, ?, current_timestamp)
                on conflict(ride_date) do update set
                    rpe = excluded.rpe,
                    assessment_json = excluded.assessment_json,
                    updated_at = current_timestamp
                """,
                (ride_date, rpe, json.dumps(assessment, ensure_ascii=False)),
            )
