"""Convert external acquisition files into the common WearTest data format."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import importlib.util
import inspect
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any
import uuid

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from weartest.data import MeasurementRecord, convert_values, normalize_unit, read_jsonl



DEFAULT_USER_ADAPTER_DIR = PROJECT_ROOT / "user_adapters"


class DataAdapter(ABC):
    """Base class for every external-data converter.

    A new file format only needs an adapter that implements this interface. The
    manufacturing test engine never needs to know whether the original data came
    from LabVIEW, CSV, a custom binary logger, BLE packets, or another source.
    """

    format_name = "unknown"
    display_name = "Unknown format"
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def convert(self, source: str | Path, **context: Any) -> list[MeasurementRecord]:
        """Convert one source file into WearTest Exchange records.

        Args:
            source: Path to the source acquisition file.
            **context: Conversion metadata. Built-in adapters expect device ID,
                station ID, session ID, and may accept mapping information or a
                starting sequence number.

        Returns:
            Canonical measurement records ready for integrity checks and testing.

        Raises:
            ValueError: If the source or supplied conversion context is invalid.
        """

        raise NotImplementedError


@dataclass(frozen=True)
class ChannelMapping:
    """Describe how one source channel becomes a canonical WearTest measurement.

    Args:
        measurement: Canonical WearTest measurement name, such as ``accel_x``.
        source_unit: Unit used by the source acquisition channel.
        target_unit: Unit expected by the WearTest test engine.
    """

    measurement: str
    source_unit: str
    target_unit: str


class WideCsvAdapter(DataAdapter):
    """Convert a typical wide acquisition CSV into WearTest Exchange records.

    Each mapped signal column becomes one canonical measurement record. Source
    units are explicit and may be converted to the canonical units expected by
    the manufacturing test engine.

    Args:
        column_map: Mapping from source CSV column name to :class:`ChannelMapping`.
            For backward compatibility, a two-item tuple ``(measurement, unit)``
            is also accepted and means the source is already in the target unit.
        time_column: CSV column containing monotonically increasing time values.
    """

    format_name = "csv"
    display_name = "CSV acquisition"
    extensions = (".csv",)

    def __init__(
        self,
        column_map: dict[str, ChannelMapping | tuple[str, str]],
        time_column: str = "time_s",
    ) -> None:
        """Create a CSV adapter for a known source-column layout.

        Args:
            column_map: Mapping from source column names to canonical channels.
            time_column: Name of the time column, expressed in seconds.
        """

        self.column_map = {
            column: self._coerce_mapping(mapping)
            for column, mapping in column_map.items()
        }
        self.time_column = time_column

    @staticmethod
    def _coerce_mapping(mapping: ChannelMapping | tuple[str, str]) -> ChannelMapping:
        """Normalize older tuple mappings into a ChannelMapping object.

        Args:
            mapping: Existing :class:`ChannelMapping` or ``(measurement, unit)``.

        Returns:
            Normalized :class:`ChannelMapping` instance.
        """

        if isinstance(mapping, ChannelMapping):
            return mapping
        measurement, unit = mapping
        return ChannelMapping(measurement, unit, unit)

    def convert(self, source: str | Path, **context: Any) -> list[MeasurementRecord]:
        """Convert one wide CSV file into canonical measurement records.

        Args:
            source: Path to the CSV acquisition file.
            **context: Requires ``device_id``, ``station_id``, and ``session_id``.
                Optional keys include ``sequence_start`` and ``test_step``.

        Returns:
            Canonical records in the same order as ``column_map``.

        Raises:
            ValueError: If required metadata, columns, or numeric data are invalid.
        """

        source_path = Path(source)
        frame = pd.read_csv(source_path)
        self._validate_context(context)
        sample_rate_hz = self._sample_rate(frame)

        records: list[MeasurementRecord] = []
        sequence = int(context.get("sequence_start", 0))

        for source_column, mapping in self.column_map.items():
            if source_column not in frame.columns:
                raise ValueError(f"CSV is missing mapped column {source_column!r}.")

            values = pd.to_numeric(frame[source_column], errors="coerce")
            if values.isna().any():
                raise ValueError(
                    f"Column {source_column!r} contains non-numeric or missing values."
                )

            converted = convert_values(
                values.tolist(),
                mapping.source_unit,
                mapping.target_unit,
            )
            records.append(
                MeasurementRecord.create(
                    device_id=str(context["device_id"]),
                    station_id=str(context["station_id"]),
                    session_id=str(context["session_id"]),
                    sequence=sequence,
                    measurement=mapping.measurement,
                    unit=normalize_unit(mapping.target_unit),
                    values=converted,
                    sample_rate_hz=sample_rate_hz,
                    test_step=str(context.get("test_step", "Imported acquisition")),
                    source_format=self.format_name,
                    source_name=source_path.name,
                )
            )
            sequence += 1

        return records

    def _sample_rate(self, frame: pd.DataFrame) -> float:
        """Calculate a representative sample rate from the CSV time column.

        Args:
            frame: Loaded CSV table.

        Returns:
            Sample rate in hertz based on median time spacing.

        Raises:
            ValueError: If the time column is missing, too short, or invalid.
        """

        if self.time_column not in frame.columns:
            raise ValueError(f"CSV is missing required time column {self.time_column!r}.")
        if len(frame) < 2:
            raise ValueError("CSV must contain at least two samples.")

        time_values = pd.to_numeric(frame[self.time_column], errors="coerce")
        if time_values.isna().any():
            raise ValueError("Time column contains non-numeric or missing values.")

        spacing = time_values.diff().dropna()
        if (spacing <= 0).any():
            raise ValueError("Time values must increase strictly.")
        return 1.0 / float(spacing.median())

    @staticmethod
    def _validate_context(context: dict[str, Any]) -> None:
        """Check metadata required for traceable imported acquisitions.

        Args:
            context: Adapter context dictionary supplied by the import service.

        Raises:
            ValueError: If device, station, or session identity is missing.
        """

        required = ("device_id", "station_id", "session_id")
        missing = [key for key in required if not context.get(key)]
        if missing:
            raise ValueError(f"Missing adapter context: {', '.join(missing)}")


class LabViewTdmsAdapter(DataAdapter):
    """Convert selected NI/LabVIEW TDMS channels into WearTest Exchange records.

    Args:
        channel_map: Mapping from ``(group, channel)`` source paths to canonical
            :class:`ChannelMapping` objects. A two-item tuple ``(measurement,
            unit)`` remains supported for files already using canonical units.
    """

    format_name = "tdms"
    display_name = "NI/LabVIEW TDMS"
    extensions = (".tdms",)

    def __init__(
        self,
        channel_map: dict[tuple[str, str], ChannelMapping | tuple[str, str]],
    ) -> None:
        """Create a TDMS adapter for an explicit set of groups and channels.

        Args:
            channel_map: Mapping from TDMS group/channel names to WearTest names.
        """

        self.channel_map = {
            channel_path: self._coerce_mapping(mapping)
            for channel_path, mapping in channel_map.items()
        }

    @staticmethod
    def _coerce_mapping(mapping: ChannelMapping | tuple[str, str]) -> ChannelMapping:
        """Normalize legacy tuple mappings into ChannelMapping objects.

        Args:
            mapping: Existing ChannelMapping or ``(measurement, unit)`` tuple.

        Returns:
            Normalized ChannelMapping.
        """

        if isinstance(mapping, ChannelMapping):
            return mapping
        measurement, unit = mapping
        return ChannelMapping(measurement, unit, unit)

    def convert(self, source: str | Path, **context: Any) -> list[MeasurementRecord]:
        """Convert mapped channels from one TDMS file.

        Args:
            source: Path to the TDMS acquisition file.
            **context: Requires ``device_id``, ``station_id``, and ``session_id``.
                Optional keys include ``sequence_start`` and ``test_step``.

        Returns:
            Canonical measurement records ready for the test engine.

        Raises:
            RuntimeError: If npTDMS is not installed.
            ValueError: If required metadata or a mapped TDMS channel is invalid.
        """

        try:
            from nptdms import TdmsFile
        except ImportError as exc:
            raise RuntimeError(
                "TDMS import requires the 'nptdms' package. Install it with "
                "'pip install nptdms' or 'conda install -c conda-forge nptdms'."
            ) from exc

        self._validate_context(context)
        source_path = Path(source)
        tdms_file = TdmsFile.read(source_path)

        records: list[MeasurementRecord] = []
        sequence = int(context.get("sequence_start", 0))
        for (group_name, channel_name), mapping in self.channel_map.items():
            try:
                channel = tdms_file[group_name][channel_name]
            except KeyError as exc:
                raise ValueError(
                    f"TDMS source is missing channel {group_name}/{channel_name}."
                ) from exc

            raw_values = [float(value) for value in channel[:]]
            if not raw_values:
                raise ValueError(f"TDMS channel {group_name}/{channel_name} is empty.")

            converted = convert_values(
                raw_values,
                mapping.source_unit,
                mapping.target_unit,
            )
            sample_rate_hz = self._sample_rate_from_properties(channel.properties)

            records.append(
                MeasurementRecord.create(
                    device_id=str(context["device_id"]),
                    station_id=str(context["station_id"]),
                    session_id=str(context["session_id"]),
                    sequence=sequence,
                    measurement=mapping.measurement,
                    unit=normalize_unit(mapping.target_unit),
                    values=converted,
                    sample_rate_hz=sample_rate_hz,
                    test_step=str(context.get("test_step", "Imported acquisition")),
                    source_format=self.format_name,
                    source_name=source_path.name,
                )
            )
            sequence += 1

        return records

    @staticmethod
    def _sample_rate_from_properties(properties: dict[str, Any]) -> float | None:
        """Read NI waveform sample spacing when it is available.

        Args:
            properties: TDMS channel property dictionary.

        Returns:
            Sample rate in hertz, or ``None`` when no waveform spacing is stored.
        """

        sample_spacing = properties.get("wf_increment")
        if sample_spacing in (None, 0):
            return None
        return 1.0 / float(sample_spacing)

    @staticmethod
    def _validate_context(context: dict[str, Any]) -> None:
        """Check metadata required for a traceable TDMS import.

        Args:
            context: Adapter context supplied by the import service.

        Raises:
            ValueError: If device, station, or session identity is missing.
        """

        required = ("device_id", "station_id", "session_id")
        missing = [key for key in required if not context.get(key)]
        if missing:
            raise ValueError(f"Missing adapter context: {', '.join(missing)}")


class AdapterRegistry:
    """Keep track of available source-data adapters.

    Users can extend WearTest by dropping a Python file into ``user_adapters``.
    Any concrete :class:`DataAdapter` subclass found in that file is registered
    without changes to the manufacturing test engine.
    """

    def __init__(self) -> None:
        """Create an empty adapter registry."""

        self._adapter_types: dict[str, type[DataAdapter]] = {}

    def register(self, adapter_type: type[DataAdapter]) -> None:
        """Register one adapter class by its declared format name.

        Args:
            adapter_type: Concrete subclass of :class:`DataAdapter`.

        Raises:
            TypeError: If the supplied class is not a concrete DataAdapter.
            ValueError: If the adapter does not provide a usable format name.
        """

        if not inspect.isclass(adapter_type) or not issubclass(adapter_type, DataAdapter):
            raise TypeError("Adapter must be a DataAdapter subclass.")
        if inspect.isabstract(adapter_type):
            raise TypeError("Cannot register an abstract DataAdapter class.")

        format_name = str(getattr(adapter_type, "format_name", "")).strip().lower()
        if not format_name or format_name == "unknown":
            raise ValueError("Adapter must define a unique format_name.")
        self._adapter_types[format_name] = adapter_type

    def get(self, format_name: str) -> type[DataAdapter]:
        """Return the adapter class registered for a source format.

        Args:
            format_name: Short format identifier such as ``csv`` or ``tdms``.

        Returns:
            Registered adapter class.

        Raises:
            KeyError: If no adapter is registered for the requested format.
        """

        key = format_name.strip().lower()
        if key not in self._adapter_types:
            raise KeyError(f"No adapter is registered for format {format_name!r}.")
        return self._adapter_types[key]

    def formats(self) -> dict[str, type[DataAdapter]]:
        """Return a copy of the currently registered adapter classes.

        Returns:
            Mapping from format name to adapter class.
        """

        return dict(self._adapter_types)

    def detect_format(self, source: str | Path) -> str | None:
        """Infer a registered format from a file extension.

        Args:
            source: Source file whose extension should be inspected.

        Returns:
            Registered format name, or ``None`` when no adapter claims the file.
        """

        suffix = Path(source).suffix.lower()
        for format_name, adapter_type in self._adapter_types.items():
            extensions = tuple(ext.lower() for ext in getattr(adapter_type, "extensions", ()))
            if suffix in extensions:
                return format_name
        return None

    def discover(self, directory: str | Path) -> list[str]:
        """Load concrete DataAdapter subclasses from a user plugin directory.

        Args:
            directory: Folder containing user-written ``.py`` adapter modules.

        Returns:
            Format names that were successfully registered from the folder.

        Raises:
            RuntimeError: If a plugin file cannot be imported.
        """

        plugin_dir = Path(directory)
        if not plugin_dir.exists():
            return []

        discovered: list[str] = []
        for path in sorted(plugin_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module = self._load_module(path)
            for _, adapter_type in inspect.getmembers(module, inspect.isclass):
                if adapter_type is DataAdapter or not issubclass(adapter_type, DataAdapter):
                    continue
                if inspect.isabstract(adapter_type):
                    continue
                self.register(adapter_type)
                discovered.append(adapter_type.format_name)
        return discovered

    @staticmethod
    def _load_module(path: Path) -> ModuleType:
        """Import one adapter module from an arbitrary file path.

        Args:
            path: Python module path to load.

        Returns:
            Imported module object.

        Raises:
            RuntimeError: If Python cannot build or execute the module.
        """

        module_name = f"weartest_user_adapter_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load adapter module: {path}")

        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise RuntimeError(f"Failed to load adapter {path.name}: {exc}") from exc
        return module


class ExternalDataImporter:
    """Convert user-supplied acquisitions into WearTest Exchange records.

    Built-in CSV and TDMS converters are always available. Additional converters
    can be discovered from ``user_adapters`` without modifying this class.

    Args:
        user_adapter_dir: Optional directory containing user-written adapters.
    """

    def __init__(self, user_adapter_dir: str | Path | None = None) -> None:
        """Create an importer and discover any user adapter plugins.

        Args:
            user_adapter_dir: Folder containing custom DataAdapter subclasses.
        """

        self.registry = AdapterRegistry()
        self.registry.register(WideCsvAdapter)
        self.registry.register(LabViewTdmsAdapter)
        self.user_adapter_dir = Path(user_adapter_dir or DEFAULT_USER_ADAPTER_DIR)
        self.registry.discover(self.user_adapter_dir)

    def available_formats(self) -> dict[str, str]:
        """Return source formats currently available to the application.

        Returns:
            Mapping from format name to user-facing display name.
        """

        formats = {"jsonl": "WearTest Exchange JSONL"}
        for name, adapter_type in self.registry.formats().items():
            formats[name] = adapter_type.display_name
        return formats

    def import_file(
        self,
        source: str | Path,
        *,
        device_id: str,
        station_id: str,
        source_format: str = "auto",
        mapping_path: str | Path | None = None,
        session_id: str | None = None,
    ) -> list[MeasurementRecord]:
        """Import one acquisition file into canonical WearTest records.

        Args:
            source: Path to a supported source acquisition file.
            device_id: Manufacturing device identifier assigned to the data.
            station_id: Test station identifier used for traceability.
            source_format: ``auto``, ``jsonl``, ``csv``, ``tdms``, or a custom
                adapter format name discovered from ``user_adapters``.
            mapping_path: JSON channel-mapping file required by CSV and TDMS.
            session_id: Optional existing session ID. A UUID is created otherwise.

        Returns:
            Canonical measurement records ready for integrity validation and test.

        Raises:
            FileNotFoundError: If source or mapping files do not exist.
            ValueError: If the format, mapping, or identity fields are invalid.
        """

        source_path = Path(source)
        if not source_path.exists():
            raise FileNotFoundError(f"Acquisition file not found: {source_path}")
        if not device_id.strip():
            raise ValueError("Device ID is required for imported data.")
        if not station_id.strip():
            raise ValueError("Test station ID is required for imported data.")

        selected_format = self._resolve_format(source_path, source_format)
        if selected_format == "jsonl":
            return read_jsonl(source_path, verify_checksum=True)

        mapping = self._load_mapping(mapping_path)
        adapter = self._build_adapter(selected_format, mapping)
        return adapter.convert(
            source_path,
            device_id=device_id.strip(),
            station_id=station_id.strip(),
            session_id=session_id or str(uuid.uuid4()),
            test_step="Imported acquisition",
        )

    def _resolve_format(self, source: Path, source_format: str) -> str:
        """Resolve an explicit or extension-derived source format.

        Args:
            source: Acquisition path used for extension detection.
            source_format: Requested source format or ``auto``.

        Returns:
            Normalized source format name.

        Raises:
            ValueError: If no converter can be selected.
        """

        requested = source_format.strip().lower()
        if requested != "auto":
            if requested == "jsonl" or requested in self.registry.formats():
                return requested
            raise ValueError(f"Unsupported source format {source_format!r}.")

        if source.suffix.lower() == ".jsonl":
            return "jsonl"
        detected = self.registry.detect_format(source)
        if detected is None:
            raise ValueError(
                f"Could not determine a converter for {source.name}. "
                "Choose a format explicitly or add a custom adapter."
            )
        return detected

    @staticmethod
    def _load_mapping(mapping_path: str | Path | None) -> dict[str, Any]:
        """Load a JSON channel mapping used by a structured source adapter.

        Args:
            mapping_path: Path to the mapping JSON file.

        Returns:
            Parsed mapping dictionary.

        Raises:
            ValueError: If no mapping was supplied.
            FileNotFoundError: If the mapping path does not exist.
        """

        if mapping_path is None or not str(mapping_path).strip():
            raise ValueError("CSV and TDMS imports require a channel mapping JSON file.")
        path = Path(mapping_path)
        if not path.exists():
            raise FileNotFoundError(f"Mapping file not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _build_adapter(self, source_format: str, mapping: dict[str, Any]) -> DataAdapter:
        """Create a configured built-in or custom adapter.

        Args:
            source_format: Registered adapter format name.
            mapping: Parsed mapping configuration for the source.

        Returns:
            Configured DataAdapter instance.

        Raises:
            ValueError: If a built-in mapping is malformed.
            TypeError: If a custom adapter does not accept ``mapping`` data.
        """

        if source_format == "csv":
            channels = self._parse_csv_channels(mapping)
            return WideCsvAdapter(
                channels,
                time_column=str(mapping.get("time_column", "time_s")),
            )
        if source_format == "tdms":
            channels = self._parse_tdms_channels(mapping)
            return LabViewTdmsAdapter(channels)

        adapter_type = self.registry.get(source_format)
        try:
            return adapter_type(mapping=mapping)
        except TypeError as exc:
            raise TypeError(
                f"Custom adapter {adapter_type.__name__} must accept a 'mapping' "
                "keyword argument in its constructor."
            ) from exc

    @staticmethod
    def _channel_mapping(data: dict[str, Any]) -> ChannelMapping:
        """Turn one mapping JSON entry into a typed ChannelMapping.

        Args:
            data: Mapping entry containing measurement and unit fields.

        Returns:
            ChannelMapping used by a built-in adapter.

        Raises:
            ValueError: If a required field is missing.
        """

        required = ("measurement", "source_unit", "target_unit")
        missing = [key for key in required if not str(data.get(key, "")).strip()]
        if missing:
            raise ValueError(f"Channel mapping is missing: {', '.join(missing)}")
        return ChannelMapping(
            measurement=str(data["measurement"]),
            source_unit=str(data["source_unit"]),
            target_unit=str(data["target_unit"]),
        )

    @classmethod
    def _parse_csv_channels(cls, mapping: dict[str, Any]) -> dict[str, ChannelMapping]:
        """Parse CSV channel mappings from JSON configuration.

        Args:
            mapping: Full mapping configuration dictionary.

        Returns:
            Mapping keyed by CSV column name.
        """

        channels = mapping.get("channels")
        if not isinstance(channels, dict) or not channels:
            raise ValueError("CSV mapping must contain a non-empty 'channels' object.")
        return {
            str(source_column): cls._channel_mapping(channel_data)
            for source_column, channel_data in channels.items()
        }

    @classmethod
    def _parse_tdms_channels(
        cls,
        mapping: dict[str, Any],
    ) -> dict[tuple[str, str], ChannelMapping]:
        """Parse TDMS group/channel mappings from JSON configuration.

        Args:
            mapping: Full mapping configuration dictionary.

        Returns:
            Mapping keyed by ``(group, channel)`` tuples.
        """

        channels = mapping.get("channels")
        if not isinstance(channels, list) or not channels:
            raise ValueError("TDMS mapping must contain a non-empty 'channels' list.")

        parsed: dict[tuple[str, str], ChannelMapping] = {}
        for entry in channels:
            group = str(entry.get("group", "")).strip()
            channel = str(entry.get("channel", "")).strip()
            if not group or not channel:
                raise ValueError("Each TDMS mapping entry needs 'group' and 'channel'.")
            parsed[(group, channel)] = cls._channel_mapping(entry)
        return parsed
