"""Load editable test limits and make device PASS/FAIL decisions."""

from __future__ import annotations

# This is application source, not a pytest test module.
__test__ = False

from configparser import ConfigParser
from dataclasses import dataclass
import math
from pathlib import Path
import sys

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from weartest.data import MeasurementRecord


# The repository keeps the limits outside Python so a test engineer can edit them.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "test_specs.ini"
DEFAULT_GOLDEN_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "golden_units.ini"
DEFAULT_PRODUCTS_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "products.ini"


@dataclass(frozen=True)
class TestSpecifications:
    """Numerical acceptance limits used by the manufacturing test engine.

    The values are intentionally stored outside the source code. This dataclass
    gives the rest of the program a typed, read-only view of those settings.

    Args:
        battery_voltage_min_v: Minimum accepted battery voltage in volts.
        battery_voltage_max_v: Maximum accepted battery voltage in volts.
        accel_axis_bias_max_g: Maximum absolute accelerometer axis bias in g.
        accel_noise_rms_max_g: Maximum accelerometer RMS noise in g.
        gyro_bias_max_dps: Maximum absolute gyroscope bias in degrees per second.
        gyro_noise_rms_max_dps: Maximum gyroscope RMS noise in degrees per second.
        ppg_snr_min_db: Minimum accepted optical signal-to-noise ratio in dB.
        ppg_fixture_frequency_hz: Known optical fixture modulation frequency.
        ppg_saturation_max_fraction: Maximum fraction of saturated optical samples.
        skin_temp_reference_c: Reference temperature used by the simulated fixture.
        skin_temp_error_max_c: Maximum absolute temperature error in degrees Celsius.
        ecg_electrode_impedance_max_kohm: Maximum accepted ECG path impedance.
    """

    battery_voltage_min_v: float
    battery_voltage_max_v: float
    accel_axis_bias_max_g: float
    accel_noise_rms_max_g: float
    gyro_bias_max_dps: float
    gyro_noise_rms_max_dps: float
    ppg_snr_min_db: float
    ppg_fixture_frequency_hz: float
    ppg_saturation_max_fraction: float
    skin_temp_reference_c: float
    skin_temp_error_max_c: float
    ecg_electrode_impedance_max_kohm: float


@dataclass(frozen=True)
class ProductProfile:
    """Configurable product definition used by simulated and imported tests.

    Args:
        name: Human-readable product profile shown in the GUI.
        include_ecg: Whether the profile requires the ECG electrode-path check.
        batch_suffix: Short suffix used to create unique IDs during all-profile simulation.
    """

    name: str
    include_ecg: bool
    batch_suffix: str


def load_product_profiles(path: str | Path | None = None) -> tuple[ProductProfile, ...]:
    """Load product profiles from the user-editable products INI file.

    Args:
        path: Optional path to a products INI file. When omitted, WearTest uses
            ``config/products.ini`` from the project root.

    Returns:
        Product profiles in the order they appear in the INI file.

    Raises:
        FileNotFoundError: If the products configuration file does not exist.
        ValueError: If no product profiles are defined or a profile is invalid.
    """

    config_path = Path(path) if path is not None else DEFAULT_PRODUCTS_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Product configuration file not found: {config_path}")

    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")
    profiles: list[ProductProfile] = []
    seen_names: set[str] = set()
    seen_suffixes: set[str] = set()

    for section in parser.sections():
        if not section.lower().startswith("product:"):
            continue
        name = section.split(":", 1)[1].strip()
        if not name:
            raise ValueError(f"Invalid product section name in {config_path}: {section}")
        if name in seen_names:
            raise ValueError(f"Duplicate product profile name: {name}")
        try:
            include_ecg = parser.getboolean(section, "include_ecg")
            batch_suffix = parser.get(section, "batch_suffix").strip()
        except Exception as exc:
            raise ValueError(f"Invalid product profile [{section}] in {config_path}.") from exc
        if not batch_suffix:
            raise ValueError(f"Product profile {name!r} must define a non-empty batch_suffix.")
        normalized_suffix = "".join(ch if ch.isalnum() else "_" for ch in batch_suffix.upper()).strip("_")
        if not normalized_suffix:
            raise ValueError(f"Product profile {name!r} has an invalid batch_suffix.")
        if normalized_suffix in seen_suffixes:
            raise ValueError(f"Duplicate product batch suffix: {normalized_suffix}")
        seen_names.add(name)
        seen_suffixes.add(normalized_suffix)
        profiles.append(ProductProfile(name=name, include_ecg=include_ecg, batch_suffix=normalized_suffix))

    if not profiles:
        raise ValueError(f"No [product:...] profiles were found in {config_path}.")
    return tuple(profiles)


def load_test_specifications(path: str | Path | None = None) -> TestSpecifications:
    """Load manufacturing test limits from an INI file.

    Args:
        path: Optional path to a specification INI file. When omitted, WearTest
            uses ``config/test_specs.ini`` from the project root.

    Returns:
        A validated :class:`TestSpecifications` instance.

    Raises:
        FileNotFoundError: If the requested configuration file does not exist.
        ValueError: If a required value is missing, non-numeric, or inconsistent.
    """

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Test specification file not found: {config_path}")

    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")

    def get_float(section: str, option: str) -> float:
        """Read one required floating-point setting with a useful error message.

        Args:
            section: INI section name.
            option: INI option name.

        Returns:
            The setting converted to ``float``.

        Raises:
            ValueError: If the setting is missing or cannot be converted.
        """

        try:
            return parser.getfloat(section, option)
        except Exception as exc:
            raise ValueError(
                f"Invalid or missing setting [{section}] {option} in {config_path}."
            ) from exc

    specs = TestSpecifications(
        battery_voltage_min_v=get_float("power", "battery_voltage_min_v"),
        battery_voltage_max_v=get_float("power", "battery_voltage_max_v"),
        accel_axis_bias_max_g=get_float("accelerometer", "axis_bias_max_g"),
        accel_noise_rms_max_g=get_float("accelerometer", "noise_rms_max_g"),
        gyro_bias_max_dps=get_float("gyroscope", "bias_max_dps"),
        gyro_noise_rms_max_dps=get_float("gyroscope", "noise_rms_max_dps"),
        ppg_snr_min_db=get_float("optical_ppg", "snr_min_db"),
        ppg_fixture_frequency_hz=get_float("optical_ppg", "fixture_frequency_hz"),
        ppg_saturation_max_fraction=get_float("optical_ppg", "saturation_max_fraction"),
        skin_temp_reference_c=get_float("temperature", "reference_c"),
        skin_temp_error_max_c=get_float("temperature", "error_max_c"),
        ecg_electrode_impedance_max_kohm=get_float("ecg", "electrode_impedance_max_kohm"),
    )

    if specs.battery_voltage_min_v >= specs.battery_voltage_max_v:
        raise ValueError("Battery minimum voltage must be below the maximum voltage.")
    if not 0.0 <= specs.ppg_saturation_max_fraction <= 1.0:
        raise ValueError("PPG saturation fraction must be between 0 and 1.")

    positive_values = {
        "accelerometer axis bias": specs.accel_axis_bias_max_g,
        "accelerometer noise": specs.accel_noise_rms_max_g,
        "gyroscope bias": specs.gyro_bias_max_dps,
        "gyroscope noise": specs.gyro_noise_rms_max_dps,
        "PPG fixture frequency": specs.ppg_fixture_frequency_hz,
        "temperature error": specs.skin_temp_error_max_c,
        "ECG impedance": specs.ecg_electrode_impedance_max_kohm,
    }
    for name, value in positive_values.items():
        if value <= 0:
            raise ValueError(f"Configured {name} limit must be greater than zero.")

    return specs


@dataclass(frozen=True)
class GoldenUnitConfiguration:
    """Known-good reference values and simulated station drift settings.

    Args:
        device_id: Identifier of the known-good reference wearable.
        include_ecg: Whether the golden-unit check includes the ECG path.
        ppg_fixture_frequency_hz: Optical fixture frequency used to estimate PPG amplitude.
        references: Expected measurement metrics for the known-good unit.
        tolerances: Maximum absolute tester bias allowed for each metric family.
        warning_fraction: Fraction of a tolerance at which a warning is raised.
        station_drift: Per-station initial offsets and per-check drift rates.
    """

    device_id: str
    include_ecg: bool
    ppg_fixture_frequency_hz: float
    references: dict[str, float]
    tolerances: dict[str, float]
    warning_fraction: float
    station_drift: dict[str, dict[str, float]]


def load_golden_unit_configuration(
    path: str | Path | None = None,
) -> GoldenUnitConfiguration:
    """Load golden-unit references and station-drift assumptions from INI.

    Args:
        path: Optional INI path. When omitted, WearTest uses
            ``config/golden_units.ini`` from the project root.

    Returns:
        Validated golden-unit configuration used by simulation and tester-health checks.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
        ValueError: If required values are missing or inconsistent.
    """

    config_path = Path(path) if path is not None else DEFAULT_GOLDEN_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Golden-unit configuration file not found: {config_path}")

    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")

    def get_float(section: str, option: str) -> float:
        """Read one required numeric golden-unit setting.

        Args:
            section: INI section name.
            option: INI option name.

        Returns:
            Requested setting converted to float.
        """

        try:
            return parser.getfloat(section, option)
        except Exception as exc:
            raise ValueError(
                f"Invalid or missing setting [{section}] {option} in {config_path}."
            ) from exc

    references = {
        "battery_voltage": get_float("reference", "battery_voltage_v"),
        "accel_x": get_float("reference", "accel_x_g"),
        "accel_y": get_float("reference", "accel_y_g"),
        "accel_z": get_float("reference", "accel_z_g"),
        "gyro_x": get_float("reference", "gyro_x_dps"),
        "gyro_y": get_float("reference", "gyro_y_dps"),
        "gyro_z": get_float("reference", "gyro_z_dps"),
        "ppg_amplitude": get_float("reference", "ppg_amplitude"),
        "skin_temperature": get_float("reference", "skin_temperature_c"),
        "ecg_electrode_impedance": get_float("reference", "ecg_impedance_kohm"),
    }
    tolerances = {
        "battery_voltage": get_float("bias_limits", "battery_voltage_v"),
        "accel": get_float("bias_limits", "accel_axis_g"),
        "gyro": get_float("bias_limits", "gyro_axis_dps"),
        "ppg_amplitude": get_float("bias_limits", "ppg_amplitude"),
        "skin_temperature": get_float("bias_limits", "skin_temperature_c"),
        "ecg_electrode_impedance": get_float("bias_limits", "ecg_impedance_kohm"),
    }
    warning_fraction = get_float("health", "warning_fraction")
    if not 0.0 < warning_fraction < 1.0:
        raise ValueError("Golden-unit warning_fraction must be between 0 and 1.")
    for name, value in tolerances.items():
        if value <= 0.0:
            raise ValueError(f"Golden-unit tolerance {name!r} must be greater than zero.")

    drift_keys = (
        "battery_offset_v", "battery_drift_per_check_v",
        "accel_x_offset_g", "accel_x_drift_per_check_g",
        "accel_y_offset_g", "accel_y_drift_per_check_g",
        "accel_z_offset_g", "accel_z_drift_per_check_g",
        "gyro_x_offset_dps", "gyro_x_drift_per_check_dps",
        "gyro_y_offset_dps", "gyro_y_drift_per_check_dps",
        "gyro_z_offset_dps", "gyro_z_drift_per_check_dps",
        "ppg_amplitude_offset", "ppg_amplitude_drift_per_check",
        "skin_temperature_offset_c", "skin_temperature_drift_per_check_c",
        "ecg_impedance_offset_kohm", "ecg_impedance_drift_per_check_kohm",
    )
    station_drift: dict[str, dict[str, float]] = {}
    for section in parser.sections():
        if not section.lower().startswith("station:"):
            continue
        station_id = section.split(":", 1)[1].strip()
        if not station_id:
            raise ValueError(f"Invalid station section name in {config_path}: {section}")
        station_drift[station_id] = {key: get_float(section, key) for key in drift_keys}

    if not station_drift:
        raise ValueError("Golden-unit configuration must define at least one [station:...] section.")

    try:
        device_id = parser.get("golden_unit", "device_id").strip()
        include_ecg = parser.getboolean("golden_unit", "include_ecg")
    except Exception as exc:
        raise ValueError(f"Invalid [golden_unit] settings in {config_path}.") from exc
    if not device_id:
        raise ValueError("Golden-unit device_id cannot be blank.")

    return GoldenUnitConfiguration(
        device_id=device_id,
        include_ecg=include_ecg,
        ppg_fixture_frequency_hz=get_float("golden_unit", "ppg_fixture_frequency_hz"),
        references=references,
        tolerances=tolerances,
        warning_fraction=warning_fraction,
        station_drift=station_drift,
    )


@dataclass(frozen=True)
class GoldenMetricResult:
    """Comparison between one measured golden-unit metric and its reference.

    Args:
        name: Human-readable metric name.
        key: Stable metric key used by storage and analysis.
        unit: Engineering unit displayed to the operator.
        measured: Metric measured by the simulated tester.
        reference: Known reference value for the golden unit.
        bias: Signed tester bias, calculated as measured minus reference.
        tolerance: Maximum allowed absolute bias.
        status: Healthy, Warning, or Needs attention for this metric.
    """

    name: str
    key: str
    unit: str
    measured: float
    reference: float
    bias: float
    tolerance: float
    status: str

    @property
    def bias_ratio(self) -> float:
        """Return absolute bias as a fraction of its allowed tolerance.

        Returns:
            Non-negative ratio where 1.0 represents the full action limit.
        """

        return abs(self.bias) / self.tolerance


@dataclass(frozen=True)
class TesterHealthResult:
    """Overall health result from one golden-unit station verification.

    Args:
        check_id: Unique identifier for this golden-unit verification.
        golden_device_id: Identifier of the known-good reference device.
        station_id: Test station being evaluated.
        check_number: Sequential golden check number for this station.
        status: Healthy, Warning, or Needs attention.
        metrics: Individual measured-to-reference comparisons.
        worst_metric: Metric with the largest fraction of its allowed bias.
        max_bias_ratio: Largest absolute-bias-to-tolerance ratio.
    """

    check_id: str
    golden_device_id: str
    station_id: str
    check_number: int
    status: str
    metrics: tuple[GoldenMetricResult, ...]
    worst_metric: str
    max_bias_ratio: float


class GoldenUnitEvaluator:
    """Evaluate a known-good wearable to determine test-station health.

    Args:
        config: Loaded golden-unit references, tolerances, and warning threshold.
    """

    def __init__(self, config: GoldenUnitConfiguration) -> None:
        """Create the station-health evaluator.

        Args:
            config: Golden-unit configuration used for all comparisons.
        """

        self.config = config

    @staticmethod
    def _index(records: list[MeasurementRecord]) -> dict[str, MeasurementRecord]:
        """Index golden-unit records by canonical measurement name.

        Args:
            records: Canonical records from one golden-unit acquisition.

        Returns:
            Mapping from measurement name to record.

        Raises:
            ValueError: If the acquisition is empty or spans sessions/stations.
        """

        if not records:
            raise ValueError("No golden-unit records supplied.")
        if len({record.session_id for record in records}) != 1:
            raise ValueError("Golden-unit evaluation requires one acquisition session.")
        if len({record.station_id for record in records}) != 1:
            raise ValueError("Golden-unit evaluation requires one test station.")
        return {record.measurement: record for record in records}

    def _metric_status(self, bias: float, tolerance: float) -> str:
        """Classify one tester bias against warning and action limits.

        Args:
            bias: Signed measured-minus-reference bias.
            tolerance: Maximum allowed absolute bias.

        Returns:
            Human-readable metric health state.
        """

        ratio = abs(bias) / tolerance
        if ratio > 1.0:
            return "Needs attention"
        if ratio >= self.config.warning_fraction:
            return "Warning"
        return "Healthy"

    def evaluate(
        self, records: list[MeasurementRecord], *, check_number: int
    ) -> TesterHealthResult:
        """Compare one golden-unit acquisition with its known reference values.

        Args:
            records: Canonical golden-unit measurements from one station.
            check_number: Sequential golden check number for that station.

        Returns:
            Overall tester-health state plus per-metric bias results.

        Raises:
            ValueError: If required records or units are missing.
        """

        index = self._index(records)
        station_id = records[0].station_id
        session_id = records[0].session_id
        references = self.config.references
        metrics: list[GoldenMetricResult] = []

        def add_scalar(name: str, key: str, record_name: str, unit: str, tolerance_key: str) -> None:
            """Append one scalar measured-to-reference comparison.

            Args:
                name: Operator-facing metric name.
                key: Stable result key.
                record_name: Canonical record to read.
                unit: Expected canonical engineering unit.
                tolerance_key: Key selecting the applicable bias tolerance.
            """

            if record_name not in index:
                raise ValueError(f"Golden-unit measurement {record_name!r} is missing.")
            record = index[record_name]
            if record.unit != unit:
                raise ValueError(
                    f"Golden-unit measurement {record_name!r} uses {record.unit!r}; expected {unit!r}."
                )
            measured = float(np.mean(np.asarray(record.values, dtype=float)))
            reference = references[key]
            tolerance = self.config.tolerances[tolerance_key]
            bias = measured - reference
            metrics.append(
                GoldenMetricResult(
                    name=name, key=key, unit=unit, measured=measured, reference=reference,
                    bias=bias, tolerance=tolerance, status=self._metric_status(bias, tolerance),
                )
            )

        add_scalar("Battery voltage", "battery_voltage", "battery_voltage", "V", "battery_voltage")
        add_scalar("Accelerometer X", "accel_x", "accel_x", "g", "accel")
        add_scalar("Accelerometer Y", "accel_y", "accel_y", "g", "accel")
        add_scalar("Accelerometer Z", "accel_z", "accel_z", "g", "accel")
        add_scalar("Gyroscope X", "gyro_x", "gyro_x", "deg/s", "gyro")
        add_scalar("Gyroscope Y", "gyro_y", "gyro_y", "deg/s", "gyro")
        add_scalar("Gyroscope Z", "gyro_z", "gyro_z", "deg/s", "gyro")

        if "ppg_optical" not in index:
            raise ValueError("Golden-unit measurement 'ppg_optical' is missing.")
        ppg_record = index["ppg_optical"]
        if ppg_record.unit != "normalized" or ppg_record.sample_rate_hz is None:
            raise ValueError("Golden-unit PPG must use normalized units and include sample_rate_hz.")
        ppg = np.asarray(ppg_record.values, dtype=float)
        time = np.arange(ppg.size) / ppg_record.sample_rate_hz
        omega = 2.0 * math.pi * self.config.ppg_fixture_frequency_hz
        design = np.column_stack(
            (np.sin(omega * time), np.cos(omega * time), np.ones(ppg.size))
        )
        coefficients, *_ = np.linalg.lstsq(design, ppg, rcond=None)
        measured_amplitude = float(math.hypot(coefficients[0], coefficients[1]))
        ppg_reference = references["ppg_amplitude"]
        ppg_tolerance = self.config.tolerances["ppg_amplitude"]
        ppg_bias = measured_amplitude - ppg_reference
        metrics.append(
            GoldenMetricResult(
                name="Optical response amplitude", key="ppg_amplitude", unit="normalized",
                measured=measured_amplitude, reference=ppg_reference, bias=ppg_bias,
                tolerance=ppg_tolerance, status=self._metric_status(ppg_bias, ppg_tolerance),
            )
        )

        add_scalar(
            "Skin temperature", "skin_temperature", "skin_temperature", "degC",
            "skin_temperature",
        )
        if self.config.include_ecg:
            add_scalar(
                "ECG electrode impedance", "ecg_electrode_impedance",
                "ecg_electrode_impedance", "kohm", "ecg_electrode_impedance",
            )

        worst = max(metrics, key=lambda item: item.bias_ratio)
        if any(metric.status == "Needs attention" for metric in metrics):
            overall_status = "Needs attention"
        elif any(metric.status == "Warning" for metric in metrics):
            overall_status = "Warning"
        else:
            overall_status = "Healthy"

        return TesterHealthResult(
            check_id=session_id, golden_device_id=records[0].device_id, station_id=station_id,
            check_number=int(check_number), status=overall_status, metrics=tuple(metrics),
            worst_metric=worst.name, max_bias_ratio=worst.bias_ratio,
        )


@dataclass(frozen=True)
class StepResult:
    """Result from one manufacturing acceptance check.

    Args:
        name: Human-readable test name shown to an operator.
        passed: Whether the check met the configured acceptance limit.
        measured: Human-readable measured value or metric summary.
        limit: Human-readable configured acceptance limit.
        failure_code: Technical traceability code used when the test fails.
    """

    name: str
    passed: bool
    measured: str
    limit: str
    failure_code: str | None = None


@dataclass(frozen=True)
class DeviceDisposition:
    """Overall pass/fail decision for one device test session.

    Args:
        device_id: Device-under-test identifier.
        session_id: Unique test-session identifier.
        passed: Overall device result.
        results: Individual manufacturing test results.
    """

    device_id: str
    session_id: str
    passed: bool
    results: tuple[StepResult, ...]

    @property
    def failure_codes(self) -> tuple[str, ...]:
        """Return all failure codes generated during this device test.

        Returns:
            Failure codes in the same order as the underlying test results.
        """

        return tuple(result.failure_code for result in self.results if result.failure_code)


class ManufacturingTestEngine:
    """Evaluate a single canonical device session against configured limits.

    Args:
        specs: Optional pre-loaded specification object.
        config_path: Optional INI path used when ``specs`` is not supplied.
    """

    def __init__(
        self,
        specs: TestSpecifications | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        """Create the test engine with explicit or file-based specifications.

        Args:
            specs: Pre-loaded acceptance limits for advanced programmatic use.
            config_path: User-editable INI file containing acceptance limits.
        """

        self.specs = specs or load_test_specifications(config_path)

    @staticmethod
    def _index(records: list[MeasurementRecord]) -> dict[str, MeasurementRecord]:
        """Index one device session by canonical measurement name.

        Args:
            records: Canonical records from one DUT and one test session.

        Returns:
            Dictionary keyed by canonical measurement name.

        Raises:
            ValueError: If records span devices/sessions or contain duplicates.
        """

        if not records:
            raise ValueError("No measurement records supplied.")

        sessions = {record.session_id for record in records}
        devices = {record.device_id for record in records}
        if len(sessions) != 1 or len(devices) != 1:
            raise ValueError("A test evaluation must contain one device and one session.")

        indexed: dict[str, MeasurementRecord] = {}
        for record in records:
            if record.measurement in indexed:
                raise ValueError(
                    f"Duplicate measurement {record.measurement!r} in one session."
                )
            indexed[record.measurement] = record
        return indexed

    @staticmethod
    def _require(
        index: dict[str, MeasurementRecord],
        name: str,
        unit: str,
    ) -> MeasurementRecord:
        """Fetch one required measurement and enforce its canonical unit.

        Args:
            index: Session measurement index.
            name: Required canonical measurement name.
            unit: Canonical unit expected by the test logic.

        Returns:
            Matching canonical measurement record.

        Raises:
            ValueError: If the measurement is missing or has the wrong unit.
        """

        if name not in index:
            raise ValueError(f"Required measurement {name!r} is missing.")
        record = index[name]
        if record.unit != unit:
            raise ValueError(
                f"Measurement {name!r} has unit {record.unit!r}; expected {unit!r}."
            )
        return record

    def evaluate(
        self,
        records: list[MeasurementRecord],
        *,
        require_ecg: bool = False,
    ) -> DeviceDisposition:
        """Run all acceptance checks required by one product profile.

        Args:
            records: Canonical device measurements for one device session.
            require_ecg: Whether the selected product profile requires the ECG
                electrode-path measurement.

        Returns:
            Overall disposition plus individual test-step results.

        Raises:
            ValueError: If required non-ECG measurements, units, or metadata are invalid.
        """

        index = self._index(records)
        results: list[StepResult] = []
        specs = self.specs

        battery = self._require(index, "battery_voltage", "V").values[0]
        battery_ok = (
            specs.battery_voltage_min_v
            <= battery
            <= specs.battery_voltage_max_v
        )
        results.append(
            StepResult(
                "Battery voltage",
                battery_ok,
                f"{battery:.3f} V",
                f"{specs.battery_voltage_min_v:.2f} to "
                f"{specs.battery_voltage_max_v:.2f} V",
                None if battery_ok else "POWER_BATTERY_VOLTAGE_OUT_OF_SPEC",
            )
        )

        accel_targets = {"x": 0.0, "y": 0.0, "z": 1.0}
        for axis, target in accel_targets.items():
            values = np.asarray(
                self._require(index, f"accel_{axis}", "g").values,
                dtype=float,
            )
            mean = float(values.mean())
            noise_rms = float(np.sqrt(np.mean((values - mean) ** 2)))
            bias = abs(mean - target)
            passed = (
                bias <= specs.accel_axis_bias_max_g
                and noise_rms <= specs.accel_noise_rms_max_g
            )
            results.append(
                StepResult(
                    f"Accelerometer {axis.upper()}",
                    passed,
                    f"bias {bias:.4f} g, noise {noise_rms:.4f} g RMS",
                    f"bias ≤ {specs.accel_axis_bias_max_g:.3f} g; "
                    f"noise ≤ {specs.accel_noise_rms_max_g:.3f} g RMS",
                    None if passed else f"IMU_ACCEL_{axis.upper()}_OUT_OF_SPEC",
                )
            )

        for axis in "xyz":
            values = np.asarray(
                self._require(index, f"gyro_{axis}", "deg/s").values,
                dtype=float,
            )
            mean = float(values.mean())
            noise_rms = float(np.sqrt(np.mean((values - mean) ** 2)))
            passed = (
                abs(mean) <= specs.gyro_bias_max_dps
                and noise_rms <= specs.gyro_noise_rms_max_dps
            )
            results.append(
                StepResult(
                    f"Gyroscope {axis.upper()}",
                    passed,
                    f"bias {mean:.3f} deg/s, noise {noise_rms:.3f} deg/s RMS",
                    f"|bias| ≤ {specs.gyro_bias_max_dps:.2f} deg/s; "
                    f"noise ≤ {specs.gyro_noise_rms_max_dps:.2f} deg/s RMS",
                    None if passed else f"IMU_GYRO_{axis.upper()}_OUT_OF_SPEC",
                )
            )

        ppg_record = self._require(index, "ppg_optical", "normalized")
        ppg = np.asarray(ppg_record.values, dtype=float)
        if ppg_record.sample_rate_hz is None:
            raise ValueError("Optical PPG waveform is missing sample_rate_hz.")

        # The fixture uses a known modulation frequency. Fitting that commanded
        # component gives a clean signal estimate while the residual captures noise.
        sample_rate_hz = ppg_record.sample_rate_hz
        time = np.arange(ppg.size) / sample_rate_hz
        angular_frequency = 2.0 * math.pi * specs.ppg_fixture_frequency_hz
        design = np.column_stack(
            (
                np.sin(angular_frequency * time),
                np.cos(angular_frequency * time),
                np.ones_like(time),
            )
        )
        coefficients, *_ = np.linalg.lstsq(design, ppg, rcond=None)
        fitted = design @ coefficients
        signal_component = fitted - coefficients[2]
        residual = ppg - fitted
        signal_rms = float(np.sqrt(np.mean(signal_component**2)))
        noise_rms = float(np.sqrt(np.mean(residual**2)))
        snr_db = 20.0 * math.log10(
            max(signal_rms, 1e-12) / max(noise_rms, 1e-12)
        )
        saturation_fraction = float(np.mean(ppg >= 1.45))
        ppg_ok = (
            snr_db >= specs.ppg_snr_min_db
            and saturation_fraction <= specs.ppg_saturation_max_fraction
        )
        results.append(
            StepResult(
                "Optical PPG response",
                ppg_ok,
                f"SNR {snr_db:.1f} dB, saturation "
                f"{100 * saturation_fraction:.1f}%",
                f"SNR ≥ {specs.ppg_snr_min_db:.1f} dB; saturation ≤ "
                f"{100 * specs.ppg_saturation_max_fraction:.1f}%",
                None if ppg_ok else "OPTICAL_PPG_SIGNAL_QUALITY_FAIL",
            )
        )

        temperature = self._require(index, "skin_temperature", "degC").values[0]
        temperature_error = abs(temperature - specs.skin_temp_reference_c)
        temperature_ok = temperature_error <= specs.skin_temp_error_max_c
        results.append(
            StepResult(
                "Skin temperature",
                temperature_ok,
                f"{temperature:.2f} °C (error {temperature_error:.2f} °C)",
                f"reference {specs.skin_temp_reference_c:.1f} °C ± "
                f"{specs.skin_temp_error_max_c:.2f} °C",
                None if temperature_ok else "TEMP_SENSOR_OFFSET_HIGH",
            )
        )

        if "ecg_electrode_impedance" in index:
            impedance = self._require(
                index,
                "ecg_electrode_impedance",
                "kohm",
            ).values[0]
            ecg_ok = impedance <= specs.ecg_electrode_impedance_max_kohm
            results.append(
                StepResult(
                    "ECG electrode path",
                    ecg_ok,
                    f"{impedance:.1f} kΩ",
                    f"≤ {specs.ecg_electrode_impedance_max_kohm:.0f} kΩ",
                    None if ecg_ok else "ECG_ELECTRODE_IMPEDANCE_HIGH",
                )
            )
        elif require_ecg:
            results.append(
                StepResult(
                    "ECG electrode path",
                    False,
                    "measurement missing",
                    "ECG measurement required by product profile",
                    "ECG_REQUIRED_MEASUREMENT_MISSING",
                )
            )

        passed = all(result.passed for result in results)
        return DeviceDisposition(
            records[0].device_id,
            records[0].session_id,
            passed,
            tuple(results),
        )
