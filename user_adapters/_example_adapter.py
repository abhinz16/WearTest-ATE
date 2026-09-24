"""Template for adding a new acquisition-file format to WearTest.

Copy this file, rename the class, and implement convert(). WearTest discovers
concrete DataAdapter subclasses in this folder when the application starts.
"""

from pathlib import Path
from typing import Any

from weartest.adapters import DataAdapter
from weartest.data import MeasurementRecord


class ExampleAdapter(DataAdapter):
    """Example only. Replace this class with a parser for a real file format."""

    format_name = "example"
    display_name = "Example custom format"
    extensions = (".example",)

    def __init__(self, mapping: dict[str, Any]) -> None:
        """Store user-supplied mapping information.

        Args:
            mapping: Parsed mapping dictionary supplied by WearTest.
        """
        self.mapping = mapping

    def convert(self, source: str | Path, **context: Any) -> list[MeasurementRecord]:
        """Convert the source file into WearTest records.

        Args:
            source: Path to the custom acquisition file.
            **context: Device ID, station ID, session ID, and other import metadata.

        Returns:
            Canonical WearTest records.

        Raises:
            NotImplementedError: Until this template is replaced by a real parser.
        """
        raise NotImplementedError("Copy this template and implement your file parser.")
