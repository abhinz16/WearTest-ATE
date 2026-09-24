"""Crossed Gage R&R analysis for WearTest measurement-system studies.

The module deliberately keeps the statistics separate from the GUI and device
acceptance engine. A GR&R study answers a different question: how much of the
observed measurement variation comes from the measurement system itself?
"""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
import math
import sys
import uuid

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_GRR_CONFIG_PATH = PROJECT_ROOT / "config" / "grr_study.ini"


@dataclass(frozen=True)
class GrrMetricDefinition:
    """One scalar measurement that can be used in a GR&R study.

    Args:
        key: Stable internal measurement key.
        display_name: Human-readable measurement name.
        unit: Engineering unit used by the study.
        nominal: Center value for the simulated reference-unit set.
        part_span: Full range covered by the simulated reference units.
        repeatability_sigma: Simulated one-sigma within-station measurement noise.
    """

    key: str
    display_name: str
    unit: str
    nominal: float
    part_span: float
    repeatability_sigma: float


@dataclass(frozen=True)
class GrrStudyConfiguration:
    """Editable defaults and decision thresholds for crossed GR&R studies.

    Args:
        default_metric: Metric selected when the workspace opens.
        reference_units: Default number of reference parts in a study.
        trials_per_station: Default repeated measurements per part and station.
        warning_percent_grr: Upper %GRR boundary for the preferred region.
        action_percent_grr: %GRR above which the project flags the system for improvement.
        ndc_min: Minimum desired number of distinct categories.
        metrics: Supported scalar measurement definitions keyed by metric name.
    """

    default_metric: str
    reference_units: int
    trials_per_station: int
    warning_percent_grr: float
    action_percent_grr: float
    ndc_min: int
    metrics: dict[str, GrrMetricDefinition]


@dataclass(frozen=True)
class GrrObservation:
    """One repeated measurement used by a crossed GR&R calculation.

    Args:
        part_id: Reference-unit identifier.
        reference_value: Known value assigned to the reference unit.
        station_id: Test station that made the measurement.
        trial: One-based repeated-trial number.
        measured_value: Value reported by the simulated measurement system.
    """

    part_id: str
    reference_value: float
    station_id: str
    trial: int
    measured_value: float

    @property
    def error(self) -> float:
        """Return measured minus known reference value.

        Returns:
            Signed measurement error in the metric's engineering unit.
        """

        return self.measured_value - self.reference_value


@dataclass(frozen=True)
class GrrStudyResult:
    """Summary of one balanced crossed Gage R&R study.

    Args:
        study_id: Unique identifier for traceability.
        metric_key: Stable measurement key.
        metric_name: Human-readable measurement name.
        unit: Engineering unit.
        part_count: Number of reference units.
        station_count: Number of stations.
        trials_per_station: Repeated trials per part/station combination.
        repeatability_sigma: Equipment-variation standard deviation.
        station_sigma: Station-to-station standard deviation component.
        interaction_sigma: Part-by-station interaction standard deviation component.
        reproducibility_sigma: Combined station and interaction standard deviation.
        grr_sigma: Combined repeatability and reproducibility standard deviation.
        part_sigma: Estimated part-to-part standard deviation.
        total_sigma: Combined measurement and part standard deviation.
        percent_grr: GR&R as a percentage of total study variation.
        ndc: Number of distinct categories estimated from part and GR&R variation.
        assessment: Acceptable, Review, or Needs improvement.
        station_biases: Mean measured-minus-reference bias for each station.
        observations: Raw repeated measurements used by the calculation.
    """

    study_id: str
    metric_key: str
    metric_name: str
    unit: str
    part_count: int
    station_count: int
    trials_per_station: int
    repeatability_sigma: float
    station_sigma: float
    interaction_sigma: float
    reproducibility_sigma: float
    grr_sigma: float
    part_sigma: float
    total_sigma: float
    percent_grr: float
    ndc: int
    assessment: str
    station_biases: dict[str, float]
    observations: tuple[GrrObservation, ...]

    @property
    def study_variation_grr(self) -> float:
        """Return six-sigma GR&R study variation.

        Returns:
            Six times the combined GR&R standard deviation.
        """

        return 6.0 * self.grr_sigma


def load_grr_configuration(path: str | Path | None = None) -> GrrStudyConfiguration:
    """Load GR&R defaults and supported metrics from an editable INI file.

    Args:
        path: Optional configuration path. When omitted, WearTest uses
            ``config/grr_study.ini`` from the project root.

    Returns:
        Validated GR&R study configuration.

    Raises:
        FileNotFoundError: If the configuration file is missing.
        ValueError: If required values are invalid or no metrics are defined.
    """

    config_path = Path(path) if path is not None else DEFAULT_GRR_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"GR&R configuration file not found: {config_path}")

    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")

    try:
        default_metric = parser.get("study", "default_metric").strip()
        reference_units = parser.getint("study", "reference_units")
        trials_per_station = parser.getint("study", "trials_per_station")
        warning_percent_grr = parser.getfloat("study", "warning_percent_grr")
        action_percent_grr = parser.getfloat("study", "action_percent_grr")
        ndc_min = parser.getint("study", "ndc_min")
    except Exception as exc:
        raise ValueError(f"Invalid [study] settings in {config_path}.") from exc

    if reference_units < 2:
        raise ValueError("GR&R reference_units must be at least 2.")
    if trials_per_station < 2:
        raise ValueError("GR&R trials_per_station must be at least 2.")
    if not 0.0 < warning_percent_grr < action_percent_grr < 100.0:
        raise ValueError("GR&R percentage thresholds must satisfy 0 < warning < action < 100.")
    if ndc_min < 1:
        raise ValueError("GR&R ndc_min must be at least 1.")

    metrics: dict[str, GrrMetricDefinition] = {}
    for section in parser.sections():
        if not section.lower().startswith("metric:"):
            continue
        key = section.split(":", 1)[1].strip()
        if not key:
            raise ValueError(f"Invalid metric section name: {section}")
        try:
            definition = GrrMetricDefinition(
                key=key,
                display_name=parser.get(section, "display_name").strip(),
                unit=parser.get(section, "unit").strip(),
                nominal=parser.getfloat(section, "nominal"),
                part_span=parser.getfloat(section, "part_span"),
                repeatability_sigma=parser.getfloat(section, "repeatability_sigma"),
            )
        except Exception as exc:
            raise ValueError(f"Invalid [{section}] settings in {config_path}.") from exc
        if not definition.display_name or not definition.unit:
            raise ValueError(f"GR&R metric {key!r} requires display_name and unit.")
        if definition.part_span <= 0.0:
            raise ValueError(f"GR&R metric {key!r} must have a positive part_span.")
        if definition.repeatability_sigma <= 0.0:
            raise ValueError(
                f"GR&R metric {key!r} must have a positive repeatability_sigma."
            )
        metrics[key] = definition

    if not metrics:
        raise ValueError("GR&R configuration must define at least one [metric:...] section.")
    if default_metric not in metrics:
        raise ValueError(f"GR&R default_metric {default_metric!r} is not defined.")

    return GrrStudyConfiguration(
        default_metric=default_metric,
        reference_units=reference_units,
        trials_per_station=trials_per_station,
        warning_percent_grr=warning_percent_grr,
        action_percent_grr=action_percent_grr,
        ndc_min=ndc_min,
        metrics=metrics,
    )


def build_reference_values(metric: GrrMetricDefinition, count: int) -> list[tuple[str, float]]:
    """Create evenly spaced known reference values across the configured part span.

    Args:
        metric: Measurement definition containing nominal value and part span.
        count: Number of known reference units to create.

    Returns:
        ``(part_id, reference_value)`` pairs ordered from low to high reference value.
    """

    if count < 2:
        raise ValueError("A GR&R study requires at least two reference units.")
    half_span = metric.part_span / 2.0
    values = np.linspace(metric.nominal - half_span, metric.nominal + half_span, count)
    return [(f"REF-{index:03d}", float(value)) for index, value in enumerate(values, start=1)]


def run_crossed_grr(
    observations: list[GrrObservation],
    *,
    metric: GrrMetricDefinition,
    config: GrrStudyConfiguration,
    study_id: str | None = None,
) -> GrrStudyResult:
    """Calculate variance components for a balanced crossed station-by-part GR&R study.

    The implementation uses a two-way random-effects ANOVA with repeated trials.
    Station is used as the reproducibility factor. Part-by-station interaction is
    included in reproducibility because a station can respond differently to
    different reference units.

    Args:
        observations: Balanced measurements for every part, station, and trial.
        metric: Definition of the scalar measurement being studied.
        config: GR&R thresholds used for the final assessment.
        study_id: Optional caller-supplied study identifier.

    Returns:
        Calculated GR&R variance components and raw observations.

    Raises:
        ValueError: If the data are incomplete, unbalanced, or too small for ANOVA.
    """

    if not observations:
        raise ValueError("No GR&R observations were supplied.")

    parts = sorted({item.part_id for item in observations})
    stations = sorted({item.station_id for item in observations})
    trials = sorted({item.trial for item in observations})
    p, s, r = len(parts), len(stations), len(trials)
    if p < 2 or s < 2 or r < 2:
        raise ValueError("Crossed GR&R requires at least 2 parts, 2 stations, and 2 trials.")

    part_index = {part: index for index, part in enumerate(parts)}
    station_index = {station: index for index, station in enumerate(stations)}
    trial_index = {trial: index for index, trial in enumerate(trials)}
    data = np.full((p, s, r), np.nan, dtype=float)
    references: dict[str, float] = {}

    for item in observations:
        i = part_index[item.part_id]
        j = station_index[item.station_id]
        k = trial_index[item.trial]
        if not np.isnan(data[i, j, k]):
            raise ValueError(
                f"Duplicate GR&R observation for {item.part_id}, {item.station_id}, trial {item.trial}."
            )
        data[i, j, k] = item.measured_value
        if item.part_id in references and not math.isclose(
            references[item.part_id], item.reference_value, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError(f"Reference value changed within part {item.part_id}.")
        references[item.part_id] = item.reference_value

    if np.isnan(data).any():
        raise ValueError("GR&R observations are incomplete or unbalanced.")

    grand_mean = float(np.mean(data))
    part_means = np.mean(data, axis=(1, 2))
    station_means = np.mean(data, axis=(0, 2))
    cell_means = np.mean(data, axis=2)

    ss_part = float(s * r * np.sum((part_means - grand_mean) ** 2))
    ss_station = float(p * r * np.sum((station_means - grand_mean) ** 2))
    interaction_residual = (
        cell_means
        - part_means[:, None]
        - station_means[None, :]
        + grand_mean
    )
    ss_interaction = float(r * np.sum(interaction_residual ** 2))
    ss_repeat = float(np.sum((data - cell_means[:, :, None]) ** 2))

    df_part = p - 1
    df_station = s - 1
    df_interaction = (p - 1) * (s - 1)
    df_repeat = p * s * (r - 1)

    ms_part = ss_part / df_part
    ms_station = ss_station / df_station
    ms_interaction = ss_interaction / df_interaction
    ms_repeat = ss_repeat / df_repeat

    var_repeat = max(ms_repeat, 0.0)
    var_interaction = max((ms_interaction - ms_repeat) / r, 0.0)
    var_station = max((ms_station - ms_interaction) / (p * r), 0.0)
    var_part = max((ms_part - ms_interaction) / (s * r), 0.0)

    repeatability_sigma = math.sqrt(var_repeat)
    station_sigma = math.sqrt(var_station)
    interaction_sigma = math.sqrt(var_interaction)
    reproducibility_sigma = math.sqrt(var_station + var_interaction)
    grr_sigma = math.sqrt(var_repeat + var_station + var_interaction)
    part_sigma = math.sqrt(var_part)
    total_sigma = math.sqrt(grr_sigma**2 + part_sigma**2)
    percent_grr = 100.0 * grr_sigma / total_sigma if total_sigma > 0.0 else 0.0
    ndc = int(math.floor(1.41 * part_sigma / grr_sigma)) if grr_sigma > 0.0 else 99

    if percent_grr <= config.warning_percent_grr:
        assessment = "Acceptable"
    elif percent_grr <= config.action_percent_grr:
        assessment = "Review"
    else:
        assessment = "Needs improvement"

    station_biases: dict[str, float] = {}
    for station in stations:
        station_items = [item for item in observations if item.station_id == station]
        station_biases[station] = float(np.mean([item.error for item in station_items]))

    return GrrStudyResult(
        study_id=study_id or str(uuid.uuid4()),
        metric_key=metric.key,
        metric_name=metric.display_name,
        unit=metric.unit,
        part_count=p,
        station_count=s,
        trials_per_station=r,
        repeatability_sigma=repeatability_sigma,
        station_sigma=station_sigma,
        interaction_sigma=interaction_sigma,
        reproducibility_sigma=reproducibility_sigma,
        grr_sigma=grr_sigma,
        part_sigma=part_sigma,
        total_sigma=total_sigma,
        percent_grr=percent_grr,
        ndc=ndc,
        assessment=assessment,
        station_biases=station_biases,
        observations=tuple(observations),
    )
