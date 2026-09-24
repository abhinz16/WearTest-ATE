"""Local production traceability and fallback storage for canonical records."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from weartest.data import MeasurementRecord, read_jsonl, write_jsonl
from weartest.grr import GrrStudyResult
from weartest.cycle_time import CycleTimeStudyResult
from weartest.test_engine import DeviceDisposition, TesterHealthResult


class ProductionDatabase:
    """Store device-level and step-level manufacturing test results.

    Args:
        path: SQLite database path used by the local test station.
    """

    def __init__(self, path: str | Path) -> None:
        """Create or open the traceability database.

        Args:
            path: SQLite database file location.
        """

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """Open one SQLite connection with row names and foreign keys enabled.

        Returns:
            Configured SQLite connection.
        """

        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        """Create database tables and indexes when they do not already exist."""

        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    product_variant TEXT NOT NULL,
                    overall_result TEXT NOT NULL CHECK(overall_result IN ('PASS','FAIL')),
                    failure_count INTEGER NOT NULL,
                    raw_record_path TEXT NOT NULL,
                    created_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS step_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    result TEXT NOT NULL CHECK(result IN ('PASS','FAIL')),
                    measured TEXT NOT NULL,
                    specification TEXT NOT NULL,
                    failure_code TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS golden_checks (
                    check_id TEXT PRIMARY KEY,
                    golden_device_id TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    check_number INTEGER NOT NULL,
                    health_status TEXT NOT NULL
                        CHECK(health_status IN ('Healthy','Warning','Needs attention')),
                    worst_metric TEXT NOT NULL,
                    max_bias_ratio REAL NOT NULL,
                    raw_record_path TEXT NOT NULL,
                    created_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS golden_metric_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    check_id TEXT NOT NULL,
                    metric_key TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    measured REAL NOT NULL,
                    reference REAL NOT NULL,
                    bias REAL NOT NULL,
                    tolerance REAL NOT NULL,
                    health_status TEXT NOT NULL
                        CHECK(health_status IN ('Healthy','Warning','Needs attention')),
                    FOREIGN KEY(check_id) REFERENCES golden_checks(check_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS grr_studies (
                    study_id TEXT PRIMARY KEY,
                    metric_key TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    part_count INTEGER NOT NULL,
                    station_count INTEGER NOT NULL,
                    trials_per_station INTEGER NOT NULL,
                    repeatability_sigma REAL NOT NULL,
                    station_sigma REAL NOT NULL,
                    interaction_sigma REAL NOT NULL,
                    reproducibility_sigma REAL NOT NULL,
                    grr_sigma REAL NOT NULL,
                    part_sigma REAL NOT NULL,
                    total_sigma REAL NOT NULL,
                    percent_grr REAL NOT NULL,
                    ndc INTEGER NOT NULL,
                    assessment TEXT NOT NULL
                        CHECK(assessment IN ('Acceptable','Review','Needs improvement')),
                    created_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS grr_measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    study_id TEXT NOT NULL,
                    part_id TEXT NOT NULL,
                    reference_value REAL NOT NULL,
                    station_id TEXT NOT NULL,
                    trial INTEGER NOT NULL,
                    measured_value REAL NOT NULL,
                    error REAL NOT NULL,
                    FOREIGN KEY(study_id) REFERENCES grr_studies(study_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS grr_station_bias (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    study_id TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    mean_bias REAL NOT NULL,
                    FOREIGN KEY(study_id) REFERENCES grr_studies(study_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS cycle_time_studies (
                    study_id TEXT PRIMARY KEY,
                    product_name TEXT NOT NULL,
                    validation_devices INTEGER NOT NULL,
                    recommended_key TEXT NOT NULL,
                    baseline_cycle_seconds REAL NOT NULL,
                    recommended_cycle_seconds REAL NOT NULL,
                    reduction_percent REAL NOT NULL,
                    throughput_units_per_hour REAL NOT NULL,
                    created_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cycle_time_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    study_id TEXT NOT NULL,
                    candidate_key TEXT NOT NULL,
                    candidate_name TEXT NOT NULL,
                    is_baseline INTEGER NOT NULL,
                    cycle_seconds REAL NOT NULL,
                    units_per_hour REAL NOT NULL,
                    reduction_percent REAL NOT NULL,
                    detection_retention_percent REAL NOT NULL,
                    escape_rate_percent REAL NOT NULL,
                    false_reject_rate_percent REAL NOT NULL,
                    false_reject_change_pp REAL NOT NULL,
                    agreement_percent REAL NOT NULL,
                    validated INTEGER NOT NULL,
                    decision_reason TEXT NOT NULL,
                    FOREIGN KEY(study_id) REFERENCES cycle_time_studies(study_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_utc);
                CREATE INDEX IF NOT EXISTS idx_steps_code ON step_results(failure_code);
                CREATE INDEX IF NOT EXISTS idx_golden_station ON golden_checks(station_id);
                CREATE INDEX IF NOT EXISTS idx_golden_created ON golden_checks(created_utc);
                CREATE INDEX IF NOT EXISTS idx_golden_metric_check ON golden_metric_results(check_id);
                CREATE INDEX IF NOT EXISTS idx_grr_created ON grr_studies(created_utc);
                CREATE INDEX IF NOT EXISTS idx_grr_measurements_study ON grr_measurements(study_id);
                CREATE INDEX IF NOT EXISTS idx_grr_bias_study ON grr_station_bias(study_id);
                CREATE INDEX IF NOT EXISTS idx_cycle_time_created ON cycle_time_studies(created_utc);
                CREATE INDEX IF NOT EXISTS idx_cycle_time_candidates_study ON cycle_time_candidates(study_id);
                """
            )

    def save_disposition(
        self,
        disposition: DeviceDisposition,
        *,
        station_id: str,
        product_variant: str,
        raw_record_path: str,
    ) -> None:
        """Persist one completed device disposition and all step results.

        Args:
            disposition: Overall and step-level manufacturing test results.
            station_id: Station responsible for the result.
            product_variant: Product configuration tested.
            raw_record_path: Path to canonical data retained for traceability.
        """

        created_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, device_id, station_id, product_variant,
                    overall_result, failure_count, raw_record_path, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    disposition.session_id,
                    disposition.device_id,
                    station_id,
                    product_variant,
                    "PASS" if disposition.passed else "FAIL",
                    len(disposition.failure_codes),
                    raw_record_path,
                    created_utc,
                ),
            )
            connection.executemany(
                """
                INSERT INTO step_results (
                    session_id, step_name, result, measured, specification, failure_code
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        disposition.session_id,
                        result.name,
                        "PASS" if result.passed else "FAIL",
                        result.measured,
                        result.limit,
                        result.failure_code,
                    )
                    for result in disposition.results
                ],
            )

    def golden_check_count(self, station_id: str) -> int:
        """Return how many golden-unit checks have been stored for one station.

        Args:
            station_id: Test station identifier.

        Returns:
            Number of completed golden-unit checks stored for the station.
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM golden_checks WHERE station_id = ?",
                (station_id,),
            ).fetchone()
        return int(row["count"])

    def save_golden_check(
        self,
        result: TesterHealthResult,
        *,
        raw_record_path: str,
    ) -> None:
        """Persist one golden-unit tester-health check and its metric biases.

        Args:
            result: Completed tester-health result from a golden-unit acquisition.
            raw_record_path: Canonical JSONL file retained for traceability.
        """

        created_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO golden_checks (
                    check_id, golden_device_id, station_id, check_number,
                    health_status, worst_metric, max_bias_ratio, raw_record_path,
                    created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.check_id, result.golden_device_id, result.station_id,
                    result.check_number, result.status, result.worst_metric,
                    result.max_bias_ratio, raw_record_path, created_utc,
                ),
            )
            connection.executemany(
                """
                INSERT INTO golden_metric_results (
                    check_id, metric_key, metric_name, unit, measured, reference,
                    bias, tolerance, health_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result.check_id, metric.key, metric.name, metric.unit,
                        metric.measured, metric.reference, metric.bias,
                        metric.tolerance, metric.status,
                    )
                    for metric in result.metrics
                ],
            )

    def recent_golden_checks(self, limit: int = 30) -> list[dict]:
        """Return recent golden-unit station checks for traceability.

        Args:
            limit: Maximum number of checks to return.

        Returns:
            Golden-unit check dictionaries ordered newest first.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT check_id, golden_device_id, station_id, check_number,
                       health_status, worst_metric, max_bias_ratio, raw_record_path,
                       created_utc
                FROM golden_checks
                ORDER BY created_utc DESC, rowid DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def tester_health_summary(self) -> list[dict]:
        """Return the latest golden-unit health result for every checked station.

        Returns:
            One row per station with latest health, check count, worst metric,
            maximum bias percentage, and timestamp.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT rowid AS internal_order, *,
                           ROW_NUMBER() OVER (
                               PARTITION BY station_id
                               ORDER BY created_utc DESC, rowid DESC
                           ) AS rank_in_station
                    FROM golden_checks
                ), counts AS (
                    SELECT station_id, COUNT(*) AS check_count
                    FROM golden_checks
                    GROUP BY station_id
                )
                SELECT ranked.station_id, ranked.health_status, ranked.check_number,
                       ranked.worst_metric, ranked.max_bias_ratio, ranked.created_utc,
                       counts.check_count
                FROM ranked
                JOIN counts USING (station_id)
                WHERE ranked.rank_in_station = 1
                ORDER BY ranked.station_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def golden_metric_history(
        self, station_id: str, metric_key: str, limit: int = 50
    ) -> list[dict]:
        """Return recent bias history for one station and golden-unit metric.

        Args:
            station_id: Test station identifier.
            metric_key: Stable golden-unit metric key.
            limit: Maximum number of points to return.

        Returns:
            Chronological metric-bias rows for trend analysis.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.check_number, c.created_utc, m.metric_name, m.unit,
                       m.measured, m.reference, m.bias, m.tolerance, m.health_status
                FROM golden_metric_results AS m
                JOIN golden_checks AS c ON c.check_id = m.check_id
                WHERE c.station_id = ? AND m.metric_key = ?
                ORDER BY c.check_number DESC
                LIMIT ?
                """,
                (station_id, metric_key, int(limit)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_grr_study(self, result: GrrStudyResult) -> None:
        """Persist one completed GR&R study and every repeated observation.

        Args:
            result: Completed crossed GR&R study result.
        """

        created_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO grr_studies (
                    study_id, metric_key, metric_name, unit, part_count, station_count,
                    trials_per_station, repeatability_sigma, station_sigma,
                    interaction_sigma, reproducibility_sigma, grr_sigma, part_sigma,
                    total_sigma, percent_grr, ndc, assessment, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.study_id, result.metric_key, result.metric_name, result.unit,
                    result.part_count, result.station_count, result.trials_per_station,
                    result.repeatability_sigma, result.station_sigma,
                    result.interaction_sigma, result.reproducibility_sigma,
                    result.grr_sigma, result.part_sigma, result.total_sigma,
                    result.percent_grr, result.ndc, result.assessment, created_utc,
                ),
            )
            connection.executemany(
                """
                INSERT INTO grr_measurements (
                    study_id, part_id, reference_value, station_id, trial,
                    measured_value, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result.study_id, item.part_id, item.reference_value,
                        item.station_id, item.trial, item.measured_value, item.error,
                    )
                    for item in result.observations
                ],
            )
            connection.executemany(
                """
                INSERT INTO grr_station_bias (study_id, station_id, mean_bias)
                VALUES (?, ?, ?)
                """,
                [
                    (result.study_id, station_id, bias)
                    for station_id, bias in result.station_biases.items()
                ],
            )

    def recent_grr_studies(self, limit: int = 20) -> list[dict]:
        """Return recent GR&R study summaries for the measurement-system workspace.

        Args:
            limit: Maximum number of studies to return.

        Returns:
            Study dictionaries ordered newest first.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT study_id, metric_key, metric_name, unit, part_count,
                       station_count, trials_per_station, repeatability_sigma,
                       reproducibility_sigma, grr_sigma, part_sigma, total_sigma,
                       percent_grr, ndc, assessment, created_utc
                FROM grr_studies
                ORDER BY created_utc DESC, rowid DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def grr_study_measurements(self, study_id: str) -> list[dict]:
        """Return raw repeated measurements for one stored GR&R study.

        Args:
            study_id: GR&R study identifier.

        Returns:
            Measurements ordered by reference part, station, and trial.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT part_id, reference_value, station_id, trial,
                       measured_value, error
                FROM grr_measurements
                WHERE study_id = ?
                ORDER BY part_id, station_id, trial
                """,
                (study_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def grr_station_biases(self, study_id: str) -> list[dict]:
        """Return station mean biases stored for one GR&R study.

        Args:
            study_id: GR&R study identifier.

        Returns:
            Station-bias dictionaries ordered by station identifier.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT station_id, mean_bias
                FROM grr_station_bias
                WHERE study_id = ?
                ORDER BY station_id
                """,
                (study_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_cycle_time_study(self, result: CycleTimeStudyResult) -> None:
        """Persist one cycle-time optimization study and all candidates.

        Args:
            result: Completed cycle-time validation and optimization result.
        """

        created_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        recommended = result.recommended
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cycle_time_studies (
                    study_id, product_name, validation_devices, recommended_key,
                    baseline_cycle_seconds, recommended_cycle_seconds,
                    reduction_percent, throughput_units_per_hour, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.study_id, result.product_name, result.validation_devices,
                    result.recommended_key, result.baseline.cycle_seconds,
                    recommended.cycle_seconds, recommended.reduction_percent,
                    recommended.units_per_hour, created_utc,
                ),
            )
            all_results = (result.baseline, *result.candidates)
            connection.executemany(
                """
                INSERT INTO cycle_time_candidates (
                    study_id, candidate_key, candidate_name, is_baseline,
                    cycle_seconds, units_per_hour, reduction_percent,
                    detection_retention_percent, escape_rate_percent,
                    false_reject_rate_percent, false_reject_change_pp,
                    agreement_percent, validated, decision_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result.study_id, item.candidate.key, item.candidate.name,
                        int(item.candidate.baseline), item.cycle_seconds,
                        item.units_per_hour, item.reduction_percent,
                        item.detection_retention_percent, item.escape_rate_percent,
                        item.false_reject_rate_percent, item.false_reject_change_pp,
                        item.agreement_percent, int(item.validated), item.decision_reason,
                    )
                    for item in all_results
                ],
            )

    def recent_cycle_time_studies(self, limit: int = 20) -> list[dict]:
        """Return recent cycle-time optimization summaries.

        Args:
            limit: Maximum number of studies to return.

        Returns:
            Cycle-time study dictionaries ordered newest first.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT study_id, product_name, validation_devices, recommended_key,
                       baseline_cycle_seconds, recommended_cycle_seconds,
                       reduction_percent, throughput_units_per_hour, created_utc
                FROM cycle_time_studies
                ORDER BY created_utc DESC, rowid DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def cycle_time_candidates(self, study_id: str) -> list[dict]:
        """Return every evaluated candidate for one cycle-time study.

        Args:
            study_id: Cycle-time study identifier.

        Returns:
            Candidate dictionaries ordered by cycle time from slowest to fastest.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT candidate_key, candidate_name, is_baseline, cycle_seconds,
                       units_per_hour, reduction_percent, detection_retention_percent,
                       escape_rate_percent, false_reject_rate_percent,
                       false_reject_change_pp, agreement_percent, validated, decision_reason
                FROM cycle_time_candidates
                WHERE study_id = ?
                ORDER BY cycle_seconds DESC
                """,
                (study_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_sessions(self, limit: int = 50) -> list[dict]:
        """Return the most recent device test sessions.

        Args:
            limit: Maximum number of sessions to return.

        Returns:
            Session dictionaries ordered newest first.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id, device_id, station_id, product_variant,
                       overall_result, failure_count, created_utc
                FROM sessions
                ORDER BY created_utc DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _ordered_sessions(self) -> list[dict]:
        """Return every test session in chronological order.

        Returns:
            Session dictionaries ordered from earliest to latest. The SQLite
            row ID is retained internally to make ties deterministic.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rowid AS test_order, session_id, device_id, station_id,
                       product_variant, overall_result, failure_count, created_utc
                FROM sessions
                ORDER BY created_utc ASC, rowid ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _first_attempts(rows: list[dict]) -> list[dict]:
        """Keep only the first recorded test for each device ID.

        Args:
            rows: Chronologically ordered production sessions.

        Returns:
            First test attempt for every unique device.
        """

        first_attempt_by_device: dict[str, dict] = {}
        for row in rows:
            first_attempt_by_device.setdefault(row["device_id"], row)
        return list(first_attempt_by_device.values())

    def summary(self) -> dict:
        """Calculate true first-pass-yield and high-level production counts.

        Returns:
            Dictionary containing unique devices, total test sessions, devices
            that passed or failed their first attempt, FPY, and top failures.

        Notes:
            First-pass yield uses only the earliest test session for each device.
            Later retests remain visible in traceability but do not rewrite FPY.
        """

        sessions = self._ordered_sessions()
        first_attempts = self._first_attempts(sessions)
        passed = sum(row["overall_result"] == "PASS" for row in first_attempts)
        total = len(first_attempts)

        with self._connect() as connection:
            top_failures = connection.execute(
                """
                SELECT step_name, failure_code, COUNT(*) AS count
                FROM step_results
                WHERE failure_code IS NOT NULL
                GROUP BY step_name, failure_code
                ORDER BY count DESC, step_name ASC
                LIMIT 8
                """
            ).fetchall()

        return {
            "total": total,
            "sessions": len(sessions),
            "passed": passed,
            "failed": total - passed,
            "fpy_percent": (100.0 * passed / total) if total else 0.0,
            "top_failures": [dict(row) for row in top_failures],
        }

    def station_summary(self) -> list[dict]:
        """Calculate first-pass performance for each production test station.

        Returns:
            One dictionary per station with tested, passed, failed, and FPY
            fields. A device belongs to the station used for its first attempt.
        """

        first_attempts = self._first_attempts(self._ordered_sessions())
        stations: dict[str, dict] = {}
        for row in first_attempts:
            station = stations.setdefault(
                row["station_id"],
                {"station_id": row["station_id"], "tested": 0, "passed": 0},
            )
            station["tested"] += 1
            station["passed"] += row["overall_result"] == "PASS"

        output: list[dict] = []
        for station in stations.values():
            tested = station["tested"]
            passed = station["passed"]
            output.append(
                {
                    "station_id": station["station_id"],
                    "tested": tested,
                    "passed": passed,
                    "failed": tested - passed,
                    "fpy_percent": (100.0 * passed / tested) if tested else 0.0,
                }
            )
        return sorted(output, key=lambda row: row["station_id"])

    def failure_summary(self, limit: int = 8) -> list[dict]:
        """Return the most frequent failed acceptance checks.

        Args:
            limit: Maximum number of failure categories to return.

        Returns:
            Failure categories ordered from most to least frequent.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT step_name, failure_code, COUNT(*) AS count
                FROM step_results
                WHERE failure_code IS NOT NULL
                GROUP BY step_name, failure_code
                ORDER BY count DESC, step_name ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def rolling_first_pass_yield(
        self,
        *,
        limit: int = 60,
        window: int = 20,
    ) -> list[dict]:
        """Calculate rolling FPY across recent first-attempt devices.

        Args:
            limit: Maximum number of recent first attempts to return.
            window: Number of first-attempt devices included in each rolling value.

        Returns:
            Ordered points with sequence number, timestamp, and rolling FPY.
        """

        if limit < 1 or window < 1:
            raise ValueError("limit and window must both be at least 1.")

        first_attempts = self._first_attempts(self._ordered_sessions())
        if len(first_attempts) < int(window):
            return []

        points: list[dict] = []
        for end_index in range(int(window), len(first_attempts) + 1):
            recent = first_attempts[end_index - int(window):end_index]
            passed = sum(1 for row in recent if row["overall_result"] == "PASS")
            points.append(
                {
                    "sequence": end_index,
                    "created_utc": first_attempts[end_index - 1]["created_utc"],
                    "fpy_percent": 100.0 * passed / int(window),
                }
            )

        return points[-int(limit):]



class ReliableJsonlSink:
    """Append records to a primary destination with a local fallback spool.

    Args:
        primary_path: Preferred record destination.
        spool_path: Local backup used when the primary write fails.

    Notes:
        A real factory implementation could replace the primary file with a
        network service while keeping the same fallback idea.
    """

    def __init__(self, primary_path: str | Path, spool_path: str | Path) -> None:
        """Prepare primary and fallback paths.

        Args:
            primary_path: Preferred JSONL destination.
            spool_path: Local recovery-spool JSONL destination.
        """

        self.primary_path = Path(primary_path)
        self.spool_path = Path(spool_path)
        self.primary_path.parent.mkdir(parents=True, exist_ok=True)
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _append(path: Path, record: MeasurementRecord) -> None:
        """Append one verified canonical record to a JSONL destination.

        Args:
            path: Destination file.
            record: Canonical measurement record to append.
        """

        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.to_json())
            handle.write("\n")
            handle.flush()

    def send(self, record: MeasurementRecord) -> str:
        """Send one record to primary storage or the fallback spool.

        Args:
            record: Canonical record to preserve.

        Returns:
            ``primary`` when the preferred write succeeds, otherwise ``spool``.
        """

        try:
            self._append(self.primary_path, record)
            return "primary"
        except OSError:
            self._append(self.spool_path, record)
            return "spool"


    def replay_spool(self) -> int:
        """Retry records saved in the recovery spool.

        Returns:
            Number of spooled records successfully copied to primary storage.

        Notes:
            Records that still cannot be delivered remain in the spool. Canonical
            record IDs make repeated delivery attempts safe for downstream
            de-duplication.
        """

        if not self.spool_path.exists():
            return 0

        records = read_jsonl(self.spool_path, verify_checksum=True)
        remaining: list[MeasurementRecord] = []
        replayed = 0

        for record in records:
            try:
                self._append(self.primary_path, record)
                replayed += 1
            except OSError:
                remaining.append(record)

        if remaining:
            write_jsonl(self.spool_path, remaining)
        else:
            self.spool_path.unlink(missing_ok=True)
        return replayed

    def send_many(self, records: Iterable[MeasurementRecord]) -> tuple[int, int]:
        """Send a group of records while counting primary and spooled writes.

        Args:
            records: Canonical records to preserve.

        Returns:
            ``(primary_count, spooled_count)``.
        """

        primary_count = 0
        spooled_count = 0
        for record in records:
            destination = self.send(record)
            if destination == "primary":
                primary_count += 1
            else:
                spooled_count += 1
        return primary_count, spooled_count
