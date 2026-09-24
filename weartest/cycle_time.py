"""Cycle-time modeling and coverage validation for WearTest ATE."""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass, replace
from pathlib import Path
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from weartest.data import MeasurementRecord
from weartest.simulator import FaultProfile


DEFAULT_CYCLE_TIME_CONFIG_PATH = PROJECT_ROOT / "config" / "cycle_time.ini"


@dataclass(frozen=True)
class CycleCandidate:
    """One candidate manufacturing test sequence.

    Args:
        key: Stable internal identifier.
        name: Human-readable strategy name.
        imu_window_s: Motion acquisition window in seconds.
        ppg_window_s: Optical acquisition window in seconds.
        parallel_motion: Whether accelerometer and gyroscope acquisition overlap.
        parallel_optical_temperature: Whether optical and temperature checks overlap.
        baseline: Whether this strategy is the reference sequence.
    """

    key: str
    name: str
    imu_window_s: float
    ppg_window_s: float
    parallel_motion: bool
    parallel_optical_temperature: bool
    baseline: bool = False


@dataclass(frozen=True)
class CycleTimeConfiguration:
    """Editable assumptions and validation guardrails for a cycle-time study.

    Args:
        default_product: Product profile selected by default in the GUI.
        validation_devices: Number of simulated devices used for validation.
        rng_seed: Reproducible simulator seed.
        min_detection_retention_percent: Minimum retained defect detection.
        max_escape_rate_percent: Maximum allowed candidate escaped-defect rate.
        max_false_reject_increase_pp: Maximum false-reject increase in percentage points.
        communication_s: Fixed communication/setup time.
        battery_s: Fixed battery-check time.
        temperature_s: Fixed temperature-check time.
        ecg_s: Fixed ECG-path check time when ECG is required.
        record_and_save_s: Fixed disposition and traceability write time.
        baseline: Reference sequential test strategy.
        candidates: Faster strategies evaluated against the baseline.
    """

    default_product: str
    validation_devices: int
    rng_seed: int
    min_detection_retention_percent: float
    max_escape_rate_percent: float
    max_false_reject_increase_pp: float
    communication_s: float
    battery_s: float
    temperature_s: float
    ecg_s: float
    record_and_save_s: float
    baseline: CycleCandidate
    candidates: tuple[CycleCandidate, ...]


@dataclass
class CandidateValidationCounts:
    """Accumulate validation outcomes for one cycle-time candidate.

    Args:
        total: Number of evaluated devices.
        known_defects: Number of devices with an intentionally injected fault.
        healthy_devices: Number of devices with no injected fault.
        baseline_detected: Defective devices failed by the baseline sequence.
        candidate_detected: Defective devices failed by this candidate.
        baseline_false_rejects: Healthy devices failed by the baseline.
        candidate_false_rejects: Healthy devices failed by this candidate.
        agreements: Devices receiving the same overall disposition as baseline.
    """

    total: int = 0
    known_defects: int = 0
    healthy_devices: int = 0
    baseline_detected: int = 0
    candidate_detected: int = 0
    baseline_false_rejects: int = 0
    candidate_false_rejects: int = 0
    agreements: int = 0

    def record(self, *, defective: bool, baseline_failed: bool, candidate_failed: bool) -> None:
        """Add one baseline-versus-candidate validation case.

        Args:
            defective: Whether a synthetic fault was intentionally injected.
            baseline_failed: Whether the baseline sequence rejected the device.
            candidate_failed: Whether this candidate rejected the device.
        """

        self.total += 1
        self.agreements += baseline_failed == candidate_failed
        if defective:
            self.known_defects += 1
            self.baseline_detected += baseline_failed
            self.candidate_detected += candidate_failed
        else:
            self.healthy_devices += 1
            self.baseline_false_rejects += baseline_failed
            self.candidate_false_rejects += candidate_failed


@dataclass(frozen=True)
class CycleCandidateResult:
    """Validated performance and throughput for one sequence.

    Args:
        candidate: Sequence definition that was evaluated.
        cycle_seconds: Estimated station cycle time.
        units_per_hour: Idealized single-station throughput.
        reduction_percent: Cycle-time reduction relative to baseline.
        detection_retention_percent: Defect detection retained versus baseline.
        escape_rate_percent: Injected defects that passed the candidate.
        false_reject_rate_percent: Healthy simulated devices rejected by candidate.
        false_reject_change_pp: Candidate minus baseline false-reject rate.
        agreement_percent: Overall candidate/baseline disposition agreement.
        validated: Whether the strategy met every configured quality guardrail.
        decision_reason: Short explanation of validation status.
    """

    candidate: CycleCandidate
    cycle_seconds: float
    units_per_hour: float
    reduction_percent: float
    detection_retention_percent: float
    escape_rate_percent: float
    false_reject_rate_percent: float
    false_reject_change_pp: float
    agreement_percent: float
    validated: bool
    decision_reason: str


@dataclass(frozen=True)
class CycleTimeStudyResult:
    """Completed cycle-time optimization study.

    Args:
        study_id: Unique identifier for database traceability.
        product_name: Product profile used during validation.
        validation_devices: Number of simulated validation devices.
        baseline: Baseline sequence result.
        candidates: Faster candidate results.
        recommended_key: Key of the fastest validated strategy.
    """

    study_id: str
    product_name: str
    validation_devices: int
    baseline: CycleCandidateResult
    candidates: tuple[CycleCandidateResult, ...]
    recommended_key: str

    @property
    def recommended(self) -> CycleCandidateResult:
        """Return the selected fastest validated strategy.

        Returns:
            Candidate result whose key matches ``recommended_key``.
        """

        for result in (self.baseline, *self.candidates):
            if result.candidate.key == self.recommended_key:
                return result
        return self.baseline


def _candidate_from_section(
    parser: ConfigParser,
    section: str,
    *,
    key: str,
    baseline: bool,
) -> CycleCandidate:
    """Create one candidate from an INI section.

    Args:
        parser: Loaded configuration parser.
        section: INI section containing candidate values.
        key: Stable candidate key.
        baseline: Whether this is the reference strategy.

    Returns:
        Parsed cycle candidate.
    """

    name = parser.get(section, "name", fallback=section.split(":", 1)[-1]).strip()
    candidate = CycleCandidate(
        key=key,
        name=name,
        imu_window_s=parser.getfloat(section, "imu_window_s"),
        ppg_window_s=parser.getfloat(section, "ppg_window_s"),
        parallel_motion=parser.getboolean(section, "parallel_motion"),
        parallel_optical_temperature=parser.getboolean(section, "parallel_optical_temperature"),
        baseline=baseline,
    )
    if candidate.imu_window_s <= 0 or candidate.ppg_window_s <= 0:
        raise ValueError(f"Cycle-time windows must be positive in [{section}].")
    return candidate


def load_cycle_time_configuration(
    path: str | Path | None = None,
) -> CycleTimeConfiguration:
    """Load cycle-time assumptions and candidate strategies from INI.

    Args:
        path: Optional configuration path. Defaults to ``config/cycle_time.ini``.

    Returns:
        Validated cycle-time configuration.

    Raises:
        FileNotFoundError: If the configuration is missing.
        ValueError: If required settings are inconsistent.
    """

    config_path = Path(path) if path is not None else DEFAULT_CYCLE_TIME_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Cycle-time configuration file not found: {config_path}")

    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")
    baseline = _candidate_from_section(parser, "baseline", key="baseline", baseline=True)
    candidates: list[CycleCandidate] = []
    for section in parser.sections():
        if not section.lower().startswith("candidate:"):
            continue
        name = section.split(":", 1)[1].strip()
        key = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
        candidates.append(_candidate_from_section(parser, section, key=key, baseline=False))

    configuration = CycleTimeConfiguration(
        default_product=parser.get("study", "default_product").strip(),
        validation_devices=parser.getint("study", "validation_devices"),
        rng_seed=parser.getint("study", "rng_seed"),
        min_detection_retention_percent=parser.getfloat(
            "study", "min_detection_retention_percent"
        ),
        max_escape_rate_percent=parser.getfloat("study", "max_escape_rate_percent"),
        max_false_reject_increase_pp=parser.getfloat(
            "study", "max_false_reject_increase_pp"
        ),
        communication_s=parser.getfloat("fixed_time", "communication_s"),
        battery_s=parser.getfloat("fixed_time", "battery_s"),
        temperature_s=parser.getfloat("fixed_time", "temperature_s"),
        ecg_s=parser.getfloat("fixed_time", "ecg_s"),
        record_and_save_s=parser.getfloat("fixed_time", "record_and_save_s"),
        baseline=baseline,
        candidates=tuple(candidates),
    )

    if configuration.validation_devices < 8:
        raise ValueError("Cycle-time validation should use at least 8 devices.")
    if not configuration.candidates:
        raise ValueError("At least one [candidate:...] cycle-time strategy is required.")
    for value in (
        configuration.communication_s,
        configuration.battery_s,
        configuration.temperature_s,
        configuration.ecg_s,
        configuration.record_and_save_s,
    ):
        if value < 0:
            raise ValueError("Fixed cycle-time components cannot be negative.")
    return configuration


def estimate_cycle_seconds(
    config: CycleTimeConfiguration,
    candidate: CycleCandidate,
    *,
    include_ecg: bool,
) -> float:
    """Estimate one station cycle time from acquisition windows and overlap.

    Args:
        config: Fixed timing assumptions and candidate definitions.
        candidate: Sequence to evaluate.
        include_ecg: Whether the product profile requires an ECG path check.

    Returns:
        Estimated cycle time in seconds.
    """

    motion_time = (
        candidate.imu_window_s
        if candidate.parallel_motion
        else 2.0 * candidate.imu_window_s
    )
    optical_temperature_time = (
        max(candidate.ppg_window_s, config.temperature_s)
        if candidate.parallel_optical_temperature
        else candidate.ppg_window_s + config.temperature_s
    )
    return (
        config.communication_s
        + config.battery_s
        + motion_time
        + optical_temperature_time
        + (config.ecg_s if include_ecg else 0.0)
        + config.record_and_save_s
    )


def truncate_records_for_candidate(
    records: list[MeasurementRecord],
    candidate: CycleCandidate,
) -> list[MeasurementRecord]:
    """Apply candidate waveform windows while preserving scalar measurements.

    Args:
        records: Full baseline acquisition records.
        candidate: Candidate sequence whose waveform windows should be modeled.

    Returns:
        New records containing only the samples available inside candidate windows.
    """

    output: list[MeasurementRecord] = []
    for record in records:
        duration_s: float | None = None
        if record.measurement.startswith("accel_") or record.measurement.startswith("gyro_"):
            duration_s = candidate.imu_window_s
        elif record.measurement == "ppg_optical":
            duration_s = candidate.ppg_window_s

        if duration_s is None or record.sample_rate_hz is None:
            output.append(record)
            continue

        requested = max(2, int(round(duration_s * record.sample_rate_hz)))
        sample_count = min(len(record.values), requested)
        output.append(replace(record, values=list(record.values[:sample_count])))
    return output


def validation_fault_profiles(*, include_ecg: bool) -> tuple[FaultProfile, ...]:
    """Return a balanced set of healthy and single-fault validation cases.

    Args:
        include_ecg: Whether ECG-specific faults apply to the selected product.

    Returns:
        Healthy profile followed by one profile for each supported defect mode.
    """

    profiles = [
        FaultProfile(),
        FaultProfile(battery_low=True),
        FaultProfile(accel_bias=True),
        FaultProfile(gyro_bias=True),
        FaultProfile(ppg_low_snr=True),
        FaultProfile(ppg_saturation=True),
        FaultProfile(skin_temp_offset=True),
    ]
    if include_ecg:
        profiles.append(FaultProfile(ecg_high_impedance=True))
    return tuple(profiles)



def fault_profile_label(profile: FaultProfile) -> str:
    """Return a short human-readable label for one validation profile.

    Args:
        profile: Synthetic device fault configuration.

    Returns:
        Friendly defect name used in progress messages.
    """

    labels = (
        (profile.battery_low, "low battery"),
        (profile.accel_bias, "accelerometer bias"),
        (profile.gyro_bias, "gyroscope bias"),
        (profile.ppg_low_snr, "low optical SNR"),
        (profile.ppg_saturation, "optical saturation"),
        (profile.skin_temp_offset, "temperature offset"),
        (profile.ecg_high_impedance, "high ECG impedance"),
    )
    active = [label for enabled, label in labels if enabled]
    return ", ".join(active) if active else "known-good device"

def is_faulty(profile: FaultProfile) -> bool:
    """Return whether a fault profile contains any intentionally injected defect.

    Args:
        profile: Synthetic fault configuration used for one validation device.

    Returns:
        True when at least one defect flag is enabled.
    """

    return any(
        (
            profile.battery_low,
            profile.accel_bias,
            profile.gyro_bias,
            profile.ppg_low_snr,
            profile.ppg_saturation,
            profile.skin_temp_offset,
            profile.ecg_high_impedance,
        )
    )


def _result_from_counts(
    *,
    config: CycleTimeConfiguration,
    candidate: CycleCandidate,
    counts: CandidateValidationCounts,
    baseline_cycle_seconds: float,
    baseline_false_reject_rate: float,
    include_ecg: bool,
) -> CycleCandidateResult:
    """Convert accumulated validation counts into one candidate result.

    Args:
        config: Study guardrails and timing assumptions.
        candidate: Sequence represented by the counts.
        counts: Accumulated baseline/candidate outcomes.
        baseline_cycle_seconds: Reference cycle time in seconds.
        baseline_false_reject_rate: Reference false-reject percentage.
        include_ecg: Whether the selected product includes ECG.

    Returns:
        Calculated candidate metrics and validation decision.
    """

    cycle_seconds = estimate_cycle_seconds(config, candidate, include_ecg=include_ecg)
    units_per_hour = 3600.0 / cycle_seconds if cycle_seconds > 0 else 0.0
    reduction = 100.0 * (baseline_cycle_seconds - cycle_seconds) / baseline_cycle_seconds

    if counts.baseline_detected:
        retention = 100.0 * counts.candidate_detected / counts.baseline_detected
    else:
        retention = 100.0
    escape_rate = (
        100.0 * (counts.known_defects - counts.candidate_detected) / counts.known_defects
        if counts.known_defects
        else 0.0
    )
    false_reject_rate = (
        100.0 * counts.candidate_false_rejects / counts.healthy_devices
        if counts.healthy_devices
        else 0.0
    )
    false_reject_change = false_reject_rate - baseline_false_reject_rate
    agreement = 100.0 * counts.agreements / counts.total if counts.total else 0.0

    if candidate.baseline:
        validated = True
        reason = "Reference sequence"
    else:
        problems: list[str] = []
        if retention < config.min_detection_retention_percent:
            problems.append("defect detection dropped")
        if escape_rate > config.max_escape_rate_percent:
            problems.append("escaped defects exceeded limit")
        if false_reject_change > config.max_false_reject_increase_pp:
            problems.append("false rejects increased too much")
        validated = not problems
        reason = "Validated" if validated else "; ".join(problems)

    return CycleCandidateResult(
        candidate=candidate,
        cycle_seconds=cycle_seconds,
        units_per_hour=units_per_hour,
        reduction_percent=reduction,
        detection_retention_percent=retention,
        escape_rate_percent=escape_rate,
        false_reject_rate_percent=false_reject_rate,
        false_reject_change_pp=false_reject_change,
        agreement_percent=agreement,
        validated=validated,
        decision_reason=reason,
    )


def finalize_cycle_time_study(
    *,
    config: CycleTimeConfiguration,
    product_name: str,
    include_ecg: bool,
    counts_by_key: dict[str, CandidateValidationCounts],
) -> CycleTimeStudyResult:
    """Calculate candidate metrics and choose the fastest validated sequence.

    Args:
        config: Timing assumptions and validation guardrails.
        product_name: Product profile used in the simulation.
        include_ecg: Whether the product requires ECG.
        counts_by_key: Validation counters keyed by candidate key, including baseline.

    Returns:
        Completed cycle-time study with the recommended strategy.
    """

    if "baseline" not in counts_by_key:
        raise ValueError("Cycle-time results require baseline validation counts.")

    baseline_counts = counts_by_key["baseline"]
    baseline_cycle = estimate_cycle_seconds(config, config.baseline, include_ecg=include_ecg)
    baseline_false_reject_rate = (
        100.0 * baseline_counts.baseline_false_rejects / baseline_counts.healthy_devices
        if baseline_counts.healthy_devices
        else 0.0
    )
    baseline_result = _result_from_counts(
        config=config,
        candidate=config.baseline,
        counts=baseline_counts,
        baseline_cycle_seconds=baseline_cycle,
        baseline_false_reject_rate=baseline_false_reject_rate,
        include_ecg=include_ecg,
    )

    candidate_results = tuple(
        _result_from_counts(
            config=config,
            candidate=candidate,
            counts=counts_by_key[candidate.key],
            baseline_cycle_seconds=baseline_cycle,
            baseline_false_reject_rate=baseline_false_reject_rate,
            include_ecg=include_ecg,
        )
        for candidate in config.candidates
    )

    eligible = [baseline_result] + [result for result in candidate_results if result.validated]
    recommended = min(eligible, key=lambda result: result.cycle_seconds)
    return CycleTimeStudyResult(
        study_id=str(uuid.uuid4()),
        product_name=product_name,
        validation_devices=baseline_counts.total,
        baseline=baseline_result,
        candidates=candidate_results,
        recommended_key=recommended.candidate.key,
    )
