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

    The values are stored in ``config/test_specs.ini`` so an engineer can tune
    a simulated test plan without editing Python.
    """

    battery_voltage_min_v: float
    battery_voltage_max_v: float
    charging_current_min_ma: float
    charging_current_max_ma: float
    idle_current_max_ma: float
    active_current_max_ma: float
    accel_axis_bias_max_g: float
    accel_noise_rms_max_g: float
    gyro_bias_max_dps: float
    gyro_noise_rms_max_dps: float
    ppg_snr_min_db: float
    ppg_fixture_frequency_hz: float
    ppg_saturation_max_fraction: float
    spo2_fixture_frequency_hz: float
    spo2_red_snr_min_db: float
    spo2_ir_snr_min_db: float
    spo2_ratio_reference: float
    spo2_ratio_tolerance: float
    spo2_saturation_max_fraction: float
    skin_temp_reference_c: float
    skin_temp_error_max_c: float
    ble_tx_power_min_dbm: float
    ble_tx_power_max_dbm: float
    ble_packet_error_rate_max_percent: float
    ble_frequency_error_max_khz: float
    haptic_frequency_reference_hz: float
    haptic_frequency_tolerance_hz: float
    haptic_rms_min_g: float
    haptic_rms_max_g: float
    ecg_electrode_impedance_max_kohm: float
    ecg_fixture_rate_hz: float
    ecg_amplitude_min_mv: float
    ecg_amplitude_max_mv: float
    ecg_correlation_min: float
    ecg_noise_rms_max_mv: float

@dataclass(frozen=True)
class ProductProfile:
    """Configurable wearable test profile shown in the GUI.

    Args:
        name: Human-readable profile name.
        include_spo2: Whether red/IR SpO₂ optical verification is required.
        include_power_current: Whether charging and operating-current checks are required.
        include_ble_rf: Whether BLE/RF functional measurements are required.
        include_haptic: Whether haptic-motor vibration verification is required.
        include_ecg: Whether ECG electrode-path impedance is required.
        include_ecg_waveform: Whether a known injected ECG waveform is required.
        batch_suffix: Suffix used to keep all-profile batch DUT IDs unique.
    """

    name: str
    include_spo2: bool
    include_power_current: bool
    include_ble_rf: bool
    include_haptic: bool
    include_ecg: bool
    include_ecg_waveform: bool
    batch_suffix: str

    def evaluation_requirements(self) -> dict[str, bool]:
        """Return keyword arguments consumed by the acceptance engine.

        Returns:
            Mapping of ``require_*`` keyword arguments to enabled profile features.
        """

        return {
            "require_spo2": self.include_spo2,
            "require_power_current": self.include_power_current,
            "require_ble_rf": self.include_ble_rf,
            "require_haptic": self.include_haptic,
            "require_ecg": self.include_ecg,
            "require_ecg_waveform": self.include_ecg_waveform,
        }

    def simulator_options(self) -> dict[str, bool]:
        """Return feature switches consumed by the virtual wearable.

        Returns:
            Mapping of ``include_*`` simulator keyword arguments.
        """

        return {
            "include_spo2": self.include_spo2,
            "include_power_current": self.include_power_current,
            "include_ble_rf": self.include_ble_rf,
            "include_haptic": self.include_haptic,
            "include_ecg": self.include_ecg,
            "include_ecg_waveform": self.include_ecg_waveform,
        }


def load_product_profiles(path: str | Path | None = None) -> tuple[ProductProfile, ...]:
    """Load product profiles from the user-editable products INI file.

    Args:
        path: Optional path to a products INI file.

    Returns:
        Product profiles in file order.

    Raises:
        FileNotFoundError: If the products configuration is missing.
        ValueError: If no valid product profiles are defined.
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
        if not name or name in seen_names:
            raise ValueError(f"Invalid or duplicate product profile name: {name!r}")
        try:
            include_ecg = parser.getboolean(section, "include_ecg", fallback=False)
            profile = ProductProfile(
                name=name,
                include_spo2=parser.getboolean(section, "include_spo2", fallback=True),
                include_power_current=parser.getboolean(
                    section, "include_power_current", fallback=True
                ),
                include_ble_rf=parser.getboolean(section, "include_ble_rf", fallback=True),
                include_haptic=parser.getboolean(section, "include_haptic", fallback=True),
                include_ecg=include_ecg,
                include_ecg_waveform=parser.getboolean(
                    section, "include_ecg_waveform", fallback=include_ecg
                ),
                batch_suffix=parser.get(section, "batch_suffix").strip(),
            )
        except Exception as exc:
            raise ValueError(f"Invalid product profile [{section}] in {config_path}.") from exc
        if not profile.batch_suffix:
            raise ValueError(f"Product profile {name!r} must define a non-empty batch_suffix.")
        normalized_suffix = "".join(
            ch if ch.isalnum() else "_" for ch in profile.batch_suffix.upper()
        ).strip("_")
        if not normalized_suffix or normalized_suffix in seen_suffixes:
            raise ValueError(f"Invalid or duplicate product batch suffix: {normalized_suffix!r}")
        if profile.include_ecg_waveform and not profile.include_ecg:
            raise ValueError(
                f"Product profile {name!r} cannot require an ECG waveform while ECG is disabled."
            )
        seen_names.add(name)
        seen_suffixes.add(normalized_suffix)
        profiles.append(
            ProductProfile(
                name=profile.name,
                include_spo2=profile.include_spo2,
                include_power_current=profile.include_power_current,
                include_ble_rf=profile.include_ble_rf,
                include_haptic=profile.include_haptic,
                include_ecg=profile.include_ecg,
                include_ecg_waveform=profile.include_ecg_waveform,
                batch_suffix=normalized_suffix,
            )
        )

    if not profiles:
        raise ValueError(f"No [product:...] profiles were found in {config_path}.")
    return tuple(profiles)

def load_test_specifications(path: str | Path | None = None) -> TestSpecifications:
    """Load manufacturing test limits from an INI file.

    Args:
        path: Optional specification INI path.

    Returns:
        Validated acceptance limits.

    Raises:
        FileNotFoundError: If the configuration file is missing.
        ValueError: If a required value is missing or inconsistent.
    """

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Test specification file not found: {config_path}")

    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")

    def get_float(section: str, option: str) -> float:
        """Read one required floating-point setting.

        Args:
            section: INI section name.
            option: INI option name.

        Returns:
            Requested value converted to float.
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
        charging_current_min_ma=get_float("power", "charging_current_min_ma"),
        charging_current_max_ma=get_float("power", "charging_current_max_ma"),
        idle_current_max_ma=get_float("power", "idle_current_max_ma"),
        active_current_max_ma=get_float("power", "active_current_max_ma"),
        accel_axis_bias_max_g=get_float("accelerometer", "axis_bias_max_g"),
        accel_noise_rms_max_g=get_float("accelerometer", "noise_rms_max_g"),
        gyro_bias_max_dps=get_float("gyroscope", "bias_max_dps"),
        gyro_noise_rms_max_dps=get_float("gyroscope", "noise_rms_max_dps"),
        ppg_snr_min_db=get_float("optical_ppg", "snr_min_db"),
        ppg_fixture_frequency_hz=get_float("optical_ppg", "fixture_frequency_hz"),
        ppg_saturation_max_fraction=get_float("optical_ppg", "saturation_max_fraction"),
        spo2_fixture_frequency_hz=get_float("spo2_optical", "fixture_frequency_hz"),
        spo2_red_snr_min_db=get_float("spo2_optical", "red_snr_min_db"),
        spo2_ir_snr_min_db=get_float("spo2_optical", "ir_snr_min_db"),
        spo2_ratio_reference=get_float("spo2_optical", "ratio_reference"),
        spo2_ratio_tolerance=get_float("spo2_optical", "ratio_tolerance"),
        spo2_saturation_max_fraction=get_float("spo2_optical", "saturation_max_fraction"),
        skin_temp_reference_c=get_float("temperature", "reference_c"),
        skin_temp_error_max_c=get_float("temperature", "error_max_c"),
        ble_tx_power_min_dbm=get_float("ble_rf", "tx_power_min_dbm"),
        ble_tx_power_max_dbm=get_float("ble_rf", "tx_power_max_dbm"),
        ble_packet_error_rate_max_percent=get_float(
            "ble_rf", "packet_error_rate_max_percent"
        ),
        ble_frequency_error_max_khz=get_float("ble_rf", "frequency_error_max_khz"),
        haptic_frequency_reference_hz=get_float("haptic", "frequency_reference_hz"),
        haptic_frequency_tolerance_hz=get_float("haptic", "frequency_tolerance_hz"),
        haptic_rms_min_g=get_float("haptic", "rms_min_g"),
        haptic_rms_max_g=get_float("haptic", "rms_max_g"),
        ecg_electrode_impedance_max_kohm=get_float("ecg", "electrode_impedance_max_kohm"),
        ecg_fixture_rate_hz=get_float("ecg_waveform", "fixture_rate_hz"),
        ecg_amplitude_min_mv=get_float("ecg_waveform", "amplitude_min_mv"),
        ecg_amplitude_max_mv=get_float("ecg_waveform", "amplitude_max_mv"),
        ecg_correlation_min=get_float("ecg_waveform", "correlation_min"),
        ecg_noise_rms_max_mv=get_float("ecg_waveform", "noise_rms_max_mv"),
    )

    ordered_ranges = (
        ("battery voltage", specs.battery_voltage_min_v, specs.battery_voltage_max_v),
        ("charging current", specs.charging_current_min_ma, specs.charging_current_max_ma),
        ("BLE transmit power", specs.ble_tx_power_min_dbm, specs.ble_tx_power_max_dbm),
        ("haptic RMS", specs.haptic_rms_min_g, specs.haptic_rms_max_g),
        ("ECG waveform amplitude", specs.ecg_amplitude_min_mv, specs.ecg_amplitude_max_mv),
    )
    for name, minimum, maximum in ordered_ranges:
        if minimum >= maximum:
            raise ValueError(f"Configured {name} minimum must be below its maximum.")

    fractions = {
        "PPG saturation": specs.ppg_saturation_max_fraction,
        "SpO2 saturation": specs.spo2_saturation_max_fraction,
        "ECG correlation": specs.ecg_correlation_min,
    }
    if not 0.0 <= fractions["PPG saturation"] <= 1.0:
        raise ValueError("PPG saturation fraction must be between 0 and 1.")
    if not 0.0 <= fractions["SpO2 saturation"] <= 1.0:
        raise ValueError("SpO2 saturation fraction must be between 0 and 1.")
    if not 0.0 <= fractions["ECG correlation"] <= 1.0:
        raise ValueError("ECG correlation minimum must be between 0 and 1.")

    positive_values = {
        "accelerometer axis bias": specs.accel_axis_bias_max_g,
        "accelerometer noise": specs.accel_noise_rms_max_g,
        "gyroscope bias": specs.gyro_bias_max_dps,
        "gyroscope noise": specs.gyro_noise_rms_max_dps,
        "PPG fixture frequency": specs.ppg_fixture_frequency_hz,
        "SpO2 fixture frequency": specs.spo2_fixture_frequency_hz,
        "SpO2 ratio tolerance": specs.spo2_ratio_tolerance,
        "temperature error": specs.skin_temp_error_max_c,
        "BLE packet-error limit": specs.ble_packet_error_rate_max_percent,
        "BLE frequency-error limit": specs.ble_frequency_error_max_khz,
        "haptic frequency": specs.haptic_frequency_reference_hz,
        "haptic frequency tolerance": specs.haptic_frequency_tolerance_hz,
        "ECG impedance": specs.ecg_electrode_impedance_max_kohm,
        "ECG fixture rate": specs.ecg_fixture_rate_hz,
        "ECG noise limit": specs.ecg_noise_rms_max_mv,
    }
    for name, value in positive_values.items():
        if value <= 0.0:
            raise ValueError(f"Configured {name} limit must be greater than zero.")
    if specs.idle_current_max_ma <= 0.0 or specs.active_current_max_ma <= 0.0:
        raise ValueError("Operating-current limits must be greater than zero.")

    return specs


def fit_modulated_signal(
    values: np.ndarray,
    sample_rate_hz: float,
    frequency_hz: float,
    *,
    saturation_threshold: float = 1.45,
) -> tuple[float, float, float]:
    """Estimate amplitude, SNR, and saturation for a known optical modulation.

    Args:
        values: Optical waveform samples.
        sample_rate_hz: Waveform sample rate in hertz.
        frequency_hz: Commanded fixture modulation frequency in hertz.
        saturation_threshold: Normalized value treated as optical saturation.

    Returns:
        Tuple containing fitted amplitude, SNR in decibels, and saturation fraction.
    """

    data = np.asarray(values, dtype=float)
    if data.size < 3 or sample_rate_hz <= 0.0:
        raise ValueError("Optical waveform must contain at least three samples and a sample rate.")
    time = np.arange(data.size) / sample_rate_hz
    omega = 2.0 * math.pi * frequency_hz
    design = np.column_stack(
        (np.sin(omega * time), np.cos(omega * time), np.ones(data.size))
    )
    coefficients, *_ = np.linalg.lstsq(design, data, rcond=None)
    fitted = design @ coefficients
    signal_component = fitted - coefficients[2]
    residual = data - fitted
    signal_rms = float(np.sqrt(np.mean(signal_component**2)))
    noise_rms = float(np.sqrt(np.mean(residual**2)))
    snr_db = 20.0 * math.log10(max(signal_rms, 1e-12) / max(noise_rms, 1e-12))
    amplitude = float(math.hypot(coefficients[0], coefficients[1]))
    saturation_fraction = float(np.mean(data >= saturation_threshold))
    return amplitude, snr_db, saturation_fraction


def build_ecg_reference_waveform(
    sample_count: int,
    sample_rate_hz: float,
    fixture_rate_hz: float,
) -> np.ndarray:
    """Build a normalized periodic ECG-like electrical fixture waveform.

    Args:
        sample_count: Number of requested waveform samples.
        sample_rate_hz: Waveform sample rate in hertz.
        fixture_rate_hz: Repetition rate of the injected reference beat.

    Returns:
        Zero-centered waveform normalized to 1 mV peak-to-peak when multiplied by 1.
    """

    if sample_count < 3 or sample_rate_hz <= 0.0 or fixture_rate_hz <= 0.0:
        raise ValueError("ECG reference waveform requires positive rates and at least three samples.")
    time = np.arange(sample_count) / sample_rate_hz
    phase = np.mod(time * fixture_rate_hz, 1.0)

    def pulse(center: float, width: float, amplitude: float) -> np.ndarray:
        """Create one periodic Gaussian feature in the synthetic ECG beat.

        Args:
            center: Feature center within one normalized beat period.
            width: Gaussian width in normalized beat units.
            amplitude: Signed feature amplitude.

        Returns:
            Periodic Gaussian contribution for every phase sample.
        """

        distance = np.abs(phase - center)
        distance = np.minimum(distance, 1.0 - distance)
        return amplitude * np.exp(-0.5 * (distance / width) ** 2)

    waveform = (
        pulse(0.18, 0.030, 0.12)
        + pulse(0.36, 0.012, -0.15)
        + pulse(0.40, 0.010, 1.00)
        + pulse(0.435, 0.014, -0.25)
        + pulse(0.68, 0.060, 0.30)
    )
    waveform -= float(np.mean(waveform))
    span = float(np.ptp(waveform))
    if span <= 0.0:
        raise ValueError("ECG reference waveform has zero amplitude.")
    return waveform / span


def ecg_waveform_metrics(
    values: np.ndarray,
    sample_rate_hz: float,
    fixture_rate_hz: float,
) -> tuple[float, float, float]:
    """Measure amplitude, template correlation, and residual noise of an ECG signal.

    Args:
        values: ECG waveform in millivolts.
        sample_rate_hz: Waveform sample rate in hertz.
        fixture_rate_hz: Known repetition rate of the injected fixture waveform.

    Returns:
        Peak-to-peak amplitude in millivolts, correlation coefficient, and residual RMS noise.
    """

    data = np.asarray(values, dtype=float)
    reference = build_ecg_reference_waveform(data.size, sample_rate_hz, fixture_rate_hz)
    design = np.column_stack((reference, np.ones(data.size)))
    coefficients, *_ = np.linalg.lstsq(design, data, rcond=None)
    fitted = design @ coefficients
    residual = data - fitted
    amplitude_mv = abs(float(coefficients[0]))
    correlation = float(np.corrcoef(data, reference)[0, 1])
    noise_rms_mv = float(np.sqrt(np.mean(residual**2)))
    return amplitude_mv, correlation, noise_rms_mv

@dataclass(frozen=True)
class GoldenUnitConfiguration:
    """Known-good reference values and simulated station-drift settings.

    Args:
        device_id: Identifier of the known-good reference wearable.
        include_spo2: Whether the golden check verifies red/IR optical paths.
        include_power_current: Whether charging and operating currents are checked.
        include_ble_rf: Whether BLE/RF metrics are checked.
        include_haptic: Whether the haptic vibration path is checked.
        include_ecg: Whether ECG electrode impedance is checked.
        include_ecg_waveform: Whether an injected ECG waveform is checked.
        ppg_fixture_frequency_hz: PPG optical fixture modulation frequency.
        spo2_fixture_frequency_hz: SpO₂ red/IR fixture modulation frequency.
        haptic_frequency_hz: Reference haptic vibration frequency.
        ecg_fixture_rate_hz: Repetition rate of the injected ECG reference signal.
        references: Expected scalar or derived metrics for the golden device.
        tolerances: Maximum allowed absolute tester bias by metric.
        warning_fraction: Fraction of tolerance that raises a warning.
        station_drift: Per-station initial offsets and per-check drift rates.
    """

    device_id: str
    include_spo2: bool
    include_power_current: bool
    include_ble_rf: bool
    include_haptic: bool
    include_ecg: bool
    include_ecg_waveform: bool
    ppg_fixture_frequency_hz: float
    spo2_fixture_frequency_hz: float
    haptic_frequency_hz: float
    ecg_fixture_rate_hz: float
    references: dict[str, float]
    tolerances: dict[str, float]
    warning_fraction: float
    station_drift: dict[str, dict[str, float]]


def load_golden_unit_configuration(
    path: str | Path | None = None,
) -> GoldenUnitConfiguration:
    """Load golden references, tester tolerances, and station drift from INI.

    Args:
        path: Optional golden-unit INI path.

    Returns:
        Validated configuration used by golden simulation and evaluation.

    Raises:
        FileNotFoundError: If the configuration file is missing.
        ValueError: If a required setting is invalid.
    """

    config_path = Path(path) if path is not None else DEFAULT_GOLDEN_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Golden-unit configuration file not found: {config_path}")
    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")

    def get_float(section: str, option: str) -> float:
        """Read one required golden-unit numeric setting.

        Args:
            section: INI section name.
            option: INI option name.

        Returns:
            Requested value converted to float.
        """

        try:
            return parser.getfloat(section, option)
        except Exception as exc:
            raise ValueError(
                f"Invalid or missing setting [{section}] {option} in {config_path}."
            ) from exc

    references = {
        "battery_voltage": get_float("reference", "battery_voltage_v"),
        "charging_current": get_float("reference", "charging_current_ma"),
        "idle_current": get_float("reference", "idle_current_ma"),
        "active_current": get_float("reference", "active_current_ma"),
        "accel_x": get_float("reference", "accel_x_g"),
        "accel_y": get_float("reference", "accel_y_g"),
        "accel_z": get_float("reference", "accel_z_g"),
        "gyro_x": get_float("reference", "gyro_x_dps"),
        "gyro_y": get_float("reference", "gyro_y_dps"),
        "gyro_z": get_float("reference", "gyro_z_dps"),
        "ppg_amplitude": get_float("reference", "ppg_amplitude"),
        "spo2_red_amplitude": get_float("reference", "spo2_red_amplitude"),
        "spo2_ir_amplitude": get_float("reference", "spo2_ir_amplitude"),
        "skin_temperature": get_float("reference", "skin_temperature_c"),
        "ble_tx_power": get_float("reference", "ble_tx_power_dbm"),
        "ble_packet_error_rate": get_float(
            "reference", "ble_packet_error_rate_percent"
        ),
        "ble_frequency_error": get_float("reference", "ble_frequency_error_khz"),
        "haptic_rms": get_float("reference", "haptic_rms_g"),
        "haptic_frequency": get_float("reference", "haptic_frequency_hz"),
        "ecg_electrode_impedance": get_float("reference", "ecg_impedance_kohm"),
        "ecg_waveform_amplitude": get_float(
            "reference", "ecg_waveform_amplitude_mv"
        ),
    }
    tolerances = {
        "battery_voltage": get_float("bias_limits", "battery_voltage_v"),
        "charging_current": get_float("bias_limits", "charging_current_ma"),
        "idle_current": get_float("bias_limits", "idle_current_ma"),
        "active_current": get_float("bias_limits", "active_current_ma"),
        "accel": get_float("bias_limits", "accel_axis_g"),
        "gyro": get_float("bias_limits", "gyro_axis_dps"),
        "ppg_amplitude": get_float("bias_limits", "ppg_amplitude"),
        "spo2_red_amplitude": get_float("bias_limits", "spo2_red_amplitude"),
        "spo2_ir_amplitude": get_float("bias_limits", "spo2_ir_amplitude"),
        "skin_temperature": get_float("bias_limits", "skin_temperature_c"),
        "ble_tx_power": get_float("bias_limits", "ble_tx_power_dbm"),
        "ble_packet_error_rate": get_float(
            "bias_limits", "ble_packet_error_rate_percent"
        ),
        "ble_frequency_error": get_float("bias_limits", "ble_frequency_error_khz"),
        "haptic_rms": get_float("bias_limits", "haptic_rms_g"),
        "haptic_frequency": get_float("bias_limits", "haptic_frequency_hz"),
        "ecg_electrode_impedance": get_float("bias_limits", "ecg_impedance_kohm"),
        "ecg_waveform_amplitude": get_float(
            "bias_limits", "ecg_waveform_amplitude_mv"
        ),
    }
    warning_fraction = get_float("health", "warning_fraction")
    if not 0.0 < warning_fraction < 1.0:
        raise ValueError("Golden-unit warning_fraction must be between 0 and 1.")
    if any(value <= 0.0 for value in tolerances.values()):
        raise ValueError("Every golden-unit tester tolerance must be greater than zero.")

    drift_keys = (
        "battery_offset_v", "battery_drift_per_check_v",
        "charging_current_offset_ma", "charging_current_drift_per_check_ma",
        "idle_current_offset_ma", "idle_current_drift_per_check_ma",
        "active_current_offset_ma", "active_current_drift_per_check_ma",
        "accel_x_offset_g", "accel_x_drift_per_check_g",
        "accel_y_offset_g", "accel_y_drift_per_check_g",
        "accel_z_offset_g", "accel_z_drift_per_check_g",
        "gyro_x_offset_dps", "gyro_x_drift_per_check_dps",
        "gyro_y_offset_dps", "gyro_y_drift_per_check_dps",
        "gyro_z_offset_dps", "gyro_z_drift_per_check_dps",
        "ppg_amplitude_offset", "ppg_amplitude_drift_per_check",
        "spo2_red_amplitude_offset", "spo2_red_amplitude_drift_per_check",
        "spo2_ir_amplitude_offset", "spo2_ir_amplitude_drift_per_check",
        "skin_temperature_offset_c", "skin_temperature_drift_per_check_c",
        "ble_tx_power_offset_dbm", "ble_tx_power_drift_per_check_dbm",
        "ble_per_offset_percent", "ble_per_drift_per_check_percent",
        "ble_frequency_error_offset_khz", "ble_frequency_error_drift_per_check_khz",
        "haptic_rms_offset_g", "haptic_rms_drift_per_check_g",
        "haptic_frequency_offset_hz", "haptic_frequency_drift_per_check_hz",
        "ecg_impedance_offset_kohm", "ecg_impedance_drift_per_check_kohm",
        "ecg_waveform_amplitude_offset_mv", "ecg_waveform_amplitude_drift_per_check_mv",
    )
    station_drift: dict[str, dict[str, float]] = {}
    for section in parser.sections():
        if section.lower().startswith("station:"):
            station_id = section.split(":", 1)[1].strip()
            if not station_id:
                raise ValueError(f"Invalid station section name in {config_path}: {section}")
            station_drift[station_id] = {key: get_float(section, key) for key in drift_keys}
    if not station_drift:
        raise ValueError("Golden-unit configuration must define at least one station.")

    try:
        device_id = parser.get("golden_unit", "device_id").strip()
        flags = {
            "include_spo2": parser.getboolean("golden_unit", "include_spo2"),
            "include_power_current": parser.getboolean(
                "golden_unit", "include_power_current"
            ),
            "include_ble_rf": parser.getboolean("golden_unit", "include_ble_rf"),
            "include_haptic": parser.getboolean("golden_unit", "include_haptic"),
            "include_ecg": parser.getboolean("golden_unit", "include_ecg"),
            "include_ecg_waveform": parser.getboolean(
                "golden_unit", "include_ecg_waveform"
            ),
        }
    except Exception as exc:
        raise ValueError(f"Invalid [golden_unit] settings in {config_path}.") from exc
    if not device_id:
        raise ValueError("Golden-unit device_id cannot be blank.")

    return GoldenUnitConfiguration(
        device_id=device_id,
        **flags,
        ppg_fixture_frequency_hz=get_float("golden_unit", "ppg_fixture_frequency_hz"),
        spo2_fixture_frequency_hz=get_float("golden_unit", "spo2_fixture_frequency_hz"),
        haptic_frequency_hz=get_float("golden_unit", "haptic_frequency_hz"),
        ecg_fixture_rate_hz=get_float("golden_unit", "ecg_fixture_rate_hz"),
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
            records: Canonical records from one golden acquisition.

        Returns:
            Mapping from canonical measurement name to record.
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
            Healthy, Warning, or Needs attention.
        """

        ratio = abs(bias) / tolerance
        if ratio > 1.0:
            return "Needs attention"
        if ratio >= self.config.warning_fraction:
            return "Warning"
        return "Healthy"

    def evaluate(
        self,
        records: list[MeasurementRecord],
        *,
        check_number: int,
    ) -> TesterHealthResult:
        """Compare one golden acquisition with its known reference values.

        Args:
            records: Canonical records from one golden-unit run.
            check_number: One-based sequential check number for the station.

        Returns:
            Overall tester-health result and metric-level bias results.
        """

        index = self._index(records)
        references = self.config.references
        metrics: list[GoldenMetricResult] = []
        station_id = records[0].station_id
        session_id = records[0].session_id

        def add_metric(
            name: str,
            key: str,
            unit: str,
            measured: float,
            tolerance_key: str,
        ) -> None:
            """Append one derived golden-unit metric.

            Args:
                name: Human-readable metric name.
                key: Reference dictionary key.
                unit: Display unit.
                measured: Value reported by the tester.
                tolerance_key: Tolerance dictionary key.
            """

            reference = references[key]
            tolerance = self.config.tolerances[tolerance_key]
            bias = measured - reference
            metrics.append(
                GoldenMetricResult(
                    name=name,
                    key=key,
                    unit=unit,
                    measured=float(measured),
                    reference=reference,
                    bias=bias,
                    tolerance=tolerance,
                    status=self._metric_status(bias, tolerance),
                )
            )

        def add_scalar(
            name: str,
            key: str,
            record_name: str,
            unit: str,
            tolerance_key: str,
        ) -> None:
            """Read and append one scalar or waveform-mean metric.

            Args:
                name: Human-readable metric name.
                key: Reference dictionary key.
                record_name: Canonical measurement name.
                unit: Expected canonical unit.
                tolerance_key: Tolerance dictionary key.
            """

            if record_name not in index:
                raise ValueError(f"Golden-unit measurement {record_name!r} is missing.")
            record = index[record_name]
            if record.unit != unit:
                raise ValueError(
                    f"Golden-unit measurement {record_name!r} uses {record.unit!r}; expected {unit!r}."
                )
            add_metric(
                name,
                key,
                unit,
                float(np.mean(np.asarray(record.values, dtype=float))),
                tolerance_key,
            )

        add_scalar("Battery voltage", "battery_voltage", "battery_voltage", "V", "battery_voltage")
        if self.config.include_power_current:
            add_scalar("Charging current", "charging_current", "charging_current", "mA", "charging_current")
            add_scalar("Idle current", "idle_current", "idle_current", "mA", "idle_current")
            add_scalar("Active current", "active_current", "active_current", "mA", "active_current")

        for axis in "xyz":
            add_scalar(f"Accelerometer {axis.upper()}", f"accel_{axis}", f"accel_{axis}", "g", "accel")
        for axis in "xyz":
            add_scalar(f"Gyroscope {axis.upper()}", f"gyro_{axis}", f"gyro_{axis}", "deg/s", "gyro")

        ppg_record = index.get("ppg_optical")
        if ppg_record is None or ppg_record.unit != "normalized" or ppg_record.sample_rate_hz is None:
            raise ValueError("Golden-unit PPG must use normalized units and include a sample rate.")
        ppg_amplitude, _, _ = fit_modulated_signal(
            np.asarray(ppg_record.values, dtype=float),
            ppg_record.sample_rate_hz,
            self.config.ppg_fixture_frequency_hz,
        )
        add_metric("Optical response amplitude", "ppg_amplitude", "normalized", ppg_amplitude, "ppg_amplitude")

        if self.config.include_spo2:
            for color, key in (("Red", "spo2_red_amplitude"), ("IR", "spo2_ir_amplitude")):
                record_name = f"spo2_{color.lower()}_optical"
                record = index.get(record_name)
                if record is None or record.unit != "normalized" or record.sample_rate_hz is None:
                    raise ValueError(f"Golden-unit {color} SpO2 optical waveform is missing or invalid.")
                amplitude, _, _ = fit_modulated_signal(
                    np.asarray(record.values, dtype=float),
                    record.sample_rate_hz,
                    self.config.spo2_fixture_frequency_hz,
                )
                add_metric(
                    f"SpO₂ {color} amplitude",
                    key,
                    "normalized",
                    amplitude,
                    key,
                )

        add_scalar("Skin temperature", "skin_temperature", "skin_temperature", "degC", "skin_temperature")

        if self.config.include_ble_rf:
            add_scalar("BLE transmit power", "ble_tx_power", "ble_tx_power", "dBm", "ble_tx_power")
            add_scalar(
                "BLE packet error rate",
                "ble_packet_error_rate",
                "ble_packet_error_rate",
                "%",
                "ble_packet_error_rate",
            )
            add_scalar(
                "BLE frequency error",
                "ble_frequency_error",
                "ble_frequency_error",
                "kHz",
                "ble_frequency_error",
            )

        if self.config.include_haptic:
            record = index.get("haptic_vibration")
            if record is None or record.unit != "g" or record.sample_rate_hz is None:
                raise ValueError("Golden-unit haptic waveform is missing or invalid.")
            vibration = np.asarray(record.values, dtype=float)
            centered = vibration - float(np.mean(vibration))
            rms_g = float(np.sqrt(np.mean(centered**2)))
            frequencies = np.fft.rfftfreq(centered.size, d=1.0 / record.sample_rate_hz)
            spectrum = np.abs(np.fft.rfft(centered))
            spectrum[0] = 0.0
            dominant_hz = float(frequencies[int(np.argmax(spectrum))])
            add_metric("Haptic RMS vibration", "haptic_rms", "g RMS", rms_g, "haptic_rms")
            add_metric(
                "Haptic dominant frequency",
                "haptic_frequency",
                "Hz",
                dominant_hz,
                "haptic_frequency",
            )

        if self.config.include_ecg:
            add_scalar(
                "ECG electrode impedance",
                "ecg_electrode_impedance",
                "ecg_electrode_impedance",
                "kohm",
                "ecg_electrode_impedance",
            )
        if self.config.include_ecg_waveform:
            record = index.get("ecg_waveform")
            if record is None or record.unit != "mV" or record.sample_rate_hz is None:
                raise ValueError("Golden-unit ECG waveform is missing or invalid.")
            amplitude_mv, _, _ = ecg_waveform_metrics(
                np.asarray(record.values, dtype=float),
                record.sample_rate_hz,
                self.config.ecg_fixture_rate_hz,
            )
            add_metric(
                "ECG waveform amplitude",
                "ecg_waveform_amplitude",
                "mV",
                amplitude_mv,
                "ecg_waveform_amplitude",
            )

        worst = max(metrics, key=lambda item: item.bias_ratio)
        if any(metric.status == "Needs attention" for metric in metrics):
            overall_status = "Needs attention"
        elif any(metric.status == "Warning" for metric in metrics):
            overall_status = "Warning"
        else:
            overall_status = "Healthy"

        return TesterHealthResult(
            check_id=session_id,
            golden_device_id=records[0].device_id,
            station_id=station_id,
            check_number=int(check_number),
            status=overall_status,
            metrics=tuple(metrics),
            worst_metric=worst.name,
            max_bias_ratio=worst.bias_ratio,
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
        require_spo2: bool = False,
        require_power_current: bool = False,
        require_ble_rf: bool = False,
        require_haptic: bool = False,
        require_ecg: bool = False,
        require_ecg_waveform: bool = False,
    ) -> DeviceDisposition:
        """Run all acceptance checks enabled by one product profile.

        Args:
            records: Canonical device measurements for one device session.
            require_spo2: Require red and infrared SpO₂ optical verification.
            require_power_current: Require charging, idle, and active-current checks.
            require_ble_rf: Require BLE/RF transmit-power, PER, and frequency checks.
            require_haptic: Require haptic-motor vibration verification.
            require_ecg: Require ECG electrode-path impedance.
            require_ecg_waveform: Require an injected ECG waveform response.

        Returns:
            Overall disposition plus every individual acceptance result.

        Raises:
            ValueError: If mandatory core measurements or supplied units are invalid.
        """

        index = self._index(records)
        results: list[StepResult] = []
        specs = self.specs

        battery = self._require(index, "battery_voltage", "V").values[0]
        battery_ok = specs.battery_voltage_min_v <= battery <= specs.battery_voltage_max_v
        results.append(
            StepResult(
                "Battery voltage",
                battery_ok,
                f"{battery:.3f} V",
                f"{specs.battery_voltage_min_v:.2f} to {specs.battery_voltage_max_v:.2f} V",
                None if battery_ok else "POWER_BATTERY_VOLTAGE_OUT_OF_SPEC",
            )
        )

        power_current_names = ("charging_current", "idle_current", "active_current")
        if require_power_current or any(name in index for name in power_current_names):
            missing = [name for name in power_current_names if name not in index]
            if missing:
                results.append(
                    StepResult(
                        "Charging and power current",
                        False,
                        "missing: " + ", ".join(missing),
                        "charging, idle, and active current measurements required",
                        "POWER_CURRENT_REQUIRED_MEASUREMENT_MISSING",
                    )
                )
            else:
                charging = self._require(index, "charging_current", "mA").values[0]
                idle = self._require(index, "idle_current", "mA").values[0]
                active = self._require(index, "active_current", "mA").values[0]
                charging_ok = specs.charging_current_min_ma <= charging <= specs.charging_current_max_ma
                idle_ok = idle <= specs.idle_current_max_ma
                active_ok = active <= specs.active_current_max_ma
                results.extend(
                    (
                        StepResult(
                            "Charging current",
                            charging_ok,
                            f"{charging:.1f} mA",
                            f"{specs.charging_current_min_ma:.0f} to {specs.charging_current_max_ma:.0f} mA",
                            None if charging_ok else "POWER_CHARGING_CURRENT_OUT_OF_SPEC",
                        ),
                        StepResult(
                            "Idle current",
                            idle_ok,
                            f"{idle:.2f} mA",
                            f"≤ {specs.idle_current_max_ma:.2f} mA",
                            None if idle_ok else "POWER_IDLE_CURRENT_HIGH",
                        ),
                        StepResult(
                            "Active current",
                            active_ok,
                            f"{active:.1f} mA",
                            f"≤ {specs.active_current_max_ma:.1f} mA",
                            None if active_ok else "POWER_ACTIVE_CURRENT_HIGH",
                        ),
                    )
                )

        accel_targets = {"x": 0.0, "y": 0.0, "z": 1.0}
        for axis, target in accel_targets.items():
            values = np.asarray(self._require(index, f"accel_{axis}", "g").values, dtype=float)
            mean = float(values.mean())
            noise_rms = float(np.sqrt(np.mean((values - mean) ** 2)))
            bias = abs(mean - target)
            passed = bias <= specs.accel_axis_bias_max_g and noise_rms <= specs.accel_noise_rms_max_g
            results.append(
                StepResult(
                    f"Accelerometer {axis.upper()}",
                    passed,
                    f"bias {bias:.4f} g, noise {noise_rms:.4f} g RMS",
                    f"bias ≤ {specs.accel_axis_bias_max_g:.3f} g; noise ≤ {specs.accel_noise_rms_max_g:.3f} g RMS",
                    None if passed else f"IMU_ACCEL_{axis.upper()}_OUT_OF_SPEC",
                )
            )

        for axis in "xyz":
            values = np.asarray(self._require(index, f"gyro_{axis}", "deg/s").values, dtype=float)
            mean = float(values.mean())
            noise_rms = float(np.sqrt(np.mean((values - mean) ** 2)))
            passed = abs(mean) <= specs.gyro_bias_max_dps and noise_rms <= specs.gyro_noise_rms_max_dps
            results.append(
                StepResult(
                    f"Gyroscope {axis.upper()}",
                    passed,
                    f"bias {mean:.3f} °/s, noise {noise_rms:.3f} °/s RMS",
                    f"|bias| ≤ {specs.gyro_bias_max_dps:.2f} °/s; noise ≤ {specs.gyro_noise_rms_max_dps:.2f} °/s RMS",
                    None if passed else f"IMU_GYRO_{axis.upper()}_OUT_OF_SPEC",
                )
            )

        ppg_record = self._require(index, "ppg_optical", "normalized")
        if ppg_record.sample_rate_hz is None:
            raise ValueError("Optical PPG waveform is missing sample_rate_hz.")
        _, ppg_snr_db, ppg_saturation = fit_modulated_signal(
            np.asarray(ppg_record.values, dtype=float),
            ppg_record.sample_rate_hz,
            specs.ppg_fixture_frequency_hz,
        )
        ppg_ok = (
            ppg_snr_db >= specs.ppg_snr_min_db
            and ppg_saturation <= specs.ppg_saturation_max_fraction
        )
        results.append(
            StepResult(
                "Optical PPG response",
                ppg_ok,
                f"SNR {ppg_snr_db:.1f} dB, saturation {100 * ppg_saturation:.1f}%",
                f"SNR ≥ {specs.ppg_snr_min_db:.1f} dB; saturation ≤ {100 * specs.ppg_saturation_max_fraction:.1f}%",
                None if ppg_ok else "OPTICAL_PPG_SIGNAL_QUALITY_FAIL",
            )
        )

        spo2_names = ("spo2_red_optical", "spo2_ir_optical")
        if require_spo2 or any(name in index for name in spo2_names):
            missing = [name for name in spo2_names if name not in index]
            if missing:
                results.append(
                    StepResult(
                        "SpO₂ optical verification",
                        False,
                        "missing: " + ", ".join(missing),
                        "red and infrared optical waveforms required",
                        "SPO2_REQUIRED_MEASUREMENT_MISSING",
                    )
                )
            else:
                red_record = self._require(index, "spo2_red_optical", "normalized")
                ir_record = self._require(index, "spo2_ir_optical", "normalized")
                if red_record.sample_rate_hz is None or ir_record.sample_rate_hz is None:
                    raise ValueError("SpO₂ waveforms must include sample_rate_hz.")
                red_amp, red_snr, red_sat = fit_modulated_signal(
                    np.asarray(red_record.values, dtype=float),
                    red_record.sample_rate_hz,
                    specs.spo2_fixture_frequency_hz,
                )
                ir_amp, ir_snr, ir_sat = fit_modulated_signal(
                    np.asarray(ir_record.values, dtype=float),
                    ir_record.sample_rate_hz,
                    specs.spo2_fixture_frequency_hz,
                )
                ratio = red_amp / max(ir_amp, 1e-12)
                ratio_error = abs(ratio - specs.spo2_ratio_reference)
                spo2_ok = (
                    red_snr >= specs.spo2_red_snr_min_db
                    and ir_snr >= specs.spo2_ir_snr_min_db
                    and ratio_error <= specs.spo2_ratio_tolerance
                    and red_sat <= specs.spo2_saturation_max_fraction
                    and ir_sat <= specs.spo2_saturation_max_fraction
                )
                results.append(
                    StepResult(
                        "SpO₂ red/IR optical paths",
                        spo2_ok,
                        f"red SNR {red_snr:.1f} dB, IR SNR {ir_snr:.1f} dB, ratio {ratio:.3f}",
                        f"SNRs ≥ {min(specs.spo2_red_snr_min_db, specs.spo2_ir_snr_min_db):.1f} dB; ratio {specs.spo2_ratio_reference:.2f} ± {specs.spo2_ratio_tolerance:.2f}",
                        None if spo2_ok else "SPO2_OPTICAL_PATH_FAIL",
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
                f"reference {specs.skin_temp_reference_c:.1f} °C ± {specs.skin_temp_error_max_c:.2f} °C",
                None if temperature_ok else "TEMP_SENSOR_OFFSET_HIGH",
            )
        )

        ble_names = ("ble_tx_power", "ble_packet_error_rate", "ble_frequency_error")
        if require_ble_rf or any(name in index for name in ble_names):
            missing = [name for name in ble_names if name not in index]
            if missing:
                results.append(
                    StepResult(
                        "BLE/RF functional test",
                        False,
                        "missing: " + ", ".join(missing),
                        "TX power, packet error rate, and frequency error required",
                        "BLE_RF_REQUIRED_MEASUREMENT_MISSING",
                    )
                )
            else:
                tx_power = self._require(index, "ble_tx_power", "dBm").values[0]
                per = self._require(index, "ble_packet_error_rate", "%").values[0]
                frequency_error = self._require(index, "ble_frequency_error", "kHz").values[0]
                ble_ok = (
                    specs.ble_tx_power_min_dbm <= tx_power <= specs.ble_tx_power_max_dbm
                    and per <= specs.ble_packet_error_rate_max_percent
                    and abs(frequency_error) <= specs.ble_frequency_error_max_khz
                )
                results.append(
                    StepResult(
                        "BLE/RF functional",
                        ble_ok,
                        f"TX {tx_power:.1f} dBm, PER {per:.2f}%, frequency error {frequency_error:+.1f} kHz",
                        f"TX {specs.ble_tx_power_min_dbm:.1f} to {specs.ble_tx_power_max_dbm:.1f} dBm; PER ≤ {specs.ble_packet_error_rate_max_percent:.1f}%; |frequency error| ≤ {specs.ble_frequency_error_max_khz:.0f} kHz",
                        None if ble_ok else "BLE_RF_FUNCTIONAL_FAIL",
                    )
                )

        if require_haptic or "haptic_vibration" in index:
            if "haptic_vibration" not in index:
                results.append(
                    StepResult(
                        "Haptic motor",
                        False,
                        "measurement missing",
                        "vibration waveform required",
                        "HAPTIC_REQUIRED_MEASUREMENT_MISSING",
                    )
                )
            else:
                haptic_record = self._require(index, "haptic_vibration", "g")
                if haptic_record.sample_rate_hz is None:
                    raise ValueError("Haptic waveform is missing sample_rate_hz.")
                vibration = np.asarray(haptic_record.values, dtype=float)
                centered = vibration - float(np.mean(vibration))
                rms_g = float(np.sqrt(np.mean(centered**2)))
                frequencies = np.fft.rfftfreq(centered.size, d=1.0 / haptic_record.sample_rate_hz)
                spectrum = np.abs(np.fft.rfft(centered))
                spectrum[0] = 0.0
                dominant_hz = float(frequencies[int(np.argmax(spectrum))])
                frequency_error = abs(dominant_hz - specs.haptic_frequency_reference_hz)
                haptic_ok = (
                    specs.haptic_rms_min_g <= rms_g <= specs.haptic_rms_max_g
                    and frequency_error <= specs.haptic_frequency_tolerance_hz
                )
                results.append(
                    StepResult(
                        "Haptic motor vibration",
                        haptic_ok,
                        f"{rms_g:.3f} g RMS at {dominant_hz:.1f} Hz",
                        f"{specs.haptic_rms_min_g:.2f} to {specs.haptic_rms_max_g:.2f} g RMS; {specs.haptic_frequency_reference_hz:.0f} ± {specs.haptic_frequency_tolerance_hz:.0f} Hz",
                        None if haptic_ok else "HAPTIC_VIBRATION_OUT_OF_SPEC",
                    )
                )

        if "ecg_electrode_impedance" in index:
            impedance = self._require(index, "ecg_electrode_impedance", "kohm").values[0]
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
                    "ECG impedance required by product profile",
                    "ECG_REQUIRED_MEASUREMENT_MISSING",
                )
            )

        if "ecg_waveform" in index:
            record = self._require(index, "ecg_waveform", "mV")
            if record.sample_rate_hz is None:
                raise ValueError("ECG waveform is missing sample_rate_hz.")
            amplitude_mv, correlation, noise_rms_mv = ecg_waveform_metrics(
                np.asarray(record.values, dtype=float),
                record.sample_rate_hz,
                specs.ecg_fixture_rate_hz,
            )
            waveform_ok = (
                specs.ecg_amplitude_min_mv <= amplitude_mv <= specs.ecg_amplitude_max_mv
                and correlation >= specs.ecg_correlation_min
                and noise_rms_mv <= specs.ecg_noise_rms_max_mv
            )
            results.append(
                StepResult(
                    "ECG waveform response",
                    waveform_ok,
                    f"amplitude {amplitude_mv:.2f} mV, correlation {correlation:.3f}, noise {noise_rms_mv:.3f} mV RMS",
                    f"amplitude {specs.ecg_amplitude_min_mv:.2f} to {specs.ecg_amplitude_max_mv:.2f} mV; correlation ≥ {specs.ecg_correlation_min:.2f}; noise ≤ {specs.ecg_noise_rms_max_mv:.2f} mV RMS",
                    None if waveform_ok else "ECG_WAVEFORM_RESPONSE_FAIL",
                )
            )
        elif require_ecg_waveform:
            results.append(
                StepResult(
                    "ECG waveform response",
                    False,
                    "measurement missing",
                    "ECG waveform required by product profile",
                    "ECG_WAVEFORM_REQUIRED_MEASUREMENT_MISSING",
                )
            )

        passed = all(result.passed for result in results)
        return DeviceDisposition(
            records[0].device_id,
            records[0].session_id,
            passed,
            tuple(results),
        )

