"""Common WearTest data format, CRC32 integrity checks, JSONL storage, and unit conversion."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
import json
import math
import uuid
import zlib


SCHEMA_NAME = "weartest.exchange"
SCHEMA_VERSION = "1.0"


class RecordValidationError(ValueError):
    """Report an incomplete, corrupted, or internally inconsistent record."""


def _utc_now() -> str:
    """Return the current UTC time in a compact ISO 8601 form.

    Returns:
        Current time with a trailing ``Z`` to identify UTC.
    """

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: dict[str, Any]) -> str:
    """Serialize a dictionary deterministically for hashing and storage.

    Args:
        payload: Record dictionary to serialize.

    Returns:
        Compact JSON with sorted keys and no NaN values.
    """

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _checksum(payload: dict[str, Any]) -> str:
    """Calculate the CRC32 integrity value for one canonical payload.

    Args:
        payload: Record body excluding the checksum field itself.

    Returns:
        Eight-character lowercase hexadecimal CRC32 value.

    Notes:
        CRC32 is used here to catch accidental corruption during transfer or
        storage. It is not a cryptographic signature and should not be treated as
        protection against intentional tampering.
    """

    raw_bytes = _canonical_json(payload).encode("utf-8")
    return f"{zlib.crc32(raw_bytes) & 0xFFFFFFFF:08x}"


@dataclass(frozen=True)
class MeasurementRecord:
    """Represent one transport-safe scalar or waveform measurement.

    Args:
        device_id: Device-under-test identifier.
        station_id: Test station that produced or imported the measurement.
        session_id: Unique test-session identifier.
        sequence: Monotonically increasing record number within the session.
        measurement: Canonical measurement name, for example ``accel_x``.
        unit: Canonical engineering unit.
        values: One or more numeric samples.
        timestamp_utc: UTC record timestamp.
        sample_rate_hz: Waveform sample rate, or ``None`` for scalar data.
        test_step: Human-readable acquisition or test step.
        source_format: Original source format such as CSV, TDMS, or simulator.
        source_name: Original file or source identifier.
        quality_flags: Non-fatal quality annotations attached during ingestion.
        schema: Canonical schema family name.
        schema_version: Canonical schema version.
        record_id: Stable logical record identifier used for de-duplication.
    """

    device_id: str
    station_id: str
    session_id: str
    sequence: int
    measurement: str
    unit: str
    values: list[float]
    timestamp_utc: str
    sample_rate_hz: float | None = None
    test_step: str = ""
    source_format: str = "unknown"
    source_name: str = ""
    quality_flags: tuple[str, ...] = ()
    schema: str = SCHEMA_NAME
    schema_version: str = SCHEMA_VERSION
    record_id: str = ""

    def to_dict(self, include_checksum: bool = True) -> dict[str, Any]:
        """Convert the record to a JSON-ready dictionary.

        Args:
            include_checksum: When true, calculate and append ``checksum_crc32``.

        Returns:
            Serializable dictionary containing the complete record.
        """

        payload = asdict(self)
        payload["quality_flags"] = list(self.quality_flags)

        if not payload["record_id"]:
            # A deterministic ID lets transport retries be recognized as the
            # same logical record instead of creating duplicate measurements.
            payload["record_id"] = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{self.session_id}:{self.sequence}:{self.measurement}:{self.timestamp_utc}",
                )
            )

        if include_checksum:
            body = dict(payload)
            body.pop("checksum_crc32", None)
            payload["checksum_crc32"] = _checksum(body)
        return payload

    def to_json(self) -> str:
        """Serialize this record as one canonical JSON string.

        Returns:
            Compact JSON including CRC32 integrity information.
        """

        return _canonical_json(self.to_dict(include_checksum=True))

    @classmethod
    def create(
        cls,
        *,
        device_id: str,
        station_id: str,
        session_id: str,
        sequence: int,
        measurement: str,
        unit: str,
        values: Iterable[float] | float,
        sample_rate_hz: float | None = None,
        test_step: str = "",
        source_format: str = "unknown",
        source_name: str = "",
        quality_flags: Iterable[str] = (),
        timestamp_utc: str | None = None,
    ) -> "MeasurementRecord":
        """Create a normalized record from scalar or iterable measurement data.

        Args:
            device_id: Device-under-test identifier.
            station_id: Test station identifier.
            session_id: Unique test-session identifier.
            sequence: Record order within the session.
            measurement: Canonical measurement name.
            unit: Canonical engineering unit.
            values: Scalar value or iterable of waveform samples.
            sample_rate_hz: Waveform sample rate when applicable.
            test_step: Human-readable acquisition step.
            source_format: Original source format.
            source_name: Original source filename or identifier.
            quality_flags: Optional non-fatal quality annotations.
            timestamp_utc: Optional explicit UTC timestamp.

        Returns:
            Immutable MeasurementRecord with values normalized to floats.
        """

        if isinstance(values, (int, float)):
            normalized_values = [float(values)]
        else:
            normalized_values = [float(value) for value in values]

        return cls(
            device_id=device_id,
            station_id=station_id,
            session_id=session_id,
            sequence=int(sequence),
            measurement=measurement,
            unit=unit,
            values=normalized_values,
            timestamp_utc=timestamp_utc or _utc_now(),
            sample_rate_hz=None if sample_rate_hz is None else float(sample_rate_hz),
            test_step=test_step,
            source_format=source_format,
            source_name=source_name,
            quality_flags=tuple(quality_flags),
        )

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        verify_checksum: bool = True,
    ) -> "MeasurementRecord":
        """Validate and rebuild a MeasurementRecord from a dictionary.

        Args:
            payload: Parsed JSON record dictionary.
            verify_checksum: Whether CRC32 must be present and valid.

        Returns:
            Validated MeasurementRecord.

        Raises:
            RecordValidationError: If schema, checksum, values, or fields are invalid.
        """

        data = dict(payload)
        checksum = data.pop("checksum_crc32", None)

        if verify_checksum:
            if not checksum:
                raise RecordValidationError("Record is missing checksum_crc32.")
            actual_checksum = _checksum(data)
            if actual_checksum != checksum:
                raise RecordValidationError(
                    f"Checksum mismatch: expected {checksum}, calculated {actual_checksum}."
                )

        if data.get("schema") != SCHEMA_NAME:
            raise RecordValidationError(f"Unsupported schema: {data.get('schema')!r}.")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise RecordValidationError(
                f"Unsupported schema version: {data.get('schema_version')!r}."
            )

        required = (
            "device_id",
            "station_id",
            "session_id",
            "sequence",
            "measurement",
            "unit",
            "values",
            "timestamp_utc",
        )
        missing = [name for name in required if name not in data]
        if missing:
            raise RecordValidationError(f"Missing required fields: {', '.join(missing)}")

        values = data["values"]
        if not isinstance(values, list) or not values:
            raise RecordValidationError("values must be a non-empty list.")
        if any(
            not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in values
        ):
            raise RecordValidationError("values must contain only finite numeric values.")

        if int(data["sequence"]) < 0:
            raise RecordValidationError("sequence must be non-negative.")
        if data.get("sample_rate_hz") is not None and float(data["sample_rate_hz"]) <= 0:
            raise RecordValidationError("sample_rate_hz must be positive when provided.")

        return cls(
            device_id=str(data["device_id"]),
            station_id=str(data["station_id"]),
            session_id=str(data["session_id"]),
            sequence=int(data["sequence"]),
            measurement=str(data["measurement"]),
            unit=str(data["unit"]),
            values=[float(value) for value in values],
            timestamp_utc=str(data["timestamp_utc"]),
            sample_rate_hz=(
                None
                if data.get("sample_rate_hz") is None
                else float(data["sample_rate_hz"])
            ),
            test_step=str(data.get("test_step", "")),
            source_format=str(data.get("source_format", "unknown")),
            source_name=str(data.get("source_name", "")),
            quality_flags=tuple(data.get("quality_flags", [])),
            schema=str(data.get("schema", SCHEMA_NAME)),
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
            record_id=str(data.get("record_id", "")),
        )

    @classmethod
    def from_json(cls, line: str, verify_checksum: bool = True) -> "MeasurementRecord":
        """Validate and rebuild a MeasurementRecord from one JSON line.

        Args:
            line: JSON string containing one record.
            verify_checksum: Whether the CRC32 must be checked.

        Returns:
            Validated MeasurementRecord.
        """

        return cls.from_dict(json.loads(line), verify_checksum=verify_checksum)


def write_jsonl(path: str | Path, records: Iterable[MeasurementRecord]) -> None:
    """Write canonical records through a temporary file before replacement.

    Args:
        path: Destination JSONL file.
        records: Canonical records to serialize.

    Notes:
        Replacing a completed temporary file reduces the chance of leaving a
        half-written batch after a local crash. It is not a substitute for a
        transactional network transport in a production factory system.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(record.to_json())
            handle.write("\n")
        handle.flush()
    temporary.replace(destination)


def read_jsonl(
    path: str | Path,
    verify_checksum: bool = True,
) -> list[MeasurementRecord]:
    """Read, verify, de-duplicate, and order-check a WearTest JSONL file.

    Args:
        path: Source WearTest Exchange JSONL file.
        verify_checksum: Whether every record CRC32 must be validated.

    Returns:
        Ordered list of unique canonical records.

    Raises:
        RecordValidationError: If any record is corrupted or out of order.
    """

    records: list[MeasurementRecord] = []
    seen_ids: set[str] = set()
    previous_sequence: dict[str, int] = {}

    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = MeasurementRecord.from_json(
                    line,
                    verify_checksum=verify_checksum,
                )
            except Exception as exc:
                raise RecordValidationError(
                    f"Invalid record at line {line_number}: {exc}"
                ) from exc

            record_id = record.to_dict(include_checksum=False)["record_id"]
            if record_id in seen_ids:
                # Network retries can legitimately resend a record. Keeping the
                # first verified copy prevents duplicate measurements downstream.
                continue
            seen_ids.add(record_id)

            last_sequence = previous_sequence.get(record.session_id)
            if last_sequence is not None and record.sequence <= last_sequence:
                raise RecordValidationError(
                    f"Out-of-order sequence in session {record.session_id}: "
                    f"{record.sequence} after {last_sequence}."
                )
            previous_sequence[record.session_id] = record.sequence
            records.append(record)

    return records


# Unit conversion is kept here because it is part of data normalization.
_STANDARD_UNITS = {
    "v": "V",
    "mv": "mV",
    "g": "g",
    "m/s^2": "m/s^2",
    "m/s2": "m/s^2",
    "deg/s": "deg/s",
    "degree/s": "deg/s",
    "rad/s": "rad/s",
    "degc": "degC",
    "c": "degC",
    "°c": "degC",
    "kohm": "kohm",
    "kω": "kohm",
    "ohm": "ohm",
    "ω": "ohm",
    "normalized": "normalized",
}


def normalize_unit(unit: str) -> str:
    """Normalize common unit spellings to WearTest's preferred labels.

    Args:
        unit: Unit label supplied by the source or mapping file.

    Returns:
        Canonical unit spelling.

    Raises:
        ValueError: If the unit is empty or unsupported.
    """

    key = unit.strip().lower()
    if not key:
        raise ValueError("Unit cannot be empty.")
    if key not in _STANDARD_UNITS:
        raise ValueError(f"Unsupported unit {unit!r}.")
    return _STANDARD_UNITS[key]


def convert_values(values: Iterable[float], source_unit: str, target_unit: str) -> list[float]:
    """Convert numeric values between supported engineering units.

    Args:
        values: Numeric scalar samples to convert.
        source_unit: Unit used in the acquisition file.
        target_unit: Unit expected by WearTest's canonical measurement.

    Returns:
        Converted values as ordinary Python floats.

    Raises:
        ValueError: If no supported conversion exists between the two units.
    """

    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    data = [float(value) for value in values]

    if source == target:
        return data

    # Voltage conversions.
    if source == "mV" and target == "V":
        return [value / 1000.0 for value in data]
    if source == "V" and target == "mV":
        return [value * 1000.0 for value in data]

    # Motion conversions.
    if source == "m/s^2" and target == "g":
        return [value / 9.80665 for value in data]
    if source == "g" and target == "m/s^2":
        return [value * 9.80665 for value in data]
    if source == "rad/s" and target == "deg/s":
        return [value * 57.29577951308232 for value in data]
    if source == "deg/s" and target == "rad/s":
        return [value / 57.29577951308232 for value in data]

    # Resistance conversions.
    if source == "ohm" and target == "kohm":
        return [value / 1000.0 for value in data]
    if source == "kohm" and target == "ohm":
        return [value * 1000.0 for value in data]

    raise ValueError(f"No supported conversion from {source_unit!r} to {target_unit!r}.")
