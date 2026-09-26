"""Generate deterministic, wearable-relevant measurements for software testing."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import sys
import uuid

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from weartest.data import MeasurementRecord
from weartest.grr import GrrMetricDefinition
from weartest.test_engine import GoldenUnitConfiguration, build_ecg_reference_waveform


@dataclass(frozen=True)
class FaultProfile:
    """Select synthetic defects to inject into a virtual wearable.

    Args:
        battery_low: Simulate an out-of-spec battery voltage.
        charging_current_low: Simulate a weak charging-current path.
        idle_current_high: Simulate excessive sleep/idle current.
        active_current_high: Simulate excessive active current.
        accel_bias: Add excessive X-axis accelerometer offset.
        gyro_bias: Add excessive Z-axis gyroscope bias.
        ppg_low_snr: Increase PPG optical noise.
        ppg_saturation: Saturate a block of PPG samples.
        spo2_low_snr: Degrade both red and infrared SpO₂ optical channels.
        spo2_ratio_error: Shift the red/IR amplitude ratio away from reference.
        spo2_saturation: Saturate a block of SpO₂ optical samples.
        skin_temp_offset: Add excessive temperature offset.
        ble_rf_fault: Degrade BLE transmit power, PER, and carrier frequency error.
        haptic_weak: Reduce haptic vibration amplitude.
        haptic_frequency_shift: Shift haptic vibration away from the reference frequency.
        ecg_high_impedance: Simulate a high-impedance ECG electrode path.
        ecg_waveform_distortion: Distort the injected ECG waveform response.
    """

    battery_low: bool = False
    charging_current_low: bool = False
    idle_current_high: bool = False
    active_current_high: bool = False
    accel_bias: bool = False
    gyro_bias: bool = False
    ppg_low_snr: bool = False
    ppg_saturation: bool = False
    spo2_low_snr: bool = False
    spo2_ratio_error: bool = False
    spo2_saturation: bool = False
    skin_temp_offset: bool = False
    ble_rf_fault: bool = False
    haptic_weak: bool = False
    haptic_frequency_shift: bool = False
    ecg_high_impedance: bool = False
    ecg_waveform_distortion: bool = False


class WearableSimulator:
    """Create bench-style data for a generic wearable sensor architecture.

    The simulator represents electrical power, motion, optical sensing, thermal,
    BLE/RF, haptic, and ECG test paths. Its values are project assumptions for
    exercising the software, not production specifications from a manufacturer.

    Args:
        rng_seed: Optional seed that makes generated data repeatable in tests.
    """

    def __init__(self, rng_seed: int | None = None) -> None:
        """Create a repeatable random-number generator.

        Args:
            rng_seed: Optional NumPy random seed.
        """

        self.rng = np.random.default_rng(rng_seed)

    def _record(
        self,
        *,
        seq: int,
        name: str,
        unit: str,
        values,
        fs: float | None,
        device_id: str,
        station_id: str,
        session_id: str,
        step: str,
    ) -> MeasurementRecord:
        """Build one canonical record from a simulated fixture measurement.

        Args:
            seq: Sequence number within the session.
            name: Canonical measurement name.
            unit: Canonical engineering unit.
            values: Scalar or waveform data.
            fs: Sample rate in hertz for waveform data.
            device_id: Device-under-test identifier.
            station_id: Simulated test station identifier.
            session_id: Unique simulated session identifier.
            step: Human-readable acquisition step.

        Returns:
            Canonical measurement record.
        """

        return MeasurementRecord.create(
            device_id=device_id,
            station_id=station_id,
            session_id=session_id,
            sequence=seq,
            measurement=name,
            unit=unit,
            values=values,
            sample_rate_hz=fs,
            test_step=step,
            source_format="simulator",
            source_name="WearableSimulator",
        )

    def acquire(
        self,
        device_id: str,
        station_id: str,
        faults: FaultProfile | None = None,
        *,
        include_spo2: bool = True,
        include_power_current: bool = True,
        include_ble_rf: bool = True,
        include_haptic: bool = True,
        include_ecg: bool = True,
        include_ecg_waveform: bool | None = None,
    ) -> list[MeasurementRecord]:
        """Simulate one complete end-of-line wearable acquisition.

        Args:
            device_id: Device-under-test identifier.
            station_id: Test station identifier.
            faults: Optional synthetic defect profile.
            include_spo2: Include red/IR SpO₂ optical records.
            include_power_current: Include charging, idle, and active-current records.
            include_ble_rf: Include BLE/RF functional metrics.
            include_haptic: Include the haptic vibration waveform.
            include_ecg: Include ECG electrode impedance.
            include_ecg_waveform: Include ECG waveform response. Defaults to ``include_ecg``.

        Returns:
            Canonical records for one simulated test session.
        """

        active_faults = faults or FaultProfile()
        if include_ecg_waveform is None:
            include_ecg_waveform = include_ecg
        session_id = str(uuid.uuid4())
        records: list[MeasurementRecord] = []
        sequence = 0

        def add(name: str, unit: str, values, fs: float | None, step: str) -> None:
            """Append one canonical measurement and advance the sequence counter.

            Args:
                name: Canonical measurement name.
                unit: Canonical unit.
                values: Scalar or waveform values.
                fs: Sample rate in hertz, or None for a scalar.
                step: Human-readable acquisition step.
            """

            nonlocal sequence
            records.append(
                self._record(
                    seq=sequence,
                    name=name,
                    unit=unit,
                    values=values,
                    fs=fs,
                    device_id=device_id,
                    station_id=station_id,
                    session_id=session_id,
                    step=step,
                )
            )
            sequence += 1

        battery = self.rng.normal(4.05, 0.025)
        if active_faults.battery_low:
            battery = self.rng.normal(3.72, 0.02)
        add("battery_voltage", "V", float(battery), None, "Power and communication")

        if include_power_current:
            charging = self.rng.normal(260.0, 8.0)
            idle = self.rng.normal(2.2, 0.18)
            active = self.rng.normal(45.0, 2.5)
            if active_faults.charging_current_low:
                charging = self.rng.normal(115.0, 6.0)
            if active_faults.idle_current_high:
                idle = self.rng.normal(8.5, 0.4)
            if active_faults.active_current_high:
                active = self.rng.normal(105.0, 5.0)
            add("charging_current", "mA", float(charging), None, "Charging and power-current test")
            add("idle_current", "mA", float(idle), None, "Charging and power-current test")
            add("active_current", "mA", float(active), None, "Charging and power-current test")

        imu_rate_hz = 100.0
        imu_samples = 500
        accel_bias = np.array([0.0, 0.0, 0.0])
        if active_faults.accel_bias:
            accel_bias[0] = 0.080
        accel_means = np.array([0.0, 0.0, 1.0]) + accel_bias
        for axis, mean in zip("xyz", accel_means):
            add(
                f"accel_{axis}",
                "g",
                self.rng.normal(mean, 0.006, imu_samples),
                imu_rate_hz,
                "Motion sensor static test",
            )

        gyro_means = np.array([0.08, -0.06, 0.10])
        if active_faults.gyro_bias:
            gyro_means[2] = 1.20
        for axis, mean in zip("xyz", gyro_means):
            add(
                f"gyro_{axis}",
                "deg/s",
                self.rng.normal(mean, 0.08, imu_samples),
                imu_rate_hz,
                "Motion sensor static test",
            )

        optical_rate_hz = 100.0
        optical_samples = 800
        optical_time = np.arange(optical_samples) / optical_rate_hz
        clean_ppg = 1.0 + 0.22 * np.sin(2 * math.pi * 1.2 * optical_time)
        ppg_noise = 0.10 if active_faults.ppg_low_snr else 0.012
        ppg = clean_ppg + self.rng.normal(0.0, ppg_noise, optical_samples)
        if active_faults.ppg_saturation:
            ppg[100:180] = 1.50
        add("ppg_optical", "normalized", ppg, optical_rate_hz, "Optical PPG response")

        if include_spo2:
            red_amplitude = 0.18
            ir_amplitude = 0.21
            if active_faults.spo2_ratio_error:
                red_amplitude = 0.105
            spo2_noise = 0.090 if active_faults.spo2_low_snr else 0.010
            red = 1.0 + red_amplitude * np.sin(2 * math.pi * 1.2 * optical_time)
            ir = 1.05 + ir_amplitude * np.sin(2 * math.pi * 1.2 * optical_time + 0.02)
            red += self.rng.normal(0.0, spo2_noise, optical_samples)
            ir += self.rng.normal(0.0, spo2_noise, optical_samples)
            if active_faults.spo2_saturation:
                red[120:200] = 1.50
                ir[120:200] = 1.50
            add("spo2_red_optical", "normalized", red, optical_rate_hz, "SpO2 red optical fixture")
            add("spo2_ir_optical", "normalized", ir, optical_rate_hz, "SpO2 infrared optical fixture")

        temperature = self.rng.normal(33.0, 0.12)
        if active_faults.skin_temp_offset:
            temperature += 1.2
        add("skin_temperature", "degC", float(temperature), None, "Temperature reference test")

        if include_ble_rf:
            tx_power = self.rng.normal(-1.0, 0.35)
            packet_error_rate = max(0.0, self.rng.normal(0.20, 0.08))
            frequency_error = self.rng.normal(8.0, 3.0)
            if active_faults.ble_rf_fault:
                tx_power = self.rng.normal(-10.0, 0.5)
                packet_error_rate = self.rng.normal(6.0, 0.5)
                frequency_error = self.rng.normal(95.0, 5.0)
            add("ble_tx_power", "dBm", float(tx_power), None, "BLE RF functional test")
            add(
                "ble_packet_error_rate",
                "%",
                float(packet_error_rate),
                None,
                "BLE RF functional test",
            )
            add(
                "ble_frequency_error",
                "kHz",
                float(frequency_error),
                None,
                "BLE RF functional test",
            )

        if include_haptic:
            haptic_rate_hz = 1000.0
            haptic_samples = 1000
            haptic_time = np.arange(haptic_samples) / haptic_rate_hz
            frequency_hz = 130.0 if active_faults.haptic_frequency_shift else 175.0
            amplitude_g = 0.10 if active_faults.haptic_weak else 0.40
            haptic = amplitude_g * np.sin(2 * math.pi * frequency_hz * haptic_time)
            haptic += self.rng.normal(0.0, 0.012, haptic_samples)
            add("haptic_vibration", "g", haptic, haptic_rate_hz, "Haptic motor vibration test")

        if include_ecg:
            impedance = self.rng.normal(90.0, 8.0)
            if active_faults.ecg_high_impedance:
                impedance = self.rng.normal(210.0, 10.0)
            add("ecg_electrode_impedance", "kohm", float(impedance), None, "ECG electrode path test")

        if include_ecg_waveform:
            ecg_rate_hz = 250.0
            ecg_samples = 1000
            fixture_rate_hz = 0.72 if active_faults.ecg_waveform_distortion else 1.0
            ecg = build_ecg_reference_waveform(ecg_samples, ecg_rate_hz, fixture_rate_hz)
            amplitude_mv = 0.55 if active_faults.ecg_waveform_distortion else 1.0
            ecg = amplitude_mv * ecg + self.rng.normal(
                0.0,
                0.11 if active_faults.ecg_waveform_distortion else 0.025,
                ecg_samples,
            )
            add("ecg_waveform", "mV", ecg, ecg_rate_hz, "Injected ECG waveform test")

        return records

    def acquire_golden(
        self,
        config: GoldenUnitConfiguration,
        station_id: str,
        check_number: int,
    ) -> list[MeasurementRecord]:
        """Simulate a known-good reference measured by one station.

        Args:
            config: Golden-unit references and station-drift assumptions.
            station_id: Test station being verified.
            check_number: One-based sequential golden check number.

        Returns:
            Canonical measurements representing the station's view of the golden unit.

        Raises:
            ValueError: If the station is not configured or check number is invalid.
        """

        if station_id not in config.station_drift:
            raise ValueError(f"No golden-unit drift profile is configured for {station_id}.")
        if check_number < 1:
            raise ValueError("Golden-unit check_number must be at least 1.")

        drift = config.station_drift[station_id]
        references = config.references
        multiplier = float(check_number - 1)
        session_id = str(uuid.uuid4())
        records: list[MeasurementRecord] = []
        sequence = 0

        def station_value(reference_key: str, offset_key: str, drift_key: str) -> float:
            """Apply configured tester offset and progressive drift.

            Args:
                reference_key: Golden reference key.
                offset_key: Initial station-offset key.
                drift_key: Per-check drift key.

            Returns:
                Station measurement before repeatability noise.
            """

            return references[reference_key] + drift[offset_key] + multiplier * drift[drift_key]

        def add(name: str, unit: str, values, fs: float | None, step: str) -> None:
            """Append one golden-unit measurement.

            Args:
                name: Canonical measurement name.
                unit: Canonical engineering unit.
                values: Scalar or waveform values.
                fs: Sample rate in hertz, or None for a scalar.
                step: Human-readable golden-check step.
            """

            nonlocal sequence
            records.append(
                self._record(
                    seq=sequence,
                    name=name,
                    unit=unit,
                    values=values,
                    fs=fs,
                    device_id=config.device_id,
                    station_id=station_id,
                    session_id=session_id,
                    step=step,
                )
            )
            sequence += 1

        battery = station_value("battery_voltage", "battery_offset_v", "battery_drift_per_check_v")
        add("battery_voltage", "V", battery + self.rng.normal(0.0, 0.002), None, "Golden-unit power reference")

        if config.include_power_current:
            power_metrics = (
                ("charging_current", "charging_current_offset_ma", "charging_current_drift_per_check_ma", 1.0),
                ("idle_current", "idle_current_offset_ma", "idle_current_drift_per_check_ma", 0.03),
                ("active_current", "active_current_offset_ma", "active_current_drift_per_check_ma", 0.5),
            )
            for name, offset_key, drift_key, sigma in power_metrics:
                measured = station_value(name, offset_key, drift_key) + self.rng.normal(0.0, sigma)
                add(name, "mA", measured, None, "Golden-unit power-current reference")

        imu_rate_hz = 100.0
        imu_samples = 500
        for axis in "xyz":
            mean = station_value(
                f"accel_{axis}", f"accel_{axis}_offset_g", f"accel_{axis}_drift_per_check_g"
            )
            add(
                f"accel_{axis}",
                "g",
                self.rng.normal(mean, 0.003, imu_samples),
                imu_rate_hz,
                "Golden-unit motion reference",
            )
        for axis in "xyz":
            mean = station_value(
                f"gyro_{axis}", f"gyro_{axis}_offset_dps", f"gyro_{axis}_drift_per_check_dps"
            )
            add(
                f"gyro_{axis}",
                "deg/s",
                self.rng.normal(mean, 0.025, imu_samples),
                imu_rate_hz,
                "Golden-unit motion reference",
            )

        optical_rate_hz = 100.0
        optical_samples = 800
        time = np.arange(optical_samples) / optical_rate_hz
        ppg_amplitude = station_value(
            "ppg_amplitude", "ppg_amplitude_offset", "ppg_amplitude_drift_per_check"
        )
        ppg = 1.0 + ppg_amplitude * np.sin(
            2 * math.pi * config.ppg_fixture_frequency_hz * time
        ) + self.rng.normal(0.0, 0.004, optical_samples)
        add("ppg_optical", "normalized", ppg, optical_rate_hz, "Golden-unit optical reference")

        if config.include_spo2:
            for color in ("red", "ir"):
                key = f"spo2_{color}_amplitude"
                amplitude = station_value(
                    key,
                    f"spo2_{color}_amplitude_offset",
                    f"spo2_{color}_amplitude_drift_per_check",
                )
                baseline = 1.0 if color == "red" else 1.05
                waveform = baseline + amplitude * np.sin(
                    2 * math.pi * config.spo2_fixture_frequency_hz * time
                ) + self.rng.normal(0.0, 0.004, optical_samples)
                add(
                    f"spo2_{color}_optical",
                    "normalized",
                    waveform,
                    optical_rate_hz,
                    f"Golden-unit SpO2 {color} optical reference",
                )

        temperature = station_value(
            "skin_temperature", "skin_temperature_offset_c", "skin_temperature_drift_per_check_c"
        ) + self.rng.normal(0.0, 0.015)
        add("skin_temperature", "degC", temperature, None, "Golden-unit temperature reference")

        if config.include_ble_rf:
            ble_metrics = (
                ("ble_tx_power", "dBm", "ble_tx_power_offset_dbm", "ble_tx_power_drift_per_check_dbm", 0.05),
                ("ble_packet_error_rate", "%", "ble_per_offset_percent", "ble_per_drift_per_check_percent", 0.01),
                ("ble_frequency_error", "kHz", "ble_frequency_error_offset_khz", "ble_frequency_error_drift_per_check_khz", 0.3),
            )
            for name, unit, offset_key, drift_key, sigma in ble_metrics:
                measured = station_value(name, offset_key, drift_key) + self.rng.normal(0.0, sigma)
                add(name, unit, measured, None, "Golden-unit BLE RF reference")

        if config.include_haptic:
            haptic_rate_hz = 1000.0
            haptic_samples = 1000
            haptic_time = np.arange(haptic_samples) / haptic_rate_hz
            rms_g = station_value(
                "haptic_rms", "haptic_rms_offset_g", "haptic_rms_drift_per_check_g"
            )
            frequency_hz = station_value(
                "haptic_frequency", "haptic_frequency_offset_hz", "haptic_frequency_drift_per_check_hz"
            )
            waveform = rms_g * math.sqrt(2.0) * np.sin(2 * math.pi * frequency_hz * haptic_time)
            waveform += self.rng.normal(0.0, 0.006, haptic_samples)
            add("haptic_vibration", "g", waveform, haptic_rate_hz, "Golden-unit haptic reference")

        if config.include_ecg:
            impedance = station_value(
                "ecg_electrode_impedance",
                "ecg_impedance_offset_kohm",
                "ecg_impedance_drift_per_check_kohm",
            ) + self.rng.normal(0.0, 0.8)
            add("ecg_electrode_impedance", "kohm", impedance, None, "Golden-unit ECG path reference")

        if config.include_ecg_waveform:
            ecg_rate_hz = 250.0
            ecg_samples = 1000
            amplitude_mv = station_value(
                "ecg_waveform_amplitude",
                "ecg_waveform_amplitude_offset_mv",
                "ecg_waveform_amplitude_drift_per_check_mv",
            )
            waveform = amplitude_mv * build_ecg_reference_waveform(
                ecg_samples, ecg_rate_hz, config.ecg_fixture_rate_hz
            )
            waveform += self.rng.normal(0.0, 0.012, ecg_samples)
            add("ecg_waveform", "mV", waveform, ecg_rate_hz, "Golden-unit ECG waveform reference")

        return records

    def measure_grr_reference(
        self,
        *,
        metric: GrrMetricDefinition,
        reference_value: float,
        station_id: str,
        golden_config: GoldenUnitConfiguration,
        station_check_number: int,
    ) -> float:
        """Simulate one repeated scalar measurement for a GR&R reference unit.

        Args:
            metric: GR&R metric definition, including repeatability noise.
            reference_value: Known value assigned to the reference unit.
            station_id: Test station making the measurement.
            golden_config: Station offset and drift configuration.
            station_check_number: Current one-based station-health drift state.

        Returns:
            Simulated scalar measurement in the metric's configured unit.

        Raises:
            ValueError: If the metric or station has no configured drift mapping.
        """

        if station_id not in golden_config.station_drift:
            raise ValueError(f"No station behavior is configured for {station_id}.")
        if station_check_number < 1:
            raise ValueError("station_check_number must be at least 1.")

        drift = golden_config.station_drift[station_id]
        mapping = {
            "battery_voltage": ("battery_offset_v", "battery_drift_per_check_v"),
            "charging_current": ("charging_current_offset_ma", "charging_current_drift_per_check_ma"),
            "idle_current": ("idle_current_offset_ma", "idle_current_drift_per_check_ma"),
            "active_current": ("active_current_offset_ma", "active_current_drift_per_check_ma"),
            "accel_x": ("accel_x_offset_g", "accel_x_drift_per_check_g"),
            "accel_y": ("accel_y_offset_g", "accel_y_drift_per_check_g"),
            "accel_z": ("accel_z_offset_g", "accel_z_drift_per_check_g"),
            "gyro_x": ("gyro_x_offset_dps", "gyro_x_drift_per_check_dps"),
            "gyro_y": ("gyro_y_offset_dps", "gyro_y_drift_per_check_dps"),
            "gyro_z": ("gyro_z_offset_dps", "gyro_z_drift_per_check_dps"),
            "ppg_amplitude": ("ppg_amplitude_offset", "ppg_amplitude_drift_per_check"),
            "spo2_red_amplitude": (
                "spo2_red_amplitude_offset", "spo2_red_amplitude_drift_per_check"
            ),
            "spo2_ir_amplitude": (
                "spo2_ir_amplitude_offset", "spo2_ir_amplitude_drift_per_check"
            ),
            "skin_temperature": (
                "skin_temperature_offset_c", "skin_temperature_drift_per_check_c"
            ),
            "ble_tx_power": ("ble_tx_power_offset_dbm", "ble_tx_power_drift_per_check_dbm"),
            "ble_packet_error_rate": (
                "ble_per_offset_percent", "ble_per_drift_per_check_percent"
            ),
            "ble_frequency_error": (
                "ble_frequency_error_offset_khz", "ble_frequency_error_drift_per_check_khz"
            ),
            "haptic_rms": ("haptic_rms_offset_g", "haptic_rms_drift_per_check_g"),
            "haptic_frequency": (
                "haptic_frequency_offset_hz", "haptic_frequency_drift_per_check_hz"
            ),
            "ecg_electrode_impedance": (
                "ecg_impedance_offset_kohm", "ecg_impedance_drift_per_check_kohm"
            ),
            "ecg_waveform_amplitude": (
                "ecg_waveform_amplitude_offset_mv", "ecg_waveform_amplitude_drift_per_check_mv"
            ),
        }
        if metric.key not in mapping:
            raise ValueError(f"No GR&R station-bias mapping exists for {metric.key!r}.")

        offset_key, drift_key = mapping[metric.key]
        multiplier = float(station_check_number - 1)
        station_bias = drift[offset_key] + multiplier * drift[drift_key]
        repeatability_noise = self.rng.normal(0.0, metric.repeatability_sigma)
        return float(reference_value + station_bias + repeatability_noise)
