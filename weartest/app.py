"""Desktop interface for WearTest automated wearable manufacturing tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import random
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# Make this module runnable both as part of the ``weartest`` package and
# directly from Spyder.  Spyder often executes the selected file as a
# standalone script, in which case package-relative imports are unavailable.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from weartest.adapters import ExternalDataImporter
from weartest.data import MeasurementRecord, read_jsonl, write_jsonl
from weartest.grr import (
    DEFAULT_GRR_CONFIG_PATH,
    GrrObservation,
    GrrStudyResult,
    build_reference_values,
    load_grr_configuration,
    run_crossed_grr,
)
from weartest.cycle_time import (
    DEFAULT_CYCLE_TIME_CONFIG_PATH,
    CandidateValidationCounts,
    CycleTimeStudyResult,
    fault_profile_label,
    finalize_cycle_time_study,
    is_faulty,
    load_cycle_time_configuration,
    truncate_records_for_candidate,
    validation_fault_profiles,
)
from weartest.simulator import FaultProfile, WearableSimulator
from weartest.storage import ProductionDatabase, ReliableJsonlSink
from weartest.test_engine import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_GOLDEN_CONFIG_PATH,
    DEFAULT_PRODUCTS_CONFIG_PATH,
    DeviceDisposition,
    GoldenUnitEvaluator,
    ManufacturingTestEngine,
    ProductProfile,
    TesterHealthResult,
    load_golden_unit_configuration,
    load_product_profiles,
    load_test_specifications,
)


class WearTestApp(tk.Tk):
    """Operator-facing manufacturing test and traceability console.

    Args:
        runtime_dir: Optional folder for local records and the SQLite database.
        config_path: Optional INI file containing the simulated test limits.
    """

    BG = "#F2F4F7"
    PANEL = "#FFFFFF"
    PANEL_2 = "#E7F5EE"
    PANEL_3 = "#F7F8FA"
    BORDER = "#C8CED6"
    TEXT = "#000000"
    MUTED = "#3E4650"
    ACCENT = "#005B8F"
    ACCENT_DARK = "#003F63"
    PASS = "#006B4F"
    PASS_BG = "#E7F5EE"
    FAIL = "#A61B1B"
    FAIL_BG = "#FCEBEC"
    WARN = "#704A00"
    SIDEBAR = "#FFFFFF"
    ACTIVE_NAV = "#E7F0F7"
    HEADER_TINT = "#F7F8FA"

    def __init__(
        self,
        runtime_dir: str | Path | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        """Create the desktop application and initialize local services.

        Args:
            runtime_dir: Local folder for normalized records and production data.
            config_path: User-editable INI file containing acceptance limits.
        """

        super().__init__()

        available_fonts = set(tkfont.families(self))
        if "Times New Roman" in available_fonts:
            self.font_family = "Times New Roman"
        elif "Liberation Serif" in available_fonts:
            self.font_family = "Liberation Serif"
        else:
            self.font_family = "Times"

        self.title("WearTest ATE | Wearable Manufacturing Test Platform")
        self.configure(bg=self.BG)

        # Size the window to the current monitor instead of assuming a large display.
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        window_width = min(1500, max(1100, int(screen_width * 0.92)))
        window_height = min(930, max(720, int(screen_height * 0.88)))
        x_position = max(0, (screen_width - window_width) // 2)
        y_position = max(0, (screen_height - window_height) // 2)
        self.geometry(
            f"{window_width}x{window_height}+{x_position}+{y_position}"
        )
        self.minsize(min(1100, screen_width - 40), min(720, screen_height - 40))

        self.runtime_dir = Path(runtime_dir or Path.cwd() / "runtime")
        self.records_dir = self.runtime_dir / "records"
        self.records_dir.mkdir(parents=True, exist_ok=True)

        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.golden_config_path = Path(DEFAULT_GOLDEN_CONFIG_PATH)
        self.products_config_path = Path(DEFAULT_PRODUCTS_CONFIG_PATH)
        self.grr_config_path = Path(DEFAULT_GRR_CONFIG_PATH)
        self.cycle_time_config_path = Path(DEFAULT_CYCLE_TIME_CONFIG_PATH)
        self.product_profiles = load_product_profiles(self.products_config_path)
        self.product_profiles_by_name = {profile.name: profile for profile in self.product_profiles}
        self.db = ProductionDatabase(self.runtime_dir / "production.db")
        self.engine = ManufacturingTestEngine(config_path=self.config_path)
        self.golden_config = load_golden_unit_configuration(self.golden_config_path)
        self.golden_evaluator = GoldenUnitEvaluator(self.golden_config)
        self.grr_config = load_grr_configuration(self.grr_config_path)
        self.cycle_time_config = load_cycle_time_configuration(self.cycle_time_config_path)
        self.simulator = WearableSimulator(rng_seed=2026)
        self.importer = ExternalDataImporter()
        self.transport = ReliableJsonlSink(
            self.runtime_dir / "transport" / "station_inbox.jsonl",
            self.runtime_dir / "transport" / "recovery_spool.jsonl",
        )
        self.replayed_spool_count = self.transport.replay_spool()
        self.last_disposition: DeviceDisposition | None = None
        self.last_tester_health: TesterHealthResult | None = None

        # Demo-batch work is scheduled incrementally with Tk ``after`` calls.
        # This keeps the interface responsive while simulated files are created.
        self._batch_running = False
        self._batch_cancel_requested = False
        self._batch_state: dict | None = None

        # One-click multi-profile and multi-station operations are also scheduled
        # incrementally so the GUI can repaint progress between each completed item.
        self._product_batch_running = False
        self._product_batch_cancel_requested = False
        self._product_batch_state: dict | None = None
        self._golden_batch_running = False
        self._golden_batch_cancel_requested = False
        self._golden_batch_state: dict | None = None

        # GR&R studies are also scheduled one observation at a time so even a
        # larger reference-unit/trial combination remains visibly responsive.
        self._grr_running = False
        self._grr_cancel_requested = False
        self._grr_state: dict | None = None
        self.last_grr_result: GrrStudyResult | None = None

        # Cycle-time validation runs one simulated DUT at a time so the GUI can
        # repaint progress and remain responsive during a larger validation set.
        self._cycle_running = False
        self._cycle_cancel_requested = False
        self._cycle_state: dict | None = None
        self.last_cycle_time_result: CycleTimeStudyResult | None = None

        self._configure_style()
        self._build_shell()
        self._build_pages()

        # Route mouse-wheel events to the Production page when the pointer is
        # anywhere inside that scrollable workspace. This is more reliable than
        # binding only the canvas because charts, labels, and cards are child widgets.
        self.current_page = "station"
        self.bind_all("<MouseWheel>", self._route_mousewheel, add="+")
        self.bind_all("<Button-4>", self._route_mousewheel, add="+")
        self.bind_all("<Button-5>", self._route_mousewheel, add="+")

        self._refresh_history()
        self._set_ready_state()
        self._refresh_limit_summary()
        self._show_page("station")
        self._update_clock()

        if self.replayed_spool_count:
            self.integrity_vars["transport"].set(
                f"Recovered {self.replayed_spool_count} record(s) from local backup storage."
            )

    def _configure_style(self) -> None:
        """Define the light professional theme used across the application."""

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # The project intentionally uses Times New Roman throughout the interface.
        self.option_add("*Font", (self.font_family, 13))
        self.option_add("*TCombobox*Listbox.font", (self.font_family, 13))

        style.configure("App.TFrame", background=self.BG)
        style.configure("Page.TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("Panel2.TFrame", background=self.PANEL_2)
        style.configure("Sidebar.TFrame", background=self.SIDEBAR)
        style.configure("Topbar.TFrame", background=self.PANEL)

        style.configure(
            "Title.TLabel",
            background=self.BG,
            foreground=self.TEXT,
            font=(self.font_family, 23, "bold"),
        )
        style.configure(
            "PageTitle.TLabel",
            background=self.BG,
            foreground=self.TEXT,
            font=(self.font_family, 27, "bold"),
        )
        style.configure(
            "PageText.TLabel",
            background=self.BG,
            foreground=self.MUTED,
            font=(self.font_family, 14),
        )
        style.configure(
            "PanelTitle.TLabel",
            background=self.PANEL,
            foreground=self.TEXT,
            font=(self.font_family, 16, "bold"),
        )
        style.configure(
            "PanelLabel.TLabel",
            background=self.PANEL,
            foreground=self.MUTED,
            font=(self.font_family, 13, "bold"),
        )
        style.configure(
            "PanelText.TLabel",
            background=self.PANEL,
            foreground=self.TEXT,
            font=(self.font_family, 14),
        )
        style.configure(
            "Muted.TLabel",
            background=self.PANEL,
            foreground=self.MUTED,
            font=(self.font_family, 14),
        )
        style.configure(
            "Metric.TLabel",
            background=self.PANEL,
            foreground=self.TEXT,
            font=(self.font_family, 23, "bold"),
        )
        style.configure(
            "MetricName.TLabel",
            background=self.PANEL,
            foreground=self.MUTED,
            font=(self.font_family, 13, "bold"),
        )
        style.configure(
            "StatusReady.TLabel",
            background=self.PANEL,
            foreground=self.ACCENT,
            font=(self.font_family, 23, "bold"),
        )
        style.configure(
            "StatusPass.TLabel",
            background=self.PANEL,
            foreground=self.PASS,
            font=(self.font_family, 23, "bold"),
        )
        style.configure(
            "StatusFail.TLabel",
            background=self.PANEL,
            foreground=self.FAIL,
            font=(self.font_family, 23, "bold"),
        )
        style.configure(
            "TopStatus.TLabel",
            background=self.PANEL_2,
            foreground=self.ACCENT,
            padding=(12, 6),
            font=(self.font_family, 13, "bold"),
        )
        style.configure(
            "Clock.TLabel",
            background=self.BG,
            foreground=self.MUTED,
            font=(self.font_family, 14),
        )

        style.configure(
            "TEntry",
            fieldbackground=self.PANEL_3,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            insertcolor=self.TEXT,
            padding=(11, 10),
            font=(self.font_family, 14),
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.PANEL_3,
            background=self.PANEL_3,
            foreground=self.TEXT,
            arrowcolor=self.ACCENT,
            bordercolor=self.BORDER,
            padding=(10, 9),
            font=(self.font_family, 14),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.PANEL_3)],
            foreground=[("readonly", self.TEXT)],
            selectbackground=[("readonly", self.PANEL_3)],
            selectforeground=[("readonly", self.TEXT)],
        )
        style.configure(
            "Primary.TButton",
            background=self.ACCENT,
            foreground="#FFFFFF",
            bordercolor=self.ACCENT,
            padding=(20, 13),
            font=(self.font_family, 13, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#0072B2"), ("pressed", self.ACCENT_DARK)],
            foreground=[("disabled", "#8A8F96")],
        )
        style.configure(
            "Secondary.TButton",
            background=self.PANEL_3,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            padding=(16, 11),
            font=(self.font_family, 13, "bold"),
        )
        style.map("Secondary.TButton", background=[("active", "#E7EBEF")])

        style.configure(
            "TCheckbutton",
            background=self.PANEL,
            foreground=self.TEXT,
            font=(self.font_family, 14),
        )
        style.map(
            "TCheckbutton",
            background=[("active", self.PANEL)],
            foreground=[("active", self.TEXT)],
        )

        style.configure(
            "Treeview",
            background=self.PANEL,
            fieldbackground=self.PANEL,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            rowheight=38,
            font=(self.font_family, 14),
        )
        style.configure(
            "Treeview.Heading",
            background=self.PANEL_3,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            relief="flat",
            font=(self.font_family, 13, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", "#DCEAF4")],
            foreground=[("selected", self.TEXT)],
        )

        style.configure(
            "Vertical.TScrollbar",
            background=self.PANEL_3,
            troughcolor=self.PANEL,
            bordercolor=self.PANEL,
            arrowcolor=self.MUTED,
        )

    def _build_shell(self) -> None:
        """Create the persistent sidebar, top bar, and page container."""

        shell = ttk.Frame(self, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_columnconfigure(1, weight=1)

        self.sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=220)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.grid_propagate(False)
        tk.Frame(shell, bg=self.BORDER, width=1).grid(row=0, column=0, sticky="nse")

        main = ttk.Frame(shell, style="App.TFrame")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        self._build_sidebar()
        self._build_topbar(main)

        self.page_host = ttk.Frame(main, style="Page.TFrame")
        self.page_host.grid(row=1, column=0, sticky="nsew", padx=30, pady=(20, 26))
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)

    def _build_sidebar(self) -> None:
        """Create the left navigation rail used across all workspaces."""

        brand = tk.Frame(self.sidebar, bg=self.SIDEBAR)
        brand.pack(fill="x", padx=20, pady=(26, 16))
        tk.Label(
            brand,
            text="WearTest",
            bg=self.SIDEBAR,
            fg=self.TEXT,
            font=(self.font_family, 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="Manufacturing Test Platform",
            bg=self.SIDEBAR,
            fg=self.MUTED,
            font=(self.font_family, 11),
        ).pack(anchor="w", pady=(3, 0))

        tk.Frame(self.sidebar, bg=self.BORDER, height=1).pack(fill="x", padx=16, pady=(0, 18))
        tk.Label(
            self.sidebar,
            text="WORKSPACES",
            bg=self.SIDEBAR,
            fg=self.MUTED,
            font=(self.font_family, 10, "bold"),
        ).pack(anchor="w", padx=20, pady=(0, 8))

        self.nav_items: dict[str, tuple[tk.Frame, tk.Frame, tk.Button]] = {}
        navigation = (
            ("station", "Run Device Test"),
            ("external", "External Data"),
            ("history", "Production"),
            ("tester", "Tester Health"),
            ("grr", "Measurement System"),
            ("cycle", "Cycle Time"),
            ("integrity", "Data Integrity"),
            ("limits", "Test Limits"),
            ("simulation", "Simulation"),
        )
        for key, label in navigation:
            row = tk.Frame(self.sidebar, bg=self.SIDEBAR, height=48)
            row.pack(fill="x", padx=10, pady=2)
            row.pack_propagate(False)

            indicator = tk.Frame(row, bg=self.SIDEBAR, width=4)
            indicator.pack(side="left", fill="y")

            button = tk.Button(
                row,
                text=label,
                command=lambda page=key: self._show_page(page),
                anchor="w",
                bd=0,
                relief="flat",
                highlightthickness=0,
                padx=16,
                pady=10,
                bg=self.SIDEBAR,
                fg=self.TEXT,
                activebackground=self.ACTIVE_NAV,
                activeforeground=self.TEXT,
                font=(self.font_family, 13, "bold"),
                cursor="hand2",
            )
            button.pack(side="left", fill="both", expand=True)
            self.nav_items[key] = (row, indicator, button)

        footer = tk.Frame(self.sidebar, bg=self.SIDEBAR)
        footer.pack(side="bottom", fill="x", padx=20, pady=(0, 20))
        tk.Frame(footer, bg=self.BORDER, height=1).pack(fill="x", pady=(0, 12))
        tk.Label(
            footer,
            text="TEST PLATFORM",
            bg=self.SIDEBAR,
            fg=self.MUTED,
            font=(self.font_family, 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            footer,
            text="Configurable limits",
            bg=self.SIDEBAR,
            fg=self.TEXT,
            font=(self.font_family, 10),
        ).pack(anchor="w", pady=(7, 0))
        tk.Label(
            footer,
            text="Local traceability enabled",
            bg=self.SIDEBAR,
            fg=self.MUTED,
            font=(self.font_family, 10),
        ).pack(anchor="w", pady=(3, 0))

    def _build_topbar(self, parent: ttk.Frame) -> None:
        """Create the compact application header.

        Args:
            parent: Main application frame that owns the header.
        """

        top = tk.Frame(parent, bg=self.PANEL, padx=30, pady=14)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)

        title_box = tk.Frame(top, bg=self.PANEL)
        title_box.grid(row=0, column=0, sticky="w")
        tk.Label(
            title_box,
            text="WearTest ATE",
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 21, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="Automated wearable manufacturing test and traceability",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 11),
        ).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(top, bg=self.PANEL)
        right.grid(row=0, column=1, sticky="e")

        status_box = tk.Frame(right, bg=self.PASS_BG, padx=12, pady=7)
        status_box.pack(side="left", padx=(0, 18))
        status_dot = tk.Canvas(
            status_box, width=10, height=10, bg=self.PASS_BG, highlightthickness=0
        )
        status_dot.create_oval(2, 2, 8, 8, fill=self.PASS, outline=self.PASS)
        status_dot.pack(side="left", padx=(0, 7))
        tk.Label(
            status_box,
            text="Ready",
            bg=self.PASS_BG,
            fg=self.PASS,
            font=(self.font_family, 12, "bold"),
        ).pack(side="left")

        time_box = tk.Frame(right, bg=self.PANEL)
        time_box.pack(side="left")
        tk.Label(
            time_box,
            text="Local time",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 10, "bold"),
        ).pack(anchor="e")
        self.clock_var = tk.StringVar(value="")
        tk.Label(
            time_box,
            textvariable=self.clock_var,
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 12),
        ).pack(anchor="e", pady=(2, 0))

        tk.Frame(parent, bg=self.BORDER, height=1).grid(row=0, column=0, sticky="sew")

    def _build_pages(self) -> None:
        """Create every workspace once and stack them in the main content area."""

        self.pages: dict[str, ttk.Frame] = {}
        for key in (
            "station", "external", "history", "tester", "grr", "cycle",
            "integrity", "limits", "simulation"
        ):
            frame = ttk.Frame(self.page_host, style="Page.TFrame")
            frame.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = frame

        self._build_station_page(self.pages["station"])
        self._build_external_page(self.pages["external"])
        self._build_history_page(self.pages["history"])
        self._build_tester_health_page(self.pages["tester"])
        self._build_grr_page(self.pages["grr"])
        self._build_cycle_time_page(self.pages["cycle"])
        self._build_integrity_page(self.pages["integrity"])
        self._build_limits_page(self.pages["limits"])
        self._build_simulation_page(self.pages["simulation"])

    def _show_page(self, page_name: str) -> None:
        """Bring one workspace to the front and update sidebar emphasis.

        Args:
            page_name: Key of the page to display.
        """

        self.current_page = page_name
        self.pages[page_name].tkraise()
        if page_name == "history" and hasattr(self, "history_tree"):
            self._refresh_history()
        if page_name == "tester" and hasattr(self, "tester_history_tree"):
            self._refresh_tester_health()
        if page_name == "grr" and hasattr(self, "grr_history_tree"):
            self._refresh_grr_history()
        if page_name == "cycle" and hasattr(self, "cycle_history_tree"):
            self._refresh_cycle_time_history()
        for key, (row, indicator, button) in self.nav_items.items():
            active = key == page_name
            background = self.ACTIVE_NAV if active else self.SIDEBAR
            row.configure(bg=background)
            indicator.configure(bg=self.ACCENT if active else self.SIDEBAR)
            button.configure(
                bg=background,
                fg=self.TEXT,
                activebackground=background,
            )

    @staticmethod
    def _is_descendant(widget: tk.Misc, ancestor: tk.Misc) -> bool:
        """Return whether a Tk widget lives inside another widget.

        Args:
            widget: Widget that received an event.
            ancestor: Container that should own the widget.

        Returns:
            True when ``widget`` is ``ancestor`` or one of its descendants.
        """

        current = widget
        while current is not None:
            if current == ancestor:
                return True
            current = getattr(current, "master", None)
        return False

    @staticmethod
    def _inside_treeview(widget: tk.Misc) -> bool:
        """Return whether an event originated from a Treeview or its child.

        Args:
            widget: Widget that received the mouse-wheel event.

        Returns:
            True when a Treeview should keep the wheel event for its own scrolling.
        """

        current = widget
        while current is not None:
            if isinstance(current, ttk.Treeview):
                return True
            current = getattr(current, "master", None)
        return False

    def _route_mousewheel(self, event: tk.Event) -> str | None:
        """Scroll the Production dashboard from any non-table child widget.

        Args:
            event: Tk mouse-wheel event from Windows, macOS, or Linux.

        Returns:
            ``"break"`` when the Production canvas handled the event, otherwise None.
        """

        scroll_canvases = {
            "history": getattr(self, "production_canvas", None),
            "tester": getattr(self, "tester_canvas", None),
            "grr": getattr(self, "grr_canvas", None),
            "cycle": getattr(self, "cycle_canvas", None),
            "limits": getattr(self, "limits_canvas", None),
        }
        canvas = scroll_canvases.get(self.current_page)
        if canvas is None:
            return None
        if not self._is_descendant(event.widget, canvas):
            return None
        if self.current_page in {"history", "tester", "grr", "cycle"} and self._inside_treeview(event.widget):
            return None

        # Windows/macOS normally report ``delta``. Linux/X11 reports Button-4/5.
        button_number = getattr(event, "num", None)
        if button_number == 4:
            units = -3
        elif button_number == 5:
            units = 3
        else:
            delta = getattr(event, "delta", 0)
            if not delta:
                return None
            units = -3 if delta > 0 else 3

        canvas.yview_scroll(units, "units")
        return "break"

    def _page_heading(self, parent: ttk.Frame, title: str, description: str) -> None:
        """Add a clean heading to a workspace.

        Args:
            parent: Page that receives the heading.
            title: Main heading shown to the operator.
            description: Short plain-language explanation of the page.
        """

        heading = tk.Frame(parent, bg=self.BG)
        heading.pack(fill="x", pady=(0, 18))
        tk.Label(
            heading,
            text=title,
            bg=self.BG,
            fg=self.TEXT,
            font=(self.font_family, 27, "bold"),
        ).pack(anchor="w")
        tk.Label(
            heading,
            text=description,
            bg=self.BG,
            fg=self.MUTED,
            font=(self.font_family, 13),
            wraplength=1050,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _panel(self, parent: ttk.Frame, *, padding: int = 22) -> tk.Frame:
        """Return a bordered light card used for grouped controls and results.

        Args:
            parent: Page or card that will contain the panel.
            padding: Internal space in pixels.

        Returns:
            Tk frame with consistent panel colors and border.
        """

        outer = tk.Frame(parent, bg=self.BORDER, bd=0)
        inner = tk.Frame(outer, bg=self.PANEL, padx=padding, pady=padding)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return inner

    def _build_station_page(self, parent: ttk.Frame) -> None:
        """Build the primary end-of-line device test workspace.

        Args:
            parent: Page frame receiving the station controls.
        """

        self._page_heading(
            parent,
            "Run Device Test",
            "Run the end-of-line acceptance sequence and review the measured result for each check.",
        )

        setup_outer = tk.Frame(parent, bg=self.BORDER)
        setup_outer.pack(fill="x", pady=(0, 14))
        setup = tk.Frame(setup_outer, bg=self.PANEL, padx=22, pady=18)
        setup.pack(fill="x", padx=1, pady=1)
        for column in range(3):
            setup.grid_columnconfigure(column, weight=1, uniform="station_fields")

        tk.Label(
            setup,
            text="Test setup",
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 16, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        tk.Label(
            setup,
            text="Enter the device information used for traceability, then start the automated sequence.",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 12),
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 15))

        self.device_id_var = tk.StringVar(value="DUT-00001")
        self.station_id_var = tk.StringVar(value="ATE-01")
        product_names = tuple(profile.name for profile in self.product_profiles)
        default_product = "Wearable + ECG" if "Wearable + ECG" in product_names else product_names[0]
        self.variant_var = tk.StringVar(value=default_product)

        self._field_label(setup, "Device ID", 2, 0)
        ttk.Entry(setup, textvariable=self.device_id_var).grid(
            row=3, column=0, sticky="ew", padx=(0, 14), pady=(5, 0)
        )

        self._field_label(setup, "Test station", 2, 1)
        ttk.Entry(setup, textvariable=self.station_id_var).grid(
            row=3, column=1, sticky="ew", padx=(0, 14), pady=(5, 0)
        )

        self._field_label(setup, "Product configuration", 2, 2)
        ttk.Combobox(
            setup,
            textvariable=self.variant_var,
            values=product_names,
            state="readonly",
        ).grid(row=3, column=2, sticky="ew", pady=(5, 0))

        action_row = tk.Frame(setup, bg=self.PANEL)
        action_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        action_row.grid_columnconfigure(0, weight=1)
        tk.Label(
            action_row,
            text="Acquire  |  Validate  |  Evaluate  |  Record",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 11),
        ).grid(row=0, column=0, sticky="w")
        self.run_test_button = ttk.Button(
            action_row,
            text="Run selected product",
            style="Primary.TButton",
            command=self._run_test,
        )
        self.run_test_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.run_all_products_button = ttk.Button(
            action_row,
            text="Run all product profiles",
            style="Secondary.TButton",
            command=self._run_all_product_profiles,
        )
        self.run_all_products_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.cancel_product_batch_button = ttk.Button(
            action_row,
            text="Cancel",
            style="Secondary.TButton",
            command=self._cancel_product_batch,
            state="disabled",
        )
        self.cancel_product_batch_button.grid(row=0, column=3, sticky="e", padx=(8, 0))

        self.product_batch_progress = ttk.Progressbar(
            setup, mode="determinate", maximum=max(1, len(self.product_profiles))
        )
        self.product_batch_progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 0), padx=(0, 14))
        self.product_batch_status_var = tk.StringVar(
            value="Run one selected product, or use the Device ID as a prefix to simulate every configured product profile."
        )
        tk.Label(
            setup, textvariable=self.product_batch_status_var, bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 10), anchor="w", justify="left",
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.overall_var = tk.StringVar()
        self.overall_detail_var = tk.StringVar()
        self.overall_label, self.result_tree = self._build_result_workspace(
            parent, self.overall_var, self.overall_detail_var
        )

    def _build_external_page(self, parent: ttk.Frame) -> None:
        """Build the workflow for testing measurements from another acquisition system.

        Args:
            parent: Page frame receiving external-acquisition controls.
        """

        self._page_heading(
            parent,
            "External Acquisition",
            "Import measurements collected by LabVIEW or another acquisition system. WearTest normalizes the source before applying the same acceptance engine used for device tests.",
        )

        outer = tk.Frame(parent, bg=self.BORDER)
        outer.pack(fill="x", pady=(0, 16))
        form = tk.Frame(outer, bg=self.PANEL, padx=22, pady=21)
        form.pack(fill="x", padx=1, pady=1)
        form.grid_columnconfigure(0, weight=1)
        form.grid_columnconfigure(1, weight=1)

        self.external_source_var = tk.StringVar()
        self.external_mapping_var = tk.StringVar()
        self.external_device_var = tk.StringVar(value="DUT-IMPORT-001")
        self.external_station_var = tk.StringVar(value="ATE-IMPORT")
        product_names = tuple(profile.name for profile in self.product_profiles)
        default_product = "Wearable + ECG" if "Wearable + ECG" in product_names else product_names[0]
        self.external_variant_var = tk.StringVar(value=default_product)

        available = self.importer.available_formats()
        format_values = ["Auto-detect"] + [f"{name} | {label}" for name, label in available.items()]
        self.external_format_var = tk.StringVar(value="Auto-detect")

        self._field_label(form, "ACQUISITION FILE", 0, 0)
        source_line = tk.Frame(form, bg=self.PANEL)
        source_line.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 12))
        source_line.grid_columnconfigure(0, weight=1)
        ttk.Entry(source_line, textvariable=self.external_source_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(source_line, text="Browse", style="Secondary.TButton", command=self._browse_external_source).grid(row=0, column=1)

        left = tk.Frame(form, bg=self.PANEL)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 14))
        right = tk.Frame(form, bg=self.PANEL)
        right.grid(row=2, column=1, sticky="nsew")

        self._field_label(left, "SOURCE FORMAT", 0, 0)
        ttk.Combobox(left, textvariable=self.external_format_var, values=format_values, state="readonly", width=34).grid(row=1, column=0, sticky="ew", pady=(5, 11))

        self._field_label(left, "DEVICE ID", 2, 0)
        ttk.Entry(left, textvariable=self.external_device_var).grid(row=3, column=0, sticky="ew", pady=(5, 11))

        self._field_label(left, "TEST STATION", 4, 0)
        ttk.Entry(left, textvariable=self.external_station_var).grid(row=5, column=0, sticky="ew", pady=(5, 0))
        left.grid_columnconfigure(0, weight=1)

        self._field_label(right, "CHANNEL MAPPING", 0, 0)
        mapping_line = tk.Frame(right, bg=self.PANEL)
        mapping_line.grid(row=1, column=0, sticky="ew", pady=(5, 11))
        mapping_line.grid_columnconfigure(0, weight=1)
        ttk.Entry(mapping_line, textvariable=self.external_mapping_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(mapping_line, text="Browse", style="Secondary.TButton", command=self._browse_mapping).grid(row=0, column=1)

        self._field_label(right, "PRODUCT CONFIGURATION", 2, 0)
        ttk.Combobox(
            right,
            textvariable=self.external_variant_var,
            values=product_names,
            state="readonly",
        ).grid(row=3, column=0, sticky="ew", pady=(5, 11))

        ttk.Button(
            right,
            text="Import and run test",
            style="Primary.TButton",
            command=self._run_external_test,
        ).grid(row=5, column=0, sticky="e", pady=(5, 0))
        right.grid_columnconfigure(0, weight=1)

        self.external_overall_var = tk.StringVar(value="Ready for external acquisition")
        self.external_detail_var = tk.StringVar(value="Choose an acquisition file and channel mapping, then run the test.")
        self.external_overall_label, self.external_result_tree = self._build_result_workspace(
            parent, self.external_overall_var, self.external_detail_var
        )

    def _field_label(self, parent: tk.Misc, text: str, row: int, column: int) -> None:
        """Place a small uppercase field label in a form.

        Args:
            parent: Form receiving the label.
            text: Label text.
            row: Grid row.
            column: Grid column.
        """

        tk.Label(
            parent,
            text=text,
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 12, "bold"),
        ).grid(row=row, column=column, sticky="w")

    def _build_result_workspace(
        self,
        parent: ttk.Frame,
        status_var: tk.StringVar,
        detail_var: tk.StringVar,
    ) -> tuple[ttk.Label, ttk.Treeview]:
        """Create the result status card and step-level result table.

        Args:
            parent: Page that owns the result workspace.
            status_var: Variable containing the primary result message.
            detail_var: Variable containing supporting operator text.

        Returns:
            Status label and result table so the caller can update them.
        """

        summary_outer = tk.Frame(parent, bg=self.BORDER)
        summary_outer.pack(fill="x", pady=(0, 14))
        summary = tk.Frame(summary_outer, bg=self.PANEL, padx=20, pady=14)
        summary.pack(fill="x", padx=1, pady=1)
        summary.grid_columnconfigure(1, weight=1)

        status_bar = tk.Frame(summary, bg=self.ACCENT, width=5)
        status_bar.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(0, 14))
        tk.Label(
            summary,
            text="Current device status",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 11, "bold"),
        ).grid(row=0, column=1, sticky="w")
        label = ttk.Label(summary, textvariable=status_var, style="StatusReady.TLabel")
        label.grid(row=1, column=1, sticky="w", pady=(3, 1))
        ttk.Label(
            summary,
            textvariable=detail_var,
            style="Muted.TLabel",
            wraplength=900,
        ).grid(row=2, column=1, sticky="w")

        table_outer = tk.Frame(parent, bg=self.BORDER)
        table_outer.pack(fill="x")
        table_frame = tk.Frame(table_outer, bg=self.PANEL, padx=14, pady=14)
        table_frame.pack(fill="x", padx=1, pady=1)
        table_frame.grid_columnconfigure(0, weight=1)

        title_row = tk.Frame(table_frame, bg=self.PANEL)
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        title_row.grid_columnconfigure(0, weight=1)
        tk.Label(
            title_row,
            text="Acceptance checks",
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            title_row,
            text="Measured values are compared with the active test limits.",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 11),
        ).grid(row=0, column=1, sticky="e")

        columns = ("test", "result", "measured", "spec")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=8)
        headings = {
            "test": "Check",
            "result": "Result",
            "measured": "Measured result",
            "spec": "Acceptance limit",
        }
        widths = {"test": 210, "result": 105, "measured": 330, "spec": 410}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="w")
        tree.tag_configure("pass", foreground=self.PASS)
        tree.tag_configure("fail", foreground=self.FAIL)
        tree.grid(row=1, column=0, sticky="ew")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        return label, tree

    def _build_history_page(self, parent: ttk.Frame) -> None:
        """Build a scrollable production dashboard from stored manufacturing results.

        Args:
            parent: Page frame receiving production analytics and traceability.
        """

        # The production page contains several charts and tables. A scrollable body
        # prevents those elements from being compressed into a fixed-height window.
        canvas = tk.Canvas(parent, bg=self.BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        self.production_canvas = canvas
        self.production_scrollbar = scrollbar
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = tk.Frame(canvas, bg=self.BG)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )
        self._page_heading(
            content,
            "Production Dashboard",
            "Review first-pass yield, recurring failures, station performance, and recent device history.",
        )

        toolbar = tk.Frame(content, bg=self.BG)
        toolbar.pack(fill="x", pady=(0, 14))
        tk.Label(
            toolbar,
            text="FPY uses each device's first recorded test. Retests remain available for traceability.",
            bg=self.BG,
            fg=self.MUTED,
            font=(self.font_family, 11),
        ).pack(side="left")
        refresh_area = tk.Frame(toolbar, bg=self.BG)
        refresh_area.pack(side="right")
        self.dashboard_refresh_var = tk.StringVar(value="")
        tk.Label(
            refresh_area,
            textvariable=self.dashboard_refresh_var,
            bg=self.BG,
            fg=self.MUTED,
            font=(self.font_family, 9),
        ).pack(anchor="e", pady=(0, 3))
        self.refresh_dashboard_button = ttk.Button(
            refresh_area,
            text="Refresh dashboard",
            style="Secondary.TButton",
            command=self._manual_refresh_dashboard,
        )
        self.refresh_dashboard_button.pack(anchor="e")

        metrics = tk.Frame(content, bg=self.BG)
        metrics.pack(fill="x", pady=(0, 14))
        self.summary_vars = {
            "total": tk.StringVar(value="0"),
            "fpy": tk.StringVar(value="0.0%"),
            "failed": tk.StringVar(value="0"),
            "sessions": tk.StringVar(value="0"),
        }
        cards = (
            ("total", "Devices tested", "Unique device IDs"),
            ("fpy", "First-pass yield", "Passed on first attempt"),
            ("failed", "First-pass failures", "Needs investigation"),
            ("sessions", "Test sessions", "Includes retests"),
        )
        for index, (key, label, note) in enumerate(cards):
            outer = tk.Frame(metrics, bg=self.BORDER)
            outer.pack(
                side="left",
                fill="x",
                expand=True,
                padx=(0, 10 if index < len(cards) - 1 else 0),
            )
            card = tk.Frame(outer, bg=self.PANEL, padx=18, pady=14)
            card.pack(fill="both", expand=True, padx=1, pady=1)
            tk.Label(
                card, text=label, bg=self.PANEL, fg=self.TEXT,
                font=(self.font_family, 11, "bold"),
            ).pack(anchor="w")
            tk.Label(
                card, textvariable=self.summary_vars[key], bg=self.PANEL, fg=self.TEXT,
                font=(self.font_family, 23, "bold"),
            ).pack(anchor="w", pady=(3, 0))
            tk.Label(
                card, text=note, bg=self.PANEL, fg=self.MUTED,
                font=(self.font_family, 10),
            ).pack(anchor="w", pady=(2, 0))

        health_outer = tk.Frame(content, bg=self.BORDER)
        health_outer.pack(fill="x", pady=(0, 14))
        health_card = tk.Frame(health_outer, bg=self.PANEL, padx=14, pady=13)
        health_card.pack(fill="x", padx=1, pady=1)
        health_card.grid_columnconfigure(0, weight=1)
        tk.Label(
            health_card, text="Tester health", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            health_card,
            text="Latest golden-unit verification for each station. A production result is only as trustworthy as the station measuring it.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10),
        ).grid(row=1, column=0, sticky="w", pady=(2, 9))
        tester_columns = ("station", "health", "checks", "latest", "worst", "usage")
        self.tester_health_tree = ttk.Treeview(
            health_card, columns=tester_columns, show="headings", height=4
        )
        tester_headings = (
            ("station", "Station", 100), ("health", "Health", 135),
            ("checks", "Checks", 75), ("latest", "Last check", 165),
            ("worst", "Largest bias", 240), ("usage", "Limit used", 95),
        )
        for column, label, width in tester_headings:
            self.tester_health_tree.heading(column, text=label)
            self.tester_health_tree.column(
                column, width=width, minwidth=70, anchor="w", stretch=True
            )
        self.tester_health_tree.tag_configure("healthy", foreground=self.PASS)
        self.tester_health_tree.tag_configure("warning", foreground=self.WARN)
        self.tester_health_tree.tag_configure("attention", foreground=self.FAIL)
        self.tester_health_tree.grid(row=2, column=0, sticky="ew")

        # Give each chart its own full-width row. This avoids compressed axes,
        # clipped labels, and legends covering the data on smaller displays.
        chart_row = tk.Frame(content, bg=self.BG)
        chart_row.pack(fill="x", pady=(0, 14))
        failure_card, self.failure_ax, self.failure_canvas = self._build_chart_card(
            chart_row,
            title="Failure Pareto",
            description="Most frequent failed acceptance checks. The line shows cumulative contribution.",
            figure_height=2.65,
        )
        failure_card.pack(fill="x")
        self.failure_percent_ax = self.failure_ax.twinx()

        yield_row = tk.Frame(content, bg=self.BG)
        yield_row.pack(fill="x", pady=(0, 14))
        yield_card, self.yield_ax, self.yield_canvas = self._build_chart_card(
            yield_row,
            title="First-Pass Yield Trend",
            description="20-device moving FPY for the most recent first-attempt devices.",
            figure_height=2.65,
        )
        yield_card.pack(fill="x")

        tables = tk.Frame(content, bg=self.BG)
        tables.pack(fill="x", pady=(0, 18))
        tables.grid_columnconfigure(0, weight=2, uniform="production_tables")
        tables.grid_columnconfigure(1, weight=3, uniform="production_tables")

        station_outer = tk.Frame(tables, bg=self.BORDER)
        station_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        station_card = tk.Frame(station_outer, bg=self.PANEL, padx=14, pady=13)
        station_card.pack(fill="both", expand=True, padx=1, pady=1)
        station_card.grid_rowconfigure(2, weight=1)
        station_card.grid_columnconfigure(0, weight=1)
        tk.Label(
            station_card, text="Station performance", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            station_card,
            text="First-attempt results grouped by test station.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10),
        ).grid(row=1, column=0, sticky="w", pady=(2, 9))
        station_columns = ("station", "tested", "passed", "failed", "fpy")
        self.station_tree = ttk.Treeview(
            station_card, columns=station_columns, show="headings", height=6
        )
        station_labels = ("Station", "Tested", "Pass", "Fail", "FPY")
        station_widths = (110, 78, 70, 70, 82)
        for column, label, width in zip(station_columns, station_labels, station_widths):
            self.station_tree.heading(column, text=label)
            self.station_tree.column(column, width=width, minwidth=60, anchor="center", stretch=True)
        self.station_tree.grid(row=2, column=0, sticky="nsew")

        history_outer = tk.Frame(tables, bg=self.BORDER)
        history_outer.grid(row=0, column=1, sticky="nsew")
        history_card = tk.Frame(history_outer, bg=self.PANEL, padx=14, pady=13)
        history_card.pack(fill="both", expand=True, padx=1, pady=1)
        history_card.grid_rowconfigure(2, weight=1)
        history_card.grid_columnconfigure(0, weight=1)
        tk.Label(
            history_card, text="Recent test sessions", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            history_card, text="Latest production and retest activity.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10),
        ).grid(row=1, column=0, sticky="w", pady=(2, 9))
        columns = ("time", "device", "station", "result")
        self.history_tree = ttk.Treeview(
            history_card, columns=columns, show="headings", height=6
        )
        labels = ("Local time", "Device", "Station", "Result")
        widths = (165, 250, 95, 80)
        for column, label, width in zip(columns, labels, widths):
            self.history_tree.heading(column, text=label)
            self.history_tree.column(
                column, width=width, minwidth=70, anchor="w", stretch=True
            )
        self.history_tree.tag_configure("pass", foreground=self.TEXT)
        self.history_tree.tag_configure("fail", foreground=self.FAIL)
        self.history_tree.grid(row=2, column=0, sticky="nsew")
        history_scroll = ttk.Scrollbar(
            history_card, orient="vertical", command=self.history_tree.yview
        )
        history_scroll.grid(row=2, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=history_scroll.set)

    def _build_chart_card(
        self,
        parent: tk.Misc,
        *,
        title: str,
        description: str,
        figure_height: float = 2.65,
    ) -> tuple[tk.Frame, object, FigureCanvasTkAgg]:
        """Create one embedded Matplotlib card for production analytics.

        Args:
            parent: Container receiving the chart card.
            title: Short chart title shown above the plot.
            description: Plain-language explanation of the chart.
            figure_height: Matplotlib figure height in inches before Tk resizing.

        Returns:
            Outer card frame, Matplotlib axis, and Tk canvas used to refresh the chart.
        """

        outer = tk.Frame(parent, bg=self.BORDER)
        card = tk.Frame(outer, bg=self.PANEL, padx=15, pady=12)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(
            card, text=title, bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            card, text=description, bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 10),
        ).pack(anchor="w", pady=(2, 7))

        figure = Figure(figsize=(9.2, figure_height), dpi=100, facecolor=self.PANEL)
        axis = figure.add_subplot(111)
        canvas = FigureCanvasTkAgg(figure, master=card)
        canvas.get_tk_widget().configure(bg=self.PANEL, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        return outer, axis, canvas

    def _style_chart_axis(self, axis) -> None:
        """Apply the application typography and light visual treatment to a chart.

        Args:
            axis: Matplotlib axis to style.
        """

        axis.set_facecolor(self.PANEL)
        axis.tick_params(colors=self.TEXT, labelsize=9)
        for label in list(axis.get_xticklabels()) + list(axis.get_yticklabels()):
            label.set_fontfamily(self.font_family)
            label.set_color(self.TEXT)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.spines["left"].set_color("#6B7280")
        axis.spines["bottom"].set_color("#6B7280")
        axis.grid(axis="y", color="#D9DEE5", linewidth=0.8, alpha=0.9)

    def _refresh_production_charts(self) -> None:
        """Redraw failure Pareto and rolling-FPY charts from the database."""

        failures = self.db.failure_summary(limit=6)
        self.failure_ax.clear()
        self.failure_percent_ax.clear()
        self._style_chart_axis(self.failure_ax)
        if failures:
            labels = [row["step_name"] for row in failures]
            counts = [row["count"] for row in failures]
            positions = list(range(len(labels)))

            bars = self.failure_ax.bar(
                positions, counts, color=self.ACCENT, width=0.58, zorder=2
            )
            wrapped_labels = [label.replace(" response", "\nresponse") for label in labels]
            self.failure_ax.set_xticks(positions, wrapped_labels)
            self.failure_ax.tick_params(axis="x", pad=8)
            self.failure_ax.set_ylabel(
                "Failures", fontfamily=self.font_family, color=self.TEXT
            )
            self.failure_ax.set_ylim(0, max(counts) * 1.24 if counts else 1)
            for bar, count in zip(bars, counts):
                self.failure_ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + max(counts) * 0.035,
                    str(count),
                    ha="center",
                    va="bottom",
                    color=self.TEXT,
                    fontfamily=self.font_family,
                    fontsize=9,
                    fontweight="bold",
                )

            total_failures = sum(counts)
            cumulative = []
            running = 0
            for count in counts:
                running += count
                cumulative.append(100.0 * running / total_failures)
            self.failure_percent_ax.plot(
                positions, cumulative, color=self.FAIL, marker="o", linewidth=1.8,
                markersize=4, zorder=3
            )
            self.failure_percent_ax.set_ylim(0, 105)
            self.failure_percent_ax.set_ylabel("")
            self.failure_percent_ax.set_yticks([0, 25, 50, 75, 100])
            self.failure_percent_ax.text(
                0.985, 0.96, "Cumulative %", transform=self.failure_percent_ax.transAxes,
                ha="right", va="top", color=self.MUTED,
                fontfamily=self.font_family, fontsize=9,
            )
            self.failure_percent_ax.tick_params(colors=self.TEXT, labelsize=8)
            self.failure_percent_ax.spines["top"].set_visible(False)
            self.failure_percent_ax.spines["left"].set_visible(False)
            self.failure_percent_ax.spines["right"].set_color(self.BORDER)
            for label in self.failure_percent_ax.get_yticklabels():
                label.set_fontfamily(self.font_family)
                label.set_color(self.TEXT)
        else:
            self.failure_ax.text(
                0.5, 0.5, "No failures recorded yet", ha="center", va="center",
                transform=self.failure_ax.transAxes, color=self.MUTED,
                fontfamily=self.font_family, fontsize=12,
            )
            self.failure_ax.set_xticks([])
            self.failure_ax.set_yticks([])
            self.failure_percent_ax.set_yticks([])
            for spine in self.failure_percent_ax.spines.values():
                spine.set_visible(False)
        self.failure_ax.figure.subplots_adjust(left=0.075, right=0.93, bottom=0.23, top=0.94)
        self.failure_canvas.draw_idle()

        points = self.db.rolling_first_pass_yield(limit=60, window=20)
        self.yield_ax.clear()
        self._style_chart_axis(self.yield_ax)
        if points:
            y_values = [row["fpy_percent"] for row in points]
            x_values = list(range(1, len(y_values) + 1))
            overall_fpy = float(self.db.summary()["fpy_percent"])

            low_value = min(y_values + [overall_fpy])
            lower_bound = max(0.0, (int(max(0.0, low_value - 5.0) // 5) * 5))
            if lower_bound > 90.0:
                lower_bound = 90.0
            upper_bound = 101.0

            self.yield_ax.plot(
                x_values,
                y_values,
                color=self.ACCENT,
                marker="o",
                markerfacecolor=self.PANEL,
                markeredgecolor=self.ACCENT,
                markeredgewidth=1.2,
                linewidth=2.2,
                markersize=4.5,
                zorder=3,
            )
            self.yield_ax.axhline(
                overall_fpy, color=self.MUTED, linestyle="--", linewidth=1.5, zorder=2
            )
            self.yield_ax.set_ylim(lower_bound, upper_bound)
            self.yield_ax.set_xlim(1, max(2, len(x_values)))
            self.yield_ax.set_ylabel(
                "Rolling FPY (%)", fontfamily=self.font_family, color=self.TEXT
            )
            self.yield_ax.set_xlabel(
                "Most recent first-attempt devices",
                fontfamily=self.font_family,
                color=self.TEXT,
            )

            # Keep labels inside the axes instead of using a legend that can
            # cover the trend line on smaller windows.
            self.yield_ax.text(
                0.015,
                0.08,
                f"Overall FPY: {overall_fpy:.1f}%",
                transform=self.yield_ax.transAxes,
                ha="left",
                va="bottom",
                color=self.MUTED,
                fontfamily=self.font_family,
                fontsize=9,
            )
            self.yield_ax.text(
                0.985,
                0.91,
                f"Latest: {y_values[-1]:.1f}%",
                transform=self.yield_ax.transAxes,
                ha="right",
                va="top",
                color=self.TEXT,
                fontfamily=self.font_family,
                fontsize=10,
                fontweight="bold",
                bbox={
                    "boxstyle": "round,pad=0.25",
                    "facecolor": self.PANEL,
                    "edgecolor": self.BORDER,
                    "linewidth": 0.8,
                },
            )

            if max(y_values) - min(y_values) < 0.01:
                self.yield_ax.text(
                    0.985,
                    0.78,
                    "No variation in this window",
                    transform=self.yield_ax.transAxes,
                    ha="right",
                    va="top",
                    color=self.MUTED,
                    fontfamily=self.font_family,
                    fontsize=9,
                )
        else:
            self.yield_ax.text(
                0.5,
                0.5,
                "At least 20 first-attempt devices are needed\nfor a 20-device FPY trend.",
                ha="center",
                va="center",
                transform=self.yield_ax.transAxes,
                color=self.MUTED,
                fontfamily=self.font_family,
                fontsize=12,
            )
            self.yield_ax.set_xticks([])
            self.yield_ax.set_yticks([])
        self.yield_ax.figure.subplots_adjust(left=0.075, right=0.985, bottom=0.22, top=0.94)
        self.yield_canvas.draw_idle()

    def _build_tester_health_page(self, parent: ttk.Frame) -> None:
        """Build the golden-unit workspace used to verify test-station health.

        Args:
            parent: Page frame receiving golden-unit controls and results.
        """

        canvas = tk.Canvas(parent, bg=self.BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        self.tester_canvas = canvas
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = tk.Frame(canvas, bg=self.BG)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )

        self._page_heading(
            content,
            "Tester Health",
            "Run a known-good reference wearable to check whether the test station is measuring accurately before trusting production decisions.",
        )

        notice = tk.Frame(content, bg="#EAF2F8", padx=16, pady=11)
        notice.pack(fill="x", pady=(0, 14))
        tk.Label(
            notice, text="Golden unit", bg="#EAF2F8", fg=self.ACCENT_DARK,
            font=(self.font_family, 11, "bold"),
        ).pack(side="left")
        tk.Label(
            notice,
            text=(
                "This workflow evaluates the tester, not a production device. The reference values, "
                "bias limits, and station drift are portfolio simulation assumptions."
            ),
            bg="#EAF2F8", fg=self.TEXT, font=(self.font_family, 11),
        ).pack(side="left", padx=(12, 0))

        setup_outer = tk.Frame(content, bg=self.BORDER)
        setup_outer.pack(fill="x", pady=(0, 14))
        setup = tk.Frame(setup_outer, bg=self.PANEL, padx=18, pady=16)
        setup.pack(fill="x", padx=1, pady=1)
        for column in range(4):
            setup.grid_columnconfigure(column, weight=1 if column < 3 else 0)

        tk.Label(
            setup, text="Golden-unit check setup", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        tk.Label(
            setup,
            text="The same standardized records, integrity checks, redundant storage, and traceability path used by production tests are used here.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 11),
            wraplength=1000, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 13))

        tk.Label(
            setup, text="Reference device", bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 11, "bold"),
        ).grid(row=2, column=0, sticky="w")
        self.golden_device_var = tk.StringVar(value=self.golden_config.device_id)
        tk.Label(
            setup, textvariable=self.golden_device_var, bg=self.PANEL_3, fg=self.TEXT,
            font=(self.font_family, 13, "bold"), padx=11, pady=10, anchor="w",
        ).grid(row=3, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))

        tk.Label(
            setup, text="Test station", bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 11, "bold"),
        ).grid(row=2, column=1, sticky="w")
        station_values = tuple(sorted(self.golden_config.station_drift))
        self.golden_station_var = tk.StringVar(value=station_values[0])
        self.golden_station_combo = ttk.Combobox(
            setup, textvariable=self.golden_station_var, values=station_values,
            state="readonly",
        )
        self.golden_station_combo.grid(row=3, column=1, sticky="ew", padx=(0, 12), pady=(5, 0))
        self.golden_station_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._update_next_golden_check_label()
        )

        tk.Label(
            setup, text="Next station check", bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 11, "bold"),
        ).grid(row=2, column=2, sticky="w")
        self.golden_next_check_var = tk.StringVar(value="Check 1")
        tk.Label(
            setup, textvariable=self.golden_next_check_var, bg=self.PANEL_3, fg=self.TEXT,
            font=(self.font_family, 13, "bold"), padx=11, pady=10, anchor="w",
        ).grid(row=3, column=2, sticky="ew", padx=(0, 12), pady=(5, 0))

        action_box = tk.Frame(setup, bg=self.PANEL)
        action_box.grid(row=2, column=3, rowspan=2, sticky="se")
        ttk.Button(
            action_box, text="Reload reference file", style="Secondary.TButton",
            command=self._reload_golden_config,
        ).pack(anchor="e", pady=(0, 7))
        self.golden_run_one_button = ttk.Button(
            action_box, text="Run selected station", style="Primary.TButton",
            command=self._run_golden_check,
        )
        self.golden_run_one_button.pack(anchor="e", pady=(0, 7))
        self.golden_run_all_button = ttk.Button(
            action_box, text="Run all stations", style="Secondary.TButton",
            command=self._run_all_golden_checks,
        )
        self.golden_run_all_button.pack(anchor="e", pady=(0, 7))
        self.golden_cancel_button = ttk.Button(
            action_box, text="Cancel", style="Secondary.TButton",
            command=self._cancel_golden_batch, state="disabled",
        )
        self.golden_cancel_button.pack(anchor="e")
        self.golden_path_var = tk.StringVar(value=str(self.golden_config_path))
        tk.Label(
            setup, textvariable=self.golden_path_var, bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 9), anchor="w", justify="left", wraplength=1000,
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.golden_batch_progress = ttk.Progressbar(
            setup, mode="determinate", maximum=max(1, len(station_values))
        )
        self.golden_batch_progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(11, 0), padx=(0, 12))
        self.golden_batch_status_var = tk.StringVar(
            value="Run one selected station, or verify every configured station with one click."
        )
        tk.Label(
            setup, textvariable=self.golden_batch_status_var, bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 10), anchor="w", justify="left",
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))

        status_outer = tk.Frame(content, bg=self.BORDER)
        status_outer.pack(fill="x", pady=(0, 14))
        status = tk.Frame(status_outer, bg=self.PANEL, padx=18, pady=15)
        status.pack(fill="x", padx=1, pady=1)
        status.grid_columnconfigure(0, weight=1)
        self.tester_status_var = tk.StringVar(value="No golden-unit check has been run")
        self.tester_detail_var = tk.StringVar(
            value="Choose a station and run the known-good reference device to establish tester health."
        )
        self.tester_status_label = tk.Label(
            status, textvariable=self.tester_status_var, bg=self.PANEL, fg=self.ACCENT_DARK,
            font=(self.font_family, 20, "bold"), anchor="w",
        )
        self.tester_status_label.grid(row=0, column=0, sticky="w")
        tk.Label(
            status, textvariable=self.tester_detail_var, bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 11), anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.tester_check_meta_var = tk.StringVar(value="")
        tk.Label(
            status, textvariable=self.tester_check_meta_var, bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 11, "bold"), anchor="e",
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=(20, 0))

        results_outer = tk.Frame(content, bg=self.BORDER)
        results_outer.pack(fill="both", expand=True, pady=(0, 14))
        results = tk.Frame(results_outer, bg=self.PANEL, padx=14, pady=13)
        results.pack(fill="both", expand=True, padx=1, pady=1)
        results.grid_rowconfigure(2, weight=1)
        results.grid_columnconfigure(0, weight=1)
        tk.Label(
            results, text="Measured tester bias", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            results,
            text="Bias is measured minus the known golden-unit reference. Warning starts before the full action limit is reached.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10),
        ).grid(row=1, column=0, sticky="w", pady=(2, 9))
        columns = ("metric", "measured", "reference", "bias", "limit", "status")
        self.golden_result_tree = ttk.Treeview(
            results, columns=columns, show="headings", height=8,
        )
        headings = (
            ("metric", "Measurement", 210, "w"),
            ("measured", "Measured", 130, "center"),
            ("reference", "Reference", 130, "center"),
            ("bias", "Tester bias", 130, "center"),
            ("limit", "Allowed bias", 130, "center"),
            ("status", "Health", 130, "center"),
        )
        for column, label, width, anchor in headings:
            self.golden_result_tree.heading(column, text=label)
            self.golden_result_tree.column(column, width=width, minwidth=90, anchor=anchor, stretch=True)
        self.golden_result_tree.tag_configure("healthy", foreground=self.PASS)
        self.golden_result_tree.tag_configure("warning", foreground=self.WARN)
        self.golden_result_tree.tag_configure("attention", foreground=self.FAIL)
        self.golden_result_tree.grid(row=2, column=0, sticky="nsew")
        result_scroll = ttk.Scrollbar(results, orient="vertical", command=self.golden_result_tree.yview)
        result_scroll.grid(row=2, column=1, sticky="ns")
        self.golden_result_tree.configure(yscrollcommand=result_scroll.set)

        history_outer = tk.Frame(content, bg=self.BORDER)
        history_outer.pack(fill="x")
        history = tk.Frame(history_outer, bg=self.PANEL, padx=14, pady=13)
        history.pack(fill="x", padx=1, pady=1)
        history.grid_columnconfigure(0, weight=1)
        tk.Label(
            history, text="Recent tester-health checks", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            history, text="Every golden-unit check is retained in the same local traceability database.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10),
        ).grid(row=1, column=0, sticky="w", pady=(2, 9))
        columns = ("time", "station", "check", "status", "worst")
        self.tester_history_tree = ttk.Treeview(
            history, columns=columns, show="headings", height=5,
        )
        history_headings = (
            ("time", "Local time", 165), ("station", "Station", 100),
            ("check", "Check", 75), ("status", "Health", 130),
            ("worst", "Largest bias", 260),
        )
        for column, label, width in history_headings:
            self.tester_history_tree.heading(column, text=label)
            self.tester_history_tree.column(column, width=width, minwidth=70, anchor="w", stretch=True)
        self.tester_history_tree.tag_configure("healthy", foreground=self.PASS)
        self.tester_history_tree.tag_configure("warning", foreground=self.WARN)
        self.tester_history_tree.tag_configure("attention", foreground=self.FAIL)
        self.tester_history_tree.grid(row=2, column=0, sticky="ew")

        self._update_next_golden_check_label()

    def _update_next_golden_check_label(self) -> None:
        """Update the predicted golden-unit check number for the selected station."""

        if not hasattr(self, "golden_station_var"):
            return
        station_id = self.golden_station_var.get().strip()
        next_number = self.db.golden_check_count(station_id) + 1
        self.golden_next_check_var.set(f"Check {next_number}")

    def _reload_golden_config(self) -> None:
        """Reload golden-unit references and station drift without restarting WearTest."""

        try:
            self.golden_config = load_golden_unit_configuration(self.golden_config_path)
            self.golden_evaluator = GoldenUnitEvaluator(self.golden_config)
            self.golden_device_var.set(self.golden_config.device_id)
            stations = tuple(sorted(self.golden_config.station_drift))
            self.golden_station_combo.configure(values=stations)
            self.golden_batch_progress.configure(maximum=max(1, len(stations)), value=0)
            if self.golden_station_var.get() not in stations:
                self.golden_station_var.set(stations[0])
            self.golden_batch_status_var.set(
                "Reference file reloaded. Run one selected station or verify all configured stations."
            )
            self._update_next_golden_check_label()
            messagebox.showinfo(
                "Golden-unit reference reloaded",
                "The updated reference values, bias limits, and simulated station drift are now active.",
            )
        except Exception as exc:
            messagebox.showerror("Could not load golden-unit reference", str(exc))

    def _execute_golden_check(self, station_id: str) -> TesterHealthResult:
        """Run and persist one golden-unit verification for one station.

        Args:
            station_id: Configured test-station identifier to verify.

        Returns:
            Completed tester-health result for the station.
        """

        check_number = self.db.golden_check_count(station_id) + 1
        records = self.simulator.acquire_golden(
            self.golden_config, station_id=station_id, check_number=check_number
        )
        raw_path = self.records_dir / f"golden_{records[0].session_id}.jsonl"
        write_jsonl(raw_path, records)
        received = read_jsonl(raw_path, verify_checksum=True)
        primary_count, spooled_count = self.transport.send_many(received)
        result = self.golden_evaluator.evaluate(received, check_number=check_number)
        self.db.save_golden_check(result, raw_record_path=str(raw_path))
        self.last_tester_health = result
        self._update_integrity(
            received, raw_path, primary_count=primary_count, spooled_count=spooled_count
        )
        return result

    def _run_golden_check(self) -> None:
        """Run the known-good reference unit on the selected station."""

        if self._golden_batch_running:
            return
        station_id = self.golden_station_var.get().strip()
        self.golden_batch_status_var.set(f"Running golden-unit verification on {station_id}...")
        self.update_idletasks()
        try:
            result = self._execute_golden_check(station_id)
            self._show_tester_health_result(result)
            self.golden_batch_status_var.set(
                f"{station_id} complete: {result.status} | Check {result.check_number}"
            )
            self._refresh_tester_health()
            self._refresh_history()
            self._update_next_golden_check_label()
        except Exception as exc:
            self.golden_batch_status_var.set(f"Golden-unit check failed on {station_id}.")
            messagebox.showerror("Golden-unit check failed", str(exc))

    def _run_all_golden_checks(self) -> None:
        """Verify the golden reference across every configured test station."""

        if self._golden_batch_running:
            return
        stations = list(sorted(self.golden_config.station_drift))
        if not stations:
            messagebox.showerror("No stations configured", "No golden-unit test stations are configured.")
            return

        self._golden_batch_running = True
        self._golden_batch_cancel_requested = False
        self._golden_batch_state = {
            "stations": stations,
            "index": 0,
            "healthy": 0,
            "warning": 0,
            "attention": 0,
        }
        self.golden_run_one_button.configure(state="disabled")
        self.golden_run_all_button.configure(state="disabled")
        self.golden_cancel_button.configure(state="normal")
        self.golden_batch_progress.configure(maximum=max(1, len(stations)), value=0)
        self.golden_batch_status_var.set(
            f"Preparing golden-unit verification for {len(stations)} stations..."
        )
        self.tester_status_var.set("All-station check in progress")
        self.tester_detail_var.set(
            "WearTest is checking each configured station sequentially and updating progress after every station."
        )
        self.after(40, self._run_next_golden_station)

    def _run_next_golden_station(self) -> None:
        """Run the next station in an all-station golden-unit verification."""

        state = self._golden_batch_state
        if not self._golden_batch_running or state is None:
            return
        if self._golden_batch_cancel_requested:
            self._finish_golden_batch(cancelled=True)
            return

        index = int(state["index"])
        stations: list[str] = state["stations"]
        if index >= len(stations):
            self._finish_golden_batch(cancelled=False)
            return

        station_id = stations[index]
        self.golden_station_var.set(station_id)
        self._update_next_golden_check_label()
        self.golden_batch_status_var.set(
            f"Checking station {index + 1} of {len(stations)}: {station_id}..."
        )
        self.update_idletasks()

        try:
            result = self._execute_golden_check(station_id)
            self._show_tester_health_result(result)
            if result.status == "Healthy":
                state["healthy"] += 1
            elif result.status == "Warning":
                state["warning"] += 1
            else:
                state["attention"] += 1
        except Exception as exc:
            state["attention"] += 1
            self.golden_batch_status_var.set(f"{station_id} could not be completed: {exc}")

        state["index"] = index + 1
        self.golden_batch_progress.configure(value=state["index"])
        self._refresh_tester_health()
        self._refresh_history()
        self.after(80, self._run_next_golden_station)

    def _cancel_golden_batch(self) -> None:
        """Request a safe stop after the current golden-unit station finishes."""

        if self._golden_batch_running:
            self._golden_batch_cancel_requested = True
            self.golden_batch_status_var.set(
                "Cancel requested. Finishing the current station check..."
            )

    def _finish_golden_batch(self, *, cancelled: bool) -> None:
        """Restore tester controls and summarize the all-station golden check.

        Args:
            cancelled: Whether the user stopped the batch before all stations ran.
        """

        state = self._golden_batch_state or {}
        completed = int(state.get("index", 0))
        total = len(state.get("stations", []))
        healthy = int(state.get("healthy", 0))
        warning = int(state.get("warning", 0))
        attention = int(state.get("attention", 0))

        self._golden_batch_running = False
        self._golden_batch_cancel_requested = False
        self._golden_batch_state = None
        self.golden_run_one_button.configure(state="normal")
        self.golden_run_all_button.configure(state="normal")
        self.golden_cancel_button.configure(state="disabled")

        if cancelled:
            prefix = f"All-station check cancelled safely: {completed} of {total} completed."
        else:
            prefix = f"All {completed} configured stations checked."
        self.golden_batch_status_var.set(
            f"{prefix} Healthy {healthy} | Warning {warning} | Needs attention {attention}."
        )
        if attention:
            self.tester_status_label.configure(fg=self.FAIL)
            self.tester_status_var.set("Some stations need attention")
        elif warning:
            self.tester_status_label.configure(fg=self.WARN)
            self.tester_status_var.set("Some stations are approaching limits")
        else:
            self.tester_status_label.configure(fg=self.PASS)
            self.tester_status_var.set("All checked stations are healthy")
        self.tester_detail_var.set(
            "Review the station summary and recent tester-health history below for individual results."
        )
        self._refresh_tester_health()
        self._refresh_history()
        self._update_next_golden_check_label()

    def _show_tester_health_result(self, result: TesterHealthResult) -> None:
        """Render one golden-unit tester-health result in the workspace.

        Args:
            result: Completed station-health result.
        """

        for item in self.golden_result_tree.get_children():
            self.golden_result_tree.delete(item)

        for metric in result.metrics:
            tag = (
                "healthy" if metric.status == "Healthy"
                else "warning" if metric.status == "Warning"
                else "attention"
            )
            self.golden_result_tree.insert(
                "", "end",
                values=(
                    metric.name,
                    f"{metric.measured:.4f} {metric.unit}",
                    f"{metric.reference:.4f} {metric.unit}",
                    f"{metric.bias:+.4f} {metric.unit}",
                    f"± {metric.tolerance:.4f} {metric.unit}",
                    metric.status,
                ),
                tags=(tag,),
            )

        if result.status == "Healthy":
            color = self.PASS
            detail = "The station is measuring the known-good reference within the normal bias range."
        elif result.status == "Warning":
            color = self.WARN
            detail = "The station is still inside the action limits, but at least one bias is approaching its allowed limit."
        else:
            color = self.FAIL
            detail = "At least one tester bias exceeds its allowed limit. Review or calibrate the station before relying on production decisions."
        self.tester_status_label.configure(fg=color)
        self.tester_status_var.set(result.status)
        self.tester_detail_var.set(detail)
        self.tester_check_meta_var.set(
            f"{result.station_id}  |  Check {result.check_number}  |  Largest bias: {result.worst_metric}"
        )

    def _refresh_tester_health(self) -> None:
        """Refresh recent golden checks and tester-health history from SQLite."""

        if not hasattr(self, "tester_history_tree"):
            return
        for item in self.tester_history_tree.get_children():
            self.tester_history_tree.delete(item)
        for row in self.db.recent_golden_checks(limit=12):
            status = row["health_status"]
            tag = "healthy" if status == "Healthy" else "warning" if status == "Warning" else "attention"
            self.tester_history_tree.insert(
                "", "end",
                values=(
                    self._utc_to_local_display(row["created_utc"]),
                    row["station_id"],
                    row["check_number"],
                    status,
                    f"{row['worst_metric']} ({100.0 * row['max_bias_ratio']:.0f}% of limit)",
                ),
                tags=(tag,),
            )
        self._update_next_golden_check_label()

    def _build_grr_page(self, parent: ttk.Frame) -> None:
        """Build the crossed GR&R measurement-system analysis workspace.

        Args:
            parent: Page frame receiving study controls, results, charts, and history.
        """

        canvas = tk.Canvas(parent, bg=self.BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        self.grr_canvas = canvas
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = tk.Frame(canvas, bg=self.BG)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )

        self._page_heading(
            content,
            "Measurement System Analysis",
            "Run a crossed Gage R&R study to separate part variation from tester repeatability and station-to-station reproducibility.",
        )

        info_outer = tk.Frame(content, bg=self.BORDER)
        info_outer.pack(fill="x", pady=(0, 14))
        info = tk.Frame(info_outer, bg=self.PANEL, padx=18, pady=15)
        info.pack(fill="x", padx=1, pady=1)
        tk.Label(
            info,
            text="How this study works",
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            info,
            text=(
                "Each configured station measures the same set of known reference units several times. "
                "WearTest then estimates repeatability, reproducibility, total GR&R, part-to-part variation, and station bias."
            ),
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 11),
            justify="left",
            wraplength=1080,
        ).pack(anchor="w", pady=(4, 0))

        controls_outer = tk.Frame(content, bg=self.BORDER)
        controls_outer.pack(fill="x", pady=(0, 14))
        controls = tk.Frame(controls_outer, bg=self.PANEL, padx=18, pady=16)
        controls.pack(fill="x", padx=1, pady=1)
        controls.grid_columnconfigure(0, weight=2)
        controls.grid_columnconfigure(1, weight=1)
        controls.grid_columnconfigure(2, weight=1)
        controls.grid_columnconfigure(3, weight=2)

        tk.Label(
            controls, text="Study setup", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))

        display_names = [metric.display_name for metric in self.grr_config.metrics.values()]
        self.grr_metric_key_by_display = {
            metric.display_name: metric.key for metric in self.grr_config.metrics.values()
        }
        default_display = self.grr_config.metrics[self.grr_config.default_metric].display_name
        self.grr_metric_var = tk.StringVar(value=default_display)
        self.grr_parts_var = tk.StringVar(value=str(self.grr_config.reference_units))
        self.grr_trials_var = tk.StringVar(value=str(self.grr_config.trials_per_station))

        for column, label in enumerate(("Measurement", "Reference units", "Trials / station", "Stations")):
            tk.Label(
                controls, text=label, bg=self.PANEL, fg=self.MUTED,
                font=(self.font_family, 10, "bold"),
            ).grid(row=1, column=column, sticky="w", padx=(0, 12))

        ttk.Combobox(
            controls,
            textvariable=self.grr_metric_var,
            values=display_names,
            state="readonly",
            font=(self.font_family, 12),
        ).grid(row=2, column=0, sticky="ew", padx=(0, 12), pady=(4, 0))

        tk.Spinbox(
            controls,
            from_=2,
            to=12,
            textvariable=self.grr_parts_var,
            bg=self.PANEL,
            fg=self.TEXT,
            buttonbackground=self.PANEL_3,
            relief="solid",
            bd=1,
            font=(self.font_family, 12),
        ).grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=(4, 0), ipady=5)

        tk.Spinbox(
            controls,
            from_=2,
            to=8,
            textvariable=self.grr_trials_var,
            bg=self.PANEL,
            fg=self.TEXT,
            buttonbackground=self.PANEL_3,
            relief="solid",
            bd=1,
            font=(self.font_family, 12),
        ).grid(row=2, column=2, sticky="ew", padx=(0, 12), pady=(4, 0), ipady=5)

        station_text = ", ".join(sorted(self.golden_config.station_drift))
        tk.Label(
            controls,
            text=station_text,
            bg=self.PANEL_3,
            fg=self.TEXT,
            font=(self.font_family, 12),
            anchor="w",
            padx=10,
            pady=7,
        ).grid(row=2, column=3, sticky="ew", pady=(4, 0))

        action_row = tk.Frame(controls, bg=self.PANEL)
        action_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(16, 0))
        self.grr_run_button = ttk.Button(
            action_row,
            text="Run GR&R study",
            style="Primary.TButton",
            command=self._run_grr_study,
        )
        self.grr_run_button.pack(side="left")
        self.grr_cancel_button = ttk.Button(
            action_row,
            text="Cancel",
            style="Secondary.TButton",
            command=self._cancel_grr_study,
            state="disabled",
        )
        self.grr_cancel_button.pack(side="left", padx=(10, 0))

        self.grr_progress = ttk.Progressbar(action_row, mode="determinate", length=300)
        self.grr_progress.pack(side="left", fill="x", expand=True, padx=(18, 0))
        self.grr_progress_text_var = tk.StringVar(value="Ready to run a measurement-system study.")
        tk.Label(
            controls,
            textvariable=self.grr_progress_text_var,
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 10),
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))

        result_strip = tk.Frame(content, bg=self.BG)
        result_strip.pack(fill="x", pady=(0, 14))
        self.grr_summary_vars = {
            "percent": tk.StringVar(value="--"),
            "repeat": tk.StringVar(value="--"),
            "repro": tk.StringVar(value="--"),
            "ndc": tk.StringVar(value="--"),
            "assessment": tk.StringVar(value="Not run"),
        }
        cards = (
            ("percent", "% GR&R", "Measurement-system share of total variation"),
            ("repeat", "Repeatability", "Within-station variation"),
            ("repro", "Reproducibility", "Station + interaction variation"),
            ("ndc", "Distinct categories", f"Project target: at least {self.grr_config.ndc_min}"),
            ("assessment", "Assessment", "Uses editable project thresholds"),
        )
        for index, (key, label, note) in enumerate(cards):
            outer = tk.Frame(result_strip, bg=self.BORDER)
            outer.pack(side="left", fill="both", expand=True, padx=(0, 8 if index < len(cards) - 1 else 0))
            card = tk.Frame(outer, bg=self.PANEL, padx=14, pady=12)
            card.pack(fill="both", expand=True, padx=1, pady=1)
            tk.Label(
                card, text=label, bg=self.PANEL, fg=self.MUTED,
                font=(self.font_family, 10, "bold"),
            ).pack(anchor="w")
            tk.Label(
                card, textvariable=self.grr_summary_vars[key], bg=self.PANEL, fg=self.TEXT,
                font=(self.font_family, 18, "bold"),
            ).pack(anchor="w", pady=(4, 2))
            tk.Label(
                card, text=note, bg=self.PANEL, fg=self.MUTED,
                font=(self.font_family, 9), wraplength=205, justify="left",
            ).pack(anchor="w")

        variation_card, self.grr_variation_ax, self.grr_variation_canvas = self._build_chart_card(
            content,
            title="Variation contribution",
            description="Compares repeatability, reproducibility, and actual reference-unit variation.",
            figure_height=2.8,
        )
        variation_card.pack(fill="x", pady=(0, 14))

        bias_card, self.grr_bias_ax, self.grr_bias_canvas = self._build_chart_card(
            content,
            title="Station bias",
            description="Mean measured-minus-reference error for each station in the completed study.",
            figure_height=2.6,
        )
        bias_card.pack(fill="x", pady=(0, 14))

        history_outer = tk.Frame(content, bg=self.BORDER)
        history_outer.pack(fill="x", pady=(0, 18))
        history = tk.Frame(history_outer, bg=self.PANEL, padx=14, pady=12)
        history.pack(fill="x", padx=1, pady=1)
        tk.Label(
            history, text="Recent GR&R studies", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            history,
            text="Completed studies remain in the local traceability database for comparison over time.",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 10),
        ).pack(anchor="w", pady=(2, 8))

        columns = ("time", "metric", "parts", "stations", "trials", "grr", "ndc", "assessment")
        self.grr_history_tree = ttk.Treeview(
            history, columns=columns, show="headings", height=7, style="Data.Treeview"
        )
        headings = {
            "time": "Local time",
            "metric": "Measurement",
            "parts": "Parts",
            "stations": "Stations",
            "trials": "Trials",
            "grr": "% GR&R",
            "ndc": "ndc",
            "assessment": "Assessment",
        }
        widths = {"time": 150, "metric": 220, "parts": 70, "stations": 80, "trials": 70, "grr": 90, "ndc": 70, "assessment": 150}
        for key in columns:
            self.grr_history_tree.heading(key, text=headings[key])
            self.grr_history_tree.column(key, width=widths[key], anchor="center" if key not in {"time", "metric", "assessment"} else "w")
        self.grr_history_tree.pack(fill="x")
        self.grr_history_tree.tag_configure("acceptable", foreground=self.PASS)
        self.grr_history_tree.tag_configure("review", foreground=self.WARN)
        self.grr_history_tree.tag_configure("improve", foreground=self.FAIL)

        decision_outer = tk.Frame(content, bg=self.BORDER)
        decision_outer.pack(fill="x", pady=(0, 20))
        decision = tk.Frame(decision_outer, bg=self.PANEL_3, padx=16, pady=12)
        decision.pack(fill="x", padx=1, pady=1)
        tk.Label(
            decision,
            text=(
                f"Project decision bands: <= {self.grr_config.warning_percent_grr:.0f}% GR&R = Acceptable, "
                f"> {self.grr_config.warning_percent_grr:.0f}% to <= {self.grr_config.action_percent_grr:.0f}% = Review, "
                f"> {self.grr_config.action_percent_grr:.0f}% = Needs improvement. "
                "The numerical result remains visible so a company can apply its own internal criteria."
            ),
            bg=self.PANEL_3,
            fg=self.MUTED,
            font=(self.font_family, 10),
            justify="left",
            wraplength=1100,
        ).pack(anchor="w")

        self._refresh_grr_charts(None)
        self._refresh_grr_history()

    def _run_grr_study(self) -> None:
        """Start a non-blocking crossed GR&R study across all configured stations."""

        if self._grr_running:
            return
        try:
            part_count = int(self.grr_parts_var.get())
            trials = int(self.grr_trials_var.get())
        except ValueError:
            messagebox.showerror("Invalid GR&R setup", "Reference units and trials must be whole numbers.")
            return
        if part_count < 2 or trials < 2:
            messagebox.showerror("Invalid GR&R setup", "Use at least 2 reference units and 2 trials per station.")
            return

        display_name = self.grr_metric_var.get().strip()
        metric_key = self.grr_metric_key_by_display.get(display_name)
        if metric_key is None:
            messagebox.showerror("Invalid GR&R setup", "Select a configured measurement.")
            return
        metric = self.grr_config.metrics[metric_key]
        stations = sorted(self.golden_config.station_drift)
        if len(stations) < 2:
            messagebox.showerror("GR&R requires multiple stations", "Configure at least two ATE stations before running GR&R.")
            return

        references = build_reference_values(metric, part_count)
        tasks = [
            (part_id, reference_value, station_id, trial)
            for part_id, reference_value in references
            for station_id in stations
            for trial in range(1, trials + 1)
        ]
        check_numbers = {
            station_id: max(1, self.db.golden_check_count(station_id) + 1)
            for station_id in stations
        }

        self._grr_running = True
        self._grr_cancel_requested = False
        self._grr_state = {
            "metric": metric,
            "tasks": tasks,
            "index": 0,
            "observations": [],
            "stations": stations,
            "part_count": part_count,
            "trials": trials,
            "check_numbers": check_numbers,
        }
        self.grr_run_button.configure(state="disabled")
        self.grr_cancel_button.configure(state="normal")
        self.grr_progress.configure(maximum=max(1, len(tasks)), value=0)
        self.grr_progress_text_var.set(
            f"Preparing {len(tasks)} repeated measurements across {len(stations)} stations..."
        )
        self.after(40, self._run_next_grr_observation)

    def _run_next_grr_observation(self) -> None:
        """Generate the next repeated reference measurement and update progress."""

        state = self._grr_state
        if not self._grr_running or state is None:
            return
        if self._grr_cancel_requested:
            self._finish_grr_study(cancelled=True)
            return

        index = int(state["index"])
        tasks = state["tasks"]
        if index >= len(tasks):
            self._finish_grr_study(cancelled=False)
            return

        part_id, reference_value, station_id, trial = tasks[index]
        metric = state["metric"]
        measured = self.simulator.measure_grr_reference(
            metric=metric,
            reference_value=reference_value,
            station_id=station_id,
            golden_config=self.golden_config,
            station_check_number=state["check_numbers"][station_id],
        )
        state["observations"].append(
            GrrObservation(
                part_id=part_id,
                reference_value=reference_value,
                station_id=station_id,
                trial=trial,
                measured_value=measured,
            )
        )
        state["index"] = index + 1
        self.grr_progress.configure(value=state["index"])
        self.grr_progress_text_var.set(
            f"Measurement {state['index']} of {len(tasks)} | {part_id} | {station_id} | trial {trial}"
        )
        self.after(18, self._run_next_grr_observation)

    def _cancel_grr_study(self) -> None:
        """Request a safe stop after the current GR&R observation."""

        if self._grr_running:
            self._grr_cancel_requested = True
            self.grr_progress_text_var.set("Cancel requested. Finishing the current observation...")

    def _finish_grr_study(self, *, cancelled: bool) -> None:
        """Analyze or discard the current GR&R batch and restore controls.

        Args:
            cancelled: Whether the user stopped the study before all observations finished.
        """

        state = self._grr_state or {}
        completed = int(state.get("index", 0))
        total = len(state.get("tasks", []))
        self._grr_running = False
        self._grr_cancel_requested = False
        self.grr_run_button.configure(state="normal")
        self.grr_cancel_button.configure(state="disabled")

        if cancelled:
            self.grr_progress_text_var.set(
                f"Study cancelled safely after {completed} of {total} measurements. Partial data were not saved."
            )
            self._grr_state = None
            return

        try:
            result = run_crossed_grr(
                state["observations"],
                metric=state["metric"],
                config=self.grr_config,
            )
            self.db.save_grr_study(result)
            self.last_grr_result = result
            self._show_grr_result(result)
            self._refresh_grr_history()
            self.grr_progress_text_var.set(
                f"Study complete: {total} measurements | %GR&R {result.percent_grr:.1f}% | {result.assessment}."
            )
        except Exception as exc:
            self.grr_progress_text_var.set("GR&R analysis could not be completed.")
            messagebox.showerror("GR&R study failed", str(exc))
        finally:
            self._grr_state = None

    def _show_grr_result(self, result: GrrStudyResult) -> None:
        """Update GR&R result cards and charts from a completed study.

        Args:
            result: Completed measurement-system study.
        """

        unit = result.unit
        self.grr_summary_vars["percent"].set(f"{result.percent_grr:.1f}%")
        self.grr_summary_vars["repeat"].set(f"{result.repeatability_sigma:.4g} {unit}")
        self.grr_summary_vars["repro"].set(f"{result.reproducibility_sigma:.4g} {unit}")
        self.grr_summary_vars["ndc"].set(str(result.ndc))
        self.grr_summary_vars["assessment"].set(result.assessment)
        self._refresh_grr_charts(result)

    def _refresh_grr_charts(self, result: GrrStudyResult | None) -> None:
        """Draw GR&R variation contribution and station-bias charts.

        Args:
            result: Completed study, or None when no study has run in this session.
        """

        self.grr_variation_ax.clear()
        self._style_chart_axis(self.grr_variation_ax)
        self.grr_bias_ax.clear()
        self._style_chart_axis(self.grr_bias_ax)

        if result is None:
            for axis, text in (
                (self.grr_variation_ax, "Run a GR&R study to view variation contributions."),
                (self.grr_bias_ax, "Station bias will appear after a study is complete."),
            ):
                axis.text(
                    0.5, 0.5, text, ha="center", va="center",
                    transform=axis.transAxes, color=self.MUTED,
                    fontfamily=self.font_family, fontsize=11,
                )
                axis.set_xticks([])
                axis.set_yticks([])
            self.grr_variation_canvas.draw_idle()
            self.grr_bias_canvas.draw_idle()
            return

        total_variance = max(result.total_sigma**2, 1e-18)
        contributions = [
            100.0 * result.repeatability_sigma**2 / total_variance,
            100.0 * result.reproducibility_sigma**2 / total_variance,
            100.0 * result.part_sigma**2 / total_variance,
        ]
        labels = ["Repeatability", "Reproducibility", "Part-to-part"]
        bars = self.grr_variation_ax.bar(labels, contributions, color=self.ACCENT, width=0.55)
        self.grr_variation_ax.set_ylabel("Variance contribution (%)", fontfamily=self.font_family)
        self.grr_variation_ax.set_ylim(0, max(100.0, max(contributions) * 1.18))
        for bar, value in zip(bars, contributions):
            self.grr_variation_ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 2,
                f"{value:.1f}%",
                ha="center", va="bottom", color=self.TEXT,
                fontfamily=self.font_family, fontsize=10, fontweight="bold",
            )
        self.grr_variation_ax.figure.subplots_adjust(left=0.08, right=0.985, bottom=0.20, top=0.94)

        stations = sorted(result.station_biases)
        biases = [result.station_biases[station] for station in stations]
        colors = [self.FAIL if abs(value) == max(abs(item) for item in biases) and value != 0 else self.ACCENT for value in biases]
        bars = self.grr_bias_ax.bar(stations, biases, color=colors, width=0.55)
        self.grr_bias_ax.axhline(0.0, color=self.TEXT, linewidth=1.0)
        self.grr_bias_ax.set_ylabel(f"Mean bias ({result.unit})", fontfamily=self.font_family)
        span = max([abs(value) for value in biases] + [1e-9])
        self.grr_bias_ax.set_ylim(-1.45 * span, 1.45 * span)
        for bar, value in zip(bars, biases):
            self.grr_bias_ax.text(
                bar.get_x() + bar.get_width() / 2,
                value + (0.08 * span if value >= 0 else -0.08 * span),
                f"{value:+.4g}",
                ha="center", va="bottom" if value >= 0 else "top",
                color=self.TEXT, fontfamily=self.font_family, fontsize=10,
            )
        self.grr_bias_ax.figure.subplots_adjust(left=0.08, right=0.985, bottom=0.20, top=0.94)
        self.grr_variation_canvas.draw_idle()
        self.grr_bias_canvas.draw_idle()

    def _refresh_grr_history(self) -> None:
        """Reload recent GR&R study summaries from SQLite."""

        if not hasattr(self, "grr_history_tree"):
            return
        for item in self.grr_history_tree.get_children():
            self.grr_history_tree.delete(item)
        for row in self.db.recent_grr_studies(limit=12):
            assessment = row["assessment"]
            tag = (
                "acceptable" if assessment == "Acceptable"
                else "review" if assessment == "Review"
                else "improve"
            )
            self.grr_history_tree.insert(
                "", "end",
                values=(
                    self._utc_to_local_display(row["created_utc"]),
                    row["metric_name"],
                    row["part_count"],
                    row["station_count"],
                    row["trials_per_station"],
                    f"{row['percent_grr']:.1f}%",
                    row["ndc"],
                    assessment,
                ),
                tags=(tag,),
            )


    def _build_cycle_time_page(self, parent: ttk.Frame) -> None:
        """Build cycle-time analysis and coverage-validation workspace.

        Args:
            parent: Page frame receiving optimization controls, results, and history.
        """

        canvas = tk.Canvas(parent, bg=self.BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        self.cycle_canvas = canvas
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = tk.Frame(canvas, bg=self.BG)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )

        self._page_heading(
            content,
            "Cycle-Time Analysis",
            "Compare faster test sequences against the baseline and validate that throughput gains do not come from losing defect coverage.",
        )

        explanation_outer = tk.Frame(content, bg=self.BORDER)
        explanation_outer.pack(fill="x", pady=(0, 14))
        explanation = tk.Frame(explanation_outer, bg=self.PANEL, padx=18, pady=15)
        explanation.pack(fill="x", padx=1, pady=1)
        tk.Label(
            explanation,
            text="What WearTest is optimizing",
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            explanation,
            text=(
                "The study shortens sensor acquisition windows and overlaps compatible checks. "
                "Each faster sequence is replayed against a balanced set of healthy and intentionally defective simulated wearables. "
                "A faster sequence is only validated when it stays inside the configured defect-detection, escape, and false-reject guardrails."
            ),
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 11),
            justify="left",
            wraplength=1100,
        ).pack(anchor="w", pady=(4, 0))

        controls_outer = tk.Frame(content, bg=self.BORDER)
        controls_outer.pack(fill="x", pady=(0, 14))
        controls = tk.Frame(controls_outer, bg=self.PANEL, padx=18, pady=16)
        controls.pack(fill="x", padx=1, pady=1)
        controls.grid_columnconfigure(0, weight=2)
        controls.grid_columnconfigure(1, weight=1)
        controls.grid_columnconfigure(2, weight=3)

        tk.Label(
            controls, text="Validation setup", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        product_names = tuple(profile.name for profile in self.product_profiles)
        default_product = (
            self.cycle_time_config.default_product
            if self.cycle_time_config.default_product in product_names
            else product_names[0]
        )
        self.cycle_product_var = tk.StringVar(value=default_product)
        self.cycle_devices_var = tk.StringVar(value=str(self.cycle_time_config.validation_devices))

        tk.Label(
            controls, text="Product profile", bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 10, "bold"),
        ).grid(row=1, column=0, sticky="w")
        tk.Label(
            controls, text="Validation devices", bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 10, "bold"),
        ).grid(row=1, column=1, sticky="w")
        tk.Label(
            controls, text="Quality guardrails", bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 10, "bold"),
        ).grid(row=1, column=2, sticky="w")

        ttk.Combobox(
            controls,
            textvariable=self.cycle_product_var,
            values=product_names,
            state="readonly",
            font=(self.font_family, 12),
        ).grid(row=2, column=0, sticky="ew", padx=(0, 12), pady=(4, 0))

        tk.Spinbox(
            controls,
            from_=16,
            to=2000,
            increment=8,
            textvariable=self.cycle_devices_var,
            bg=self.PANEL,
            fg=self.TEXT,
            buttonbackground=self.PANEL_3,
            relief="solid",
            bd=1,
            font=(self.font_family, 12),
        ).grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=(4, 0), ipady=5)

        guardrails = (
            f"Detection retained >= {self.cycle_time_config.min_detection_retention_percent:.1f}%  |  "
            f"Escapes <= {self.cycle_time_config.max_escape_rate_percent:.1f}%  |  "
            f"False-reject increase <= {self.cycle_time_config.max_false_reject_increase_pp:.1f} pp"
        )
        tk.Label(
            controls,
            text=guardrails,
            bg=self.PANEL_3,
            fg=self.TEXT,
            font=(self.font_family, 11),
            anchor="w",
            padx=10,
            pady=7,
        ).grid(row=2, column=2, sticky="ew", pady=(4, 0))

        action_row = tk.Frame(controls, bg=self.PANEL)
        action_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.cycle_run_button = ttk.Button(
            action_row,
            text="Run cycle-time analysis",
            style="Primary.TButton",
            command=self._run_cycle_time_study,
        )
        self.cycle_run_button.pack(side="left")
        self.cycle_cancel_button = ttk.Button(
            action_row,
            text="Cancel",
            style="Secondary.TButton",
            command=self._cancel_cycle_time_study,
            state="disabled",
        )
        self.cycle_cancel_button.pack(side="left", padx=(10, 0))
        self.cycle_progress = ttk.Progressbar(action_row, mode="determinate", length=360)
        self.cycle_progress.pack(side="left", fill="x", expand=True, padx=(18, 0))

        self.cycle_progress_text_var = tk.StringVar(value="Ready to validate the configured test sequences.")
        tk.Label(
            controls,
            textvariable=self.cycle_progress_text_var,
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 10),
            justify="left",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        metrics = tk.Frame(content, bg=self.BG)
        metrics.pack(fill="x", pady=(0, 14))
        self.cycle_summary_vars = {
            "baseline": tk.StringVar(value="-"),
            "recommended": tk.StringVar(value="-"),
            "reduction": tk.StringVar(value="-"),
            "throughput": tk.StringVar(value="-"),
        }
        cards = (
            ("baseline", "Baseline cycle", "Configured sequential sequence"),
            ("recommended", "Validated cycle", "Fastest sequence inside guardrails"),
            ("reduction", "Cycle reduction", "Compared with baseline"),
            ("throughput", "Estimated throughput", "Idealized units per hour"),
        )
        for index, (key, title, detail) in enumerate(cards):
            metrics.grid_columnconfigure(index, weight=1, uniform="cycle_metrics")
            outer = tk.Frame(metrics, bg=self.BORDER)
            outer.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0 if index == 3 else 5))
            card = tk.Frame(outer, bg=self.PANEL, padx=15, pady=13)
            card.pack(fill="both", expand=True, padx=1, pady=1)
            tk.Label(card, text=title, bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10, "bold")).pack(anchor="w")
            tk.Label(card, textvariable=self.cycle_summary_vars[key], bg=self.PANEL, fg=self.TEXT, font=(self.font_family, 20, "bold")).pack(anchor="w", pady=(3, 2))
            tk.Label(card, text=detail, bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 9)).pack(anchor="w")

        recommendation_outer = tk.Frame(content, bg=self.BORDER)
        recommendation_outer.pack(fill="x", pady=(0, 14))
        recommendation = tk.Frame(recommendation_outer, bg=self.PANEL, padx=18, pady=14)
        recommendation.pack(fill="x", padx=1, pady=1)
        tk.Label(
            recommendation, text="Recommendation", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).pack(anchor="w")
        self.cycle_recommendation_var = tk.StringVar(
            value="Run the study to identify the fastest sequence that preserves the configured quality guardrails."
        )
        tk.Label(
            recommendation, textvariable=self.cycle_recommendation_var,
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 11),
            justify="left", wraplength=1100,
        ).pack(anchor="w", pady=(4, 0))

        table_outer = tk.Frame(content, bg=self.BORDER)
        table_outer.pack(fill="x", pady=(0, 14))
        table_card = tk.Frame(table_outer, bg=self.PANEL, padx=15, pady=13)
        table_card.pack(fill="x", padx=1, pady=1)
        tk.Label(
            table_card, text="Sequence validation", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            table_card,
            text="Every strategy is evaluated against the same healthy and defective device set.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10),
        ).pack(anchor="w", pady=(2, 9))
        columns = ("strategy", "cycle", "uph", "retained", "escapes", "false_reject", "decision")
        self.cycle_result_tree = ttk.Treeview(table_card, columns=columns, show="headings", height=6)
        headings = (
            ("strategy", "Strategy", 220),
            ("cycle", "Cycle", 90),
            ("uph", "Units/hour", 90),
            ("retained", "Detection retained", 125),
            ("escapes", "Escapes", 90),
            ("false_reject", "False reject change", 125),
            ("decision", "Decision", 150),
        )
        for column, label, width in headings:
            self.cycle_result_tree.heading(column, text=label)
            self.cycle_result_tree.column(column, width=width, minwidth=75, anchor="w", stretch=True)
        self.cycle_result_tree.tag_configure("baseline", foreground=self.TEXT)
        self.cycle_result_tree.tag_configure("validated", foreground=self.PASS)
        self.cycle_result_tree.tag_configure("rejected", foreground=self.FAIL)
        self.cycle_result_tree.pack(fill="x")

        cycle_chart, self.cycle_time_ax, self.cycle_time_canvas = self._build_chart_card(
            content,
            title="Estimated station cycle time",
            description="Validated candidates can reduce acquisition time or overlap compatible checks without changing the acceptance engine.",
            figure_height=2.8,
        )
        cycle_chart.pack(fill="x", pady=(0, 14))

        coverage_chart, self.cycle_coverage_ax, self.cycle_coverage_canvas = self._build_chart_card(
            content,
            title="Coverage versus cycle time",
            description="The fastest sequence is useful only if simulated defect detection stays inside the configured quality guardrails.",
            figure_height=2.8,
        )
        coverage_chart.pack(fill="x", pady=(0, 14))

        history_outer = tk.Frame(content, bg=self.BORDER)
        history_outer.pack(fill="x", pady=(0, 18))
        history_card = tk.Frame(history_outer, bg=self.PANEL, padx=15, pady=13)
        history_card.pack(fill="x", padx=1, pady=1)
        tk.Label(
            history_card, text="Recent optimization studies", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            history_card, text="Stored locally for engineering traceability.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 10),
        ).pack(anchor="w", pady=(2, 9))
        history_columns = ("time", "product", "devices", "baseline", "recommended", "reduction")
        self.cycle_history_tree = ttk.Treeview(history_card, columns=history_columns, show="headings", height=5)
        history_headings = (
            ("time", "Local time", 165),
            ("product", "Product", 165),
            ("devices", "Devices", 80),
            ("baseline", "Baseline", 90),
            ("recommended", "Validated cycle", 105),
            ("reduction", "Reduction", 90),
        )
        for column, label, width in history_headings:
            self.cycle_history_tree.heading(column, text=label)
            self.cycle_history_tree.column(column, width=width, minwidth=70, anchor="w", stretch=True)
        self.cycle_history_tree.pack(fill="x")

        note_outer = tk.Frame(content, bg=self.BORDER)
        note_outer.pack(fill="x", pady=(0, 12))
        note = tk.Frame(note_outer, bg=self.PANEL_3, padx=14, pady=11)
        note.pack(fill="x", padx=1, pady=1)
        tk.Label(
            note,
            text=(
                "Cycle times are configurable simulation assumptions, not measured WHOOP factory times. "
                "The throughput value is an idealized single-station estimate (3600 / cycle time) and does not include line balancing, operator handling, downtime, or maintenance."
            ),
            bg=self.PANEL_3, fg=self.MUTED, font=(self.font_family, 10),
            justify="left", wraplength=1100,
        ).pack(anchor="w")

        self._refresh_cycle_time_charts(None)
        self._refresh_cycle_time_history()

    def _run_cycle_time_study(self) -> None:
        """Start a responsive simulation that validates every configured sequence."""

        if self._cycle_running:
            return
        try:
            validation_devices = int(self.cycle_devices_var.get())
        except ValueError:
            messagebox.showerror("Invalid cycle-time setup", "Validation devices must be a whole number.")
            return
        if validation_devices < 8:
            messagebox.showerror("Invalid cycle-time setup", "Use at least 8 validation devices.")
            return

        product_name = self.cycle_product_var.get().strip()
        profile = self.product_profiles_by_name.get(product_name)
        if profile is None:
            messagebox.showerror("Invalid cycle-time setup", "Select a configured product profile.")
            return

        candidates = (self.cycle_time_config.baseline, *self.cycle_time_config.candidates)
        self._cycle_running = True
        self._cycle_cancel_requested = False
        self._cycle_state = {
            "index": 0,
            "validation_devices": validation_devices,
            "profile": profile,
            "simulator": WearableSimulator(rng_seed=self.cycle_time_config.rng_seed),
            "fault_profiles": validation_fault_profiles(include_ecg=profile.include_ecg),
            "candidates": candidates,
            "counts": {candidate.key: CandidateValidationCounts() for candidate in candidates},
        }
        self.cycle_run_button.configure(state="disabled")
        self.cycle_cancel_button.configure(state="normal")
        self.cycle_progress.configure(maximum=validation_devices, value=0)
        self.cycle_progress_text_var.set(
            f"Preparing {validation_devices} validation devices across {len(candidates)} test sequences..."
        )
        self.after(35, self._run_next_cycle_time_device)

    def _run_next_cycle_time_device(self) -> None:
        """Generate and evaluate the next device in a cycle-time validation study."""

        state = self._cycle_state
        if not self._cycle_running or state is None:
            return
        if self._cycle_cancel_requested:
            self._finish_cycle_time_study(cancelled=True)
            return

        index = int(state["index"])
        total = int(state["validation_devices"])
        if index >= total:
            self._finish_cycle_time_study(cancelled=False)
            return

        profile = state["profile"]
        fault_profiles = state["fault_profiles"]
        fault = fault_profiles[index % len(fault_profiles)]
        device_id = f"CYCLE-{index + 1:04d}"
        records = state["simulator"].acquire(
            device_id,
            "ATE-CYCLE",
            faults=fault,
            include_ecg=profile.include_ecg,
        )
        baseline_disposition = self.engine.evaluate(records, require_ecg=profile.include_ecg)
        baseline_failed = not baseline_disposition.passed
        defective = is_faulty(fault)

        for candidate in state["candidates"]:
            candidate_records = (
                records
                if candidate.baseline
                else truncate_records_for_candidate(records, candidate)
            )
            disposition = self.engine.evaluate(
                candidate_records,
                require_ecg=profile.include_ecg,
            )
            state["counts"][candidate.key].record(
                defective=defective,
                baseline_failed=baseline_failed,
                candidate_failed=not disposition.passed,
            )

        state["index"] = index + 1
        self.cycle_progress.configure(value=state["index"])
        self.cycle_progress_text_var.set(
            f"Device {state['index']} of {total} | {fault_profile_label(fault)} | validating {len(state['candidates'])} sequences"
        )
        self.after(8, self._run_next_cycle_time_device)

    def _cancel_cycle_time_study(self) -> None:
        """Request a safe stop after the current validation device finishes."""

        if self._cycle_running:
            self._cycle_cancel_requested = True
            self.cycle_progress_text_var.set("Cancel requested. Finishing the current validation device...")

    def _finish_cycle_time_study(self, *, cancelled: bool) -> None:
        """Finalize or discard a cycle-time validation batch.

        Args:
            cancelled: Whether the user stopped the validation before completion.
        """

        state = self._cycle_state or {}
        completed = int(state.get("index", 0))
        total = int(state.get("validation_devices", 0))
        self._cycle_running = False
        self._cycle_cancel_requested = False
        self.cycle_run_button.configure(state="normal")
        self.cycle_cancel_button.configure(state="disabled")

        if cancelled:
            self.cycle_progress_text_var.set(
                f"Study cancelled safely after {completed} of {total} devices. Partial results were not saved."
            )
            self._cycle_state = None
            return

        try:
            profile = state["profile"]
            result = finalize_cycle_time_study(
                config=self.cycle_time_config,
                product_name=profile.name,
                include_ecg=profile.include_ecg,
                counts_by_key=state["counts"],
            )
            self.db.save_cycle_time_study(result)
            self.last_cycle_time_result = result
            self._show_cycle_time_result(result)
            self._refresh_cycle_time_history()
            self.cycle_progress_text_var.set(
                f"Study complete: {total} devices | {result.recommended.candidate.name} validated at {result.recommended.cycle_seconds:.1f} s."
            )
        except Exception as exc:
            self.cycle_progress_text_var.set("Cycle-time analysis could not be completed.")
            messagebox.showerror("Cycle-time study failed", str(exc))
        finally:
            self._cycle_state = None

    def _show_cycle_time_result(self, result: CycleTimeStudyResult) -> None:
        """Populate summary cards, table, and charts from a completed study.

        Args:
            result: Completed cycle-time validation result.
        """

        recommended = result.recommended
        baseline = result.baseline
        throughput_gain = (
            100.0 * (recommended.units_per_hour - baseline.units_per_hour) / baseline.units_per_hour
            if baseline.units_per_hour
            else 0.0
        )
        self.cycle_summary_vars["baseline"].set(f"{baseline.cycle_seconds:.1f} s")
        self.cycle_summary_vars["recommended"].set(f"{recommended.cycle_seconds:.1f} s")
        self.cycle_summary_vars["reduction"].set(f"{recommended.reduction_percent:.1f}%")
        self.cycle_summary_vars["throughput"].set(f"{recommended.units_per_hour:.0f} / h")
        self.cycle_recommendation_var.set(
            f"{recommended.candidate.name} is the fastest validated sequence in this study. "
            f"Estimated cycle time falls from {baseline.cycle_seconds:.1f} s to {recommended.cycle_seconds:.1f} s "
            f"({recommended.reduction_percent:.1f}% reduction), while idealized throughput rises by {throughput_gain:.1f}%. "
            f"Defect detection retained: {recommended.detection_retention_percent:.1f}%; escaped defects: {recommended.escape_rate_percent:.1f}%."
        )

        for item in self.cycle_result_tree.get_children():
            self.cycle_result_tree.delete(item)
        for item in (baseline, *result.candidates):
            if item.candidate.baseline:
                tag = "baseline"
                decision = "Baseline"
            elif item.validated:
                tag = "validated"
                decision = "Validated"
            else:
                tag = "rejected"
                decision = "Rejected"
            self.cycle_result_tree.insert(
                "", "end",
                values=(
                    item.candidate.name,
                    f"{item.cycle_seconds:.1f} s",
                    f"{item.units_per_hour:.0f}",
                    f"{item.detection_retention_percent:.1f}%",
                    f"{item.escape_rate_percent:.1f}%",
                    f"{item.false_reject_change_pp:+.1f} pp",
                    decision,
                ),
                tags=(tag,),
            )
        self._refresh_cycle_time_charts(result)

    def _refresh_cycle_time_charts(self, result: CycleTimeStudyResult | None) -> None:
        """Draw cycle-time and coverage tradeoff charts.

        Args:
            result: Completed study, or None before the first study runs.
        """

        self.cycle_time_ax.clear()
        self._style_chart_axis(self.cycle_time_ax)
        self.cycle_coverage_ax.clear()
        self._style_chart_axis(self.cycle_coverage_ax)

        if result is None:
            self.cycle_time_ax.text(
                0.5, 0.5, "Run the analysis to compare configured sequences.",
                ha="center", va="center", transform=self.cycle_time_ax.transAxes,
                color=self.MUTED, fontfamily=self.font_family, fontsize=12,
            )
            self.cycle_coverage_ax.text(
                0.5, 0.5, "Coverage tradeoffs will appear after validation.",
                ha="center", va="center", transform=self.cycle_coverage_ax.transAxes,
                color=self.MUTED, fontfamily=self.font_family, fontsize=12,
            )
            for axis in (self.cycle_time_ax, self.cycle_coverage_ax):
                axis.set_xticks([])
                axis.set_yticks([])
            self.cycle_time_canvas.draw_idle()
            self.cycle_coverage_canvas.draw_idle()
            return

        all_results = (result.baseline, *result.candidates)
        labels = [item.candidate.name for item in all_results]
        cycle_values = [item.cycle_seconds for item in all_results]
        colors = [
            self.TEXT if item.candidate.baseline
            else self.PASS if item.validated
            else self.FAIL
            for item in all_results
        ]
        positions = list(range(len(all_results)))
        bars = self.cycle_time_ax.bar(positions, cycle_values, color=colors, width=0.58)
        self.cycle_time_ax.set_xticks(positions, labels)
        self.cycle_time_ax.tick_params(axis="x", labelrotation=12, pad=8)
        self.cycle_time_ax.set_ylabel("Estimated cycle time (s)", fontfamily=self.font_family)
        self.cycle_time_ax.set_ylim(0, max(cycle_values) * 1.20)
        for bar, value in zip(bars, cycle_values):
            self.cycle_time_ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max(cycle_values) * 0.025,
                f"{value:.1f}s",
                ha="center", va="bottom", color=self.TEXT,
                fontfamily=self.font_family, fontsize=9, fontweight="bold",
            )
        self.cycle_time_ax.figure.subplots_adjust(left=0.08, right=0.985, bottom=0.24, top=0.94)

        for item in all_results:
            color = self.TEXT if item.candidate.baseline else self.PASS if item.validated else self.FAIL
            marker = "s" if item.candidate.baseline else "o"
            self.cycle_coverage_ax.scatter(
                item.cycle_seconds,
                item.detection_retention_percent,
                s=70,
                color=color,
                marker=marker,
                zorder=3,
            )
            self.cycle_coverage_ax.annotate(
                item.candidate.name,
                (item.cycle_seconds, item.detection_retention_percent),
                xytext=(6, 6), textcoords="offset points",
                color=self.TEXT, fontfamily=self.font_family, fontsize=9,
            )
        self.cycle_coverage_ax.axhline(
            self.cycle_time_config.min_detection_retention_percent,
            color=self.FAIL,
            linestyle="--",
            linewidth=1.3,
        )
        self.cycle_coverage_ax.set_xlabel("Estimated cycle time (s)", fontfamily=self.font_family)
        self.cycle_coverage_ax.set_ylabel("Defect detection retained (%)", fontfamily=self.font_family)
        y_min = min(
            self.cycle_time_config.min_detection_retention_percent - 6.0,
            min(item.detection_retention_percent for item in all_results) - 3.0,
        )
        self.cycle_coverage_ax.set_ylim(max(0.0, y_min), 102.0)
        self.cycle_coverage_ax.figure.subplots_adjust(left=0.08, right=0.985, bottom=0.20, top=0.94)
        self.cycle_time_canvas.draw_idle()
        self.cycle_coverage_canvas.draw_idle()

    def _refresh_cycle_time_history(self) -> None:
        """Reload recent cycle-time optimization studies from SQLite."""

        if not hasattr(self, "cycle_history_tree"):
            return
        for item in self.cycle_history_tree.get_children():
            self.cycle_history_tree.delete(item)
        for row in self.db.recent_cycle_time_studies(limit=12):
            self.cycle_history_tree.insert(
                "", "end",
                values=(
                    self._utc_to_local_display(row["created_utc"]),
                    row["product_name"],
                    row["validation_devices"],
                    f"{row['baseline_cycle_seconds']:.1f} s",
                    f"{row['recommended_cycle_seconds']:.1f} s",
                    f"{row['reduction_percent']:.1f}%",
                ),
            )

    def _build_integrity_page(self, parent: ttk.Frame) -> None:
        """Build a compact dashboard for the latest acquisition integrity checks.

        Args:
            parent: Page frame receiving integrity status cards.
        """

        self._page_heading(
            parent,
            "Data Integrity",
            "Confirm that the latest acquisition was standardized, verified, ordered correctly, and preserved for traceability.",
        )

        self.integrity_vars = {
            "format": tk.StringVar(value="JSON Lines (.jsonl)"),
            "checksum": tk.StringVar(value="Waiting for a test"),
            "sequence": tk.StringVar(value="Waiting for a test"),
            "records": tk.StringVar(value="0"),
            "source": tk.StringVar(value="Waiting for a test"),
            "raw": tk.StringVar(value="No normalized file yet"),
            "transport": tk.StringVar(value="Waiting for a test"),
        }

        # A concise explanation of the integrity pipeline replaces the previous
        # full-width stack of technical boxes.
        pipeline_outer = tk.Frame(parent, bg=self.BORDER)
        pipeline_outer.pack(fill="x", pady=(0, 16))
        pipeline = tk.Frame(pipeline_outer, bg=self.PANEL, padx=20, pady=16)
        pipeline.pack(fill="x", padx=1, pady=1)
        tk.Label(
            pipeline, text="How incoming data is protected", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            pipeline,
            text="Every source is converted to one internal record structure before the acceptance checks run.",
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 11),
        ).pack(anchor="w", pady=(3, 12))

        steps = tk.Frame(pipeline, bg=self.PANEL)
        steps.pack(fill="x")
        for index, (title, detail) in enumerate((
            ("Standardize", "Common JSONL records"),
            ("Verify", "CRC32 integrity check"),
            ("Order", "Sequence validation"),
            ("Preserve", "Primary + recovery storage"),
        )):
            steps.grid_columnconfigure(index, weight=1, uniform="integrity_steps")
            cell = tk.Frame(steps, bg=self.PANEL_3, padx=14, pady=10)
            cell.grid(row=0, column=index, sticky="ew", padx=(0, 8 if index < 3 else 0))
            tk.Label(
                cell, text=title, bg=self.PANEL_3, fg=self.TEXT,
                font=(self.font_family, 12, "bold"),
            ).pack(anchor="w")
            tk.Label(
                cell, text=detail, bg=self.PANEL_3, fg=self.MUTED,
                font=(self.font_family, 10),
            ).pack(anchor="w", pady=(2, 0))

        tk.Label(
            parent, text="Latest acquisition", bg=self.BG, fg=self.TEXT,
            font=(self.font_family, 16, "bold"),
        ).pack(anchor="w", pady=(0, 9))

        grid = tk.Frame(parent, bg=self.BG)
        grid.pack(fill="x", pady=(0, 12))
        grid.grid_columnconfigure(0, weight=1, uniform="integrity_cards")
        grid.grid_columnconfigure(1, weight=1, uniform="integrity_cards")

        cards = (
            ("checksum", "Checksum verification", "Detects accidental data changes", 0, 0),
            ("sequence", "Record ordering", "Confirms records arrived in sequence", 0, 1),
            ("records", "Records received", "Measurements in the latest test", 1, 0),
            ("source", "Source format", "Original acquisition source", 1, 1),
        )
        for key, title, note, row, column in cards:
            outer = tk.Frame(grid, bg=self.BORDER)
            outer.grid(
                row=row, column=column, sticky="nsew",
                padx=(0, 6) if column == 0 else (6, 0), pady=(0, 12),
            )
            card = tk.Frame(outer, bg=self.PANEL, padx=17, pady=14)
            card.pack(fill="both", expand=True, padx=1, pady=1)
            tk.Label(
                card, text=title, bg=self.PANEL, fg=self.TEXT,
                font=(self.font_family, 13, "bold"),
            ).pack(anchor="w")
            tk.Label(
                card, text=note, bg=self.PANEL, fg=self.MUTED,
                font=(self.font_family, 10),
            ).pack(anchor="w", pady=(2, 8))
            value_size = 21 if key == "records" else 12
            value_weight = "bold" if key == "records" else "normal"
            tk.Label(
                card, textvariable=self.integrity_vars[key], bg=self.PANEL, fg=self.TEXT,
                wraplength=480, justify="left",
                font=(self.font_family, value_size, value_weight),
            ).pack(anchor="w")

        details_outer = tk.Frame(parent, bg=self.BORDER)
        details_outer.pack(fill="x")
        details = tk.Frame(details_outer, bg=self.PANEL, padx=18, pady=15)
        details.pack(fill="x", padx=1, pady=1)
        details.grid_columnconfigure(0, weight=1)
        details.grid_columnconfigure(1, weight=1)

        for column, (key, title) in enumerate((
            ("raw", "Normalized record file"),
            ("transport", "Storage and recovery"),
        )):
            box = tk.Frame(details, bg=self.PANEL)
            box.grid(row=0, column=column, sticky="new", padx=(0, 16) if column == 0 else (16, 0))
            tk.Label(
                box, text=title, bg=self.PANEL, fg=self.TEXT,
                font=(self.font_family, 12, "bold"),
            ).pack(anchor="w")
            tk.Label(
                box, textvariable=self.integrity_vars[key], bg=self.PANEL, fg=self.MUTED,
                wraplength=500, justify="left", font=(self.font_family, 11),
            ).pack(anchor="w", pady=(4, 0))

    def _build_limits_page(self, parent: ttk.Frame) -> None:
        """Show active acceptance limits as readable engineering cards.

        Args:
            parent: Page frame receiving configuration information.
        """

        canvas = tk.Canvas(parent, bg=self.BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        self.limits_canvas = canvas
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = tk.Frame(canvas, bg=self.BG)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )

        self._page_heading(
            content,
            "Test Limits",
            "Review the acceptance limits currently used by the simulator and reload them after editing the configuration file.",
        )

        config_outer = tk.Frame(content, bg=self.BORDER)
        config_outer.pack(fill="x", pady=(0, 14))
        config_card = tk.Frame(config_outer, bg=self.PANEL, padx=18, pady=14)
        config_card.pack(fill="x", padx=1, pady=1)
        config_card.grid_columnconfigure(0, weight=1)
        tk.Label(
            config_card, text="Configuration file", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            config_card, text="test_specs.ini", bg=self.PANEL, fg=self.ACCENT_DARK,
            font=(self.font_family, 13, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.limit_path_var = tk.StringVar(value=str(self.config_path))
        tk.Label(
            config_card, textvariable=self.limit_path_var, bg=self.PANEL, fg=self.MUTED,
            wraplength=850, justify="left", font=(self.font_family, 10),
        ).grid(row=2, column=0, sticky="w", pady=(2, 0))
        ttk.Button(
            config_card, text="Reload from file", style="Secondary.TButton",
            command=self._reload_limits,
        ).grid(row=0, column=1, rowspan=3, sticky="e", padx=(18, 0))

        notice = tk.Frame(content, bg="#EAF2F8", padx=16, pady=11)
        notice.pack(fill="x", pady=(0, 14))
        tk.Label(
            notice, text="Portfolio simulation", bg="#EAF2F8", fg=self.ACCENT_DARK,
            font=(self.font_family, 11, "bold"),
        ).pack(side="left")
        tk.Label(
            notice,
            text="These are project assumptions used to exercise the test software. They are not WHOOP production specifications.",
            bg="#EAF2F8", fg=self.TEXT, font=(self.font_family, 11),
        ).pack(side="left", padx=(12, 0))

        self.limit_vars = {
            "battery": tk.StringVar(),
            "accel_bias": tk.StringVar(),
            "accel_noise": tk.StringVar(),
            "gyro_bias": tk.StringVar(),
            "gyro_noise": tk.StringVar(),
            "ppg_snr": tk.StringVar(),
            "ppg_sat": tk.StringVar(),
            "temp_ref": tk.StringVar(),
            "temp_error": tk.StringVar(),
            "ecg_impedance": tk.StringVar(),
        }

        tk.Label(
            content, text="Active acceptance limits", bg=self.BG, fg=self.TEXT,
            font=(self.font_family, 16, "bold"),
        ).pack(anchor="w", pady=(0, 9))

        limits_grid = tk.Frame(content, bg=self.BG)
        limits_grid.pack(fill="both", expand=True)
        limits_grid.grid_columnconfigure(0, weight=1, uniform="limit_cards")
        limits_grid.grid_columnconfigure(1, weight=1, uniform="limit_cards")

        def add_limit_card(
            title: str, rows: tuple[tuple[str, str], ...], row: int, column: int,
            *, columnspan: int = 1,
        ) -> None:
            """Add one grouped acceptance-limit card.

            Args:
                title: Sensor or subsystem heading.
                rows: Pairs of human-readable labels and ``limit_vars`` keys.
                row: Grid row in the limits workspace.
                column: Grid column in the limits workspace.
                columnspan: Number of grid columns occupied by the card.
            """

            outer = tk.Frame(limits_grid, bg=self.BORDER)
            left_pad = 0 if column == 0 else 6
            right_pad = 0 if column + columnspan >= 2 else 6
            outer.grid(
                row=row, column=column, columnspan=columnspan, sticky="nsew",
                padx=(left_pad, right_pad), pady=(0, 12),
            )
            card = tk.Frame(outer, bg=self.PANEL, padx=18, pady=15)
            card.pack(fill="both", expand=True, padx=1, pady=1)
            card.grid_columnconfigure(0, weight=1)
            tk.Label(
                card, text=title, bg=self.PANEL, fg=self.TEXT,
                font=(self.font_family, 14, "bold"),
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 9))
            for index, (label, key) in enumerate(rows, start=1):
                if index > 1:
                    tk.Frame(card, bg="#E3E6EA", height=1).grid(
                        row=index * 2 - 2, column=0, columnspan=2, sticky="ew", pady=5
                    )
                tk.Label(
                    card, text=label, bg=self.PANEL, fg=self.MUTED,
                    font=(self.font_family, 11),
                ).grid(row=index * 2 - 1, column=0, sticky="w", pady=2)
                tk.Label(
                    card, textvariable=self.limit_vars[key], bg=self.PANEL, fg=self.TEXT,
                    font=(self.font_family, 12, "bold"),
                ).grid(row=index * 2 - 1, column=1, sticky="e", padx=(20, 0), pady=2)

        add_limit_card(
            "Power", (("Battery voltage", "battery"),), 0, 0
        )
        add_limit_card(
            "Optical sensing",
            (("Minimum PPG signal-to-noise ratio", "ppg_snr"),
             ("Maximum PPG saturation", "ppg_sat")),
            0, 1,
        )
        add_limit_card(
            "Motion sensing",
            (("Accelerometer axis bias", "accel_bias"),
             ("Accelerometer RMS noise", "accel_noise"),
             ("Gyroscope bias", "gyro_bias"),
             ("Gyroscope RMS noise", "gyro_noise")),
            1, 0, columnspan=2,
        )
        add_limit_card(
            "Temperature",
            (("Fixture reference", "temp_ref"),
             ("Maximum error", "temp_error")),
            2, 0,
        )
        add_limit_card(
            "ECG electrode path", (("Maximum impedance", "ecg_impedance"),), 2, 1
        )

    def _build_simulation_page(self, parent: ttk.Frame) -> None:
        """Build developer controls for injecting known virtual DUT faults.

        Args:
            parent: Page frame receiving simulation controls.
        """

        self._page_heading(
            parent,
            "Simulation",
            "Inject known defects into the virtual wearable to verify that the independent manufacturing test engine detects them.",
        )

        outer = tk.Frame(parent, bg=self.BORDER)
        outer.pack(fill="x")
        card = tk.Frame(outer, bg=self.PANEL, padx=18, pady=18)
        card.pack(fill="x", padx=1, pady=1)
        tk.Label(card, text="DEFECT INJECTION", bg=self.PANEL, fg=self.TEXT, font=(self.font_family, 13, "bold")).pack(anchor="w")
        tk.Label(
            card,
            text="These controls affect only the virtual DUT. A real acquisition is never assigned a pass/fail result from this page.",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 14),
            wraplength=900,
            justify="left",
        ).pack(anchor="w", pady=(3, 13))

        self.fault_vars: dict[str, tk.BooleanVar] = {}
        faults = (
            ("battery_low", "Low battery voltage"),
            ("accel_bias", "Accelerometer offset"),
            ("gyro_bias", "Gyroscope offset"),
            ("ppg_low_snr", "Weak or noisy optical response"),
            ("ppg_saturation", "Optical channel saturation"),
            ("skin_temp_offset", "Temperature sensor offset"),
            ("ecg_high_impedance", "High ECG electrode-path impedance"),
        )
        grid = tk.Frame(card, bg=self.PANEL)
        grid.pack(fill="x")
        for index, (key, label) in enumerate(faults):
            variable = tk.BooleanVar(value=False)
            self.fault_vars[key] = variable
            ttk.Checkbutton(grid, text=label, variable=variable).grid(
                row=index // 2, column=index % 2, sticky="w", padx=(0, 60), pady=7
            )
        ttk.Button(card, text="Clear all defects", style="Secondary.TButton", command=self._clear_faults).pack(anchor="w", pady=(16, 0))

        batch_outer = tk.Frame(parent, bg=self.BORDER)
        batch_outer.pack(fill="x", pady=(14, 0))
        batch = tk.Frame(batch_outer, bg=self.PANEL, padx=18, pady=18)
        batch.pack(fill="x", padx=1, pady=1)
        batch.grid_columnconfigure(0, weight=1)
        batch.grid_columnconfigure(1, weight=0)
        batch.grid_columnconfigure(2, weight=0)
        batch.grid_columnconfigure(3, weight=0)

        tk.Label(
            batch, text="Production demo batch", bg=self.PANEL, fg=self.TEXT,
            font=(self.font_family, 16, "bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        tk.Label(
            batch,
            text=(
                "Generate a synthetic production batch across ATE-01, ATE-02, and "
                "ATE-03. Progress is shown while each device file is created, checked, "
                "and saved to traceability."
            ),
            bg=self.PANEL, fg=self.MUTED, font=(self.font_family, 13),
            wraplength=980, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 14))

        controls = tk.Frame(batch, bg=self.PANEL)
        controls.grid(row=2, column=0, columnspan=4, sticky="ew")
        controls.grid_columnconfigure(4, weight=1)
        tk.Label(
            controls, text="Devices in batch", bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 12, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.batch_count_var = tk.StringVar(value="120")
        self.batch_count_entry = ttk.Entry(controls, textvariable=self.batch_count_var, width=10)
        self.batch_count_entry.grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.batch_generate_button = ttk.Button(
            controls, text="Generate demo batch", style="Primary.TButton",
            command=self._generate_demo_batch,
        )
        self.batch_generate_button.grid(row=1, column=1, sticky="w", padx=(14, 0), pady=(5, 0))
        self.batch_cancel_button = ttk.Button(
            controls, text="Cancel", style="Secondary.TButton",
            command=self._cancel_demo_batch, state="disabled",
        )
        self.batch_cancel_button.grid(row=1, column=2, sticky="w", padx=(10, 0), pady=(5, 0))
        ttk.Button(
            controls, text="Open production dashboard", style="Secondary.TButton",
            command=lambda: self._show_page("history"),
        ).grid(row=1, column=3, sticky="w", padx=(10, 0), pady=(5, 0))

        progress_panel = tk.Frame(batch, bg=self.PANEL_3, padx=16, pady=14)
        progress_panel.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(16, 0))
        progress_panel.grid_columnconfigure(0, weight=1)

        progress_header = tk.Frame(progress_panel, bg=self.PANEL_3)
        progress_header.grid(row=0, column=0, sticky="ew")
        progress_header.grid_columnconfigure(0, weight=1)
        self.batch_progress_text_var = tk.StringVar(value="Ready to generate a demo batch")
        self.batch_percent_var = tk.StringVar(value="0%")
        tk.Label(
            progress_header, textvariable=self.batch_progress_text_var,
            bg=self.PANEL_3, fg=self.TEXT, font=(self.font_family, 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            progress_header, textvariable=self.batch_percent_var,
            bg=self.PANEL_3, fg=self.ACCENT, font=(self.font_family, 14, "bold"),
        ).grid(row=0, column=1, sticky="e")

        self.batch_progress_var = tk.DoubleVar(value=0.0)
        self.batch_progress = ttk.Progressbar(
            progress_panel, variable=self.batch_progress_var, maximum=100.0,
            mode="determinate",
        )
        self.batch_progress.grid(row=1, column=0, sticky="ew", pady=(9, 8))

        self.batch_stage_var = tk.StringVar(value="No files are being generated.")
        self.batch_file_var = tk.StringVar(value="Files created: 0")
        tk.Label(
            progress_panel, textvariable=self.batch_stage_var,
            bg=self.PANEL_3, fg=self.MUTED, font=(self.font_family, 12),
            justify="left", anchor="w",
        ).grid(row=2, column=0, sticky="ew")
        tk.Label(
            progress_panel, textvariable=self.batch_file_var,
            bg=self.PANEL_3, fg=self.MUTED, font=(self.font_family, 12),
            justify="left", anchor="w",
        ).grid(row=3, column=0, sticky="ew", pady=(3, 0))

        self.batch_status_var = tk.StringVar(
            value="The interface remains available while the batch is generated."
        )
        tk.Label(
            batch, textvariable=self.batch_status_var, bg=self.PANEL, fg=self.MUTED,
            font=(self.font_family, 12), wraplength=980, justify="left",
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))

    def _generate_demo_batch(self) -> None:
        """Start non-blocking generation of a synthetic production batch."""

        if self._batch_running:
            return

        try:
            count = int(self.batch_count_var.get())
        except ValueError:
            messagebox.showerror("Invalid batch size", "Enter a whole number of devices.")
            return
        if not 10 <= count <= 500:
            messagebox.showerror(
                "Invalid batch size", "Choose between 10 and 500 devices for the demo batch."
            )
            return

        choices = (
            "none", "battery_low", "accel_bias", "gyro_bias",
            "ppg_low_snr", "ppg_saturation", "skin_temp_offset",
            "ecg_high_impedance",
        )
        self._batch_state = {
            "count": count,
            "index": 0,
            "choices": choices,
            "weights": (90.0, 1.5, 1.5, 2.0, 2.0, 1.0, 1.0, 1.0),
            "rng": random.Random(20260923 + count),
            "stations": ("ATE-01", "ATE-02", "ATE-03"),
            "batch_tag": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "files_created": 0,
            "passes": 0,
            "failures": 0,
            "started": datetime.now(),
        }
        self._batch_running = True
        self._batch_cancel_requested = False
        self.batch_count_entry.configure(state="disabled")
        self.batch_generate_button.configure(state="disabled")
        self.batch_cancel_button.configure(state="normal")
        self.batch_progress_var.set(0.0)
        self.batch_percent_var.set("0%")
        self.batch_progress_text_var.set(f"0 of {count} devices completed")
        self.batch_stage_var.set("Preparing the production simulation...")
        self.batch_file_var.set("Files created: 0")
        self.batch_status_var.set(
            "Batch generation is running. You can continue to see progress here or cancel the remaining devices."
        )

        # Run one device at a time and return control to Tkinter between devices.
        # This avoids the frozen-window behavior caused by a long synchronous loop.
        self.after(50, self._generate_next_demo_device)

    def _generate_next_demo_device(self) -> None:
        """Generate one demo device, update progress, then schedule the next one."""

        state = self._batch_state
        if not self._batch_running or state is None:
            return
        if self._batch_cancel_requested:
            self._finish_demo_batch(cancelled=True)
            return

        index = int(state["index"])
        count = int(state["count"])
        if index >= count:
            self._finish_demo_batch(cancelled=False)
            return

        device_number = index + 1
        station_id = state["stations"][index % len(state["stations"])]
        device_id = f"DEMO-{state['batch_tag']}-{device_number:04d}"
        self.batch_stage_var.set(
            f"Generating measurements for {device_id} on {station_id}..."
        )
        self.batch_progress_text_var.set(
            f"{index} of {count} devices completed | Working on device {device_number}"
        )

        # Choose one synthetic defect category, generate the DUT data, and write the
        # standardized JSONL file used by the normal test pipeline.
        fault_name = state["rng"].choices(
            state["choices"], weights=state["weights"], k=1
        )[0]
        fault_values = {name: False for name in state["choices"] if name != "none"}
        if fault_name != "none":
            fault_values[fault_name] = True

        records = self.simulator.acquire(
            device_id, station_id, faults=FaultProfile(**fault_values), include_ecg=True
        )
        raw_path = self.records_dir / f"{records[0].session_id}.jsonl"

        self.batch_stage_var.set(
            f"Writing standardized device file {device_number} of {count}..."
        )
        write_jsonl(raw_path, records)
        state["files_created"] += 1
        self.batch_file_var.set(
            f"Files created: {state['files_created']} | Latest: {raw_path.name}"
        )

        self.batch_stage_var.set(
            f"Validating and evaluating {device_id}..."
        )
        received = read_jsonl(raw_path, verify_checksum=True)
        self.transport.send_many(received)
        disposition = self.engine.evaluate(received, require_ecg=True)
        self.db.save_disposition(
            disposition, station_id=station_id, product_variant="Wearable + ECG",
            raw_record_path=str(raw_path),
        )
        if disposition.passed:
            state["passes"] += 1
        else:
            state["failures"] += 1

        state["index"] = device_number
        percent = 100.0 * device_number / count
        self.batch_progress_var.set(percent)
        self.batch_percent_var.set(f"{percent:.0f}%")
        self.batch_progress_text_var.set(
            f"{device_number} of {count} devices completed"
        )
        self.batch_stage_var.set(
            f"Saved {device_id}: {'PASS' if disposition.passed else 'FAIL'} | Preparing next device..."
        )

        # Give Tkinter a chance to repaint the progress controls before processing
        # the next DUT. The short delay keeps the UI responsive without adding
        # meaningful time to the demonstration.
        self.after(8, self._generate_next_demo_device)

    def _cancel_demo_batch(self) -> None:
        """Request cancellation after the current demo device finishes."""

        if not self._batch_running:
            return
        self._batch_cancel_requested = True
        self.batch_cancel_button.configure(state="disabled")
        self.batch_stage_var.set("Cancelling after the current device finishes...")

    def _finish_demo_batch(self, *, cancelled: bool) -> None:
        """Finish batch generation and restore the simulation controls.

        Args:
            cancelled: True when the user stopped the batch before completion.
        """

        state = self._batch_state or {}
        completed = int(state.get("index", 0))
        count = int(state.get("count", completed))
        files_created = int(state.get("files_created", 0))
        passes = int(state.get("passes", 0))
        failures = int(state.get("failures", 0))
        started = state.get("started")
        elapsed = (datetime.now() - started).total_seconds() if started else 0.0

        self._batch_running = False
        self._batch_cancel_requested = False
        self.batch_count_entry.configure(state="normal")
        self.batch_generate_button.configure(state="normal")
        self.batch_cancel_button.configure(state="disabled")

        if cancelled:
            percent = (100.0 * completed / count) if count else 0.0
            self.batch_progress_var.set(percent)
            self.batch_percent_var.set(f"{percent:.0f}%")
            self.batch_progress_text_var.set(
                f"Cancelled after {completed} of {count} devices"
            )
            self.batch_stage_var.set("Batch generation stopped. Completed device files were kept.")
            self.batch_status_var.set(
                f"Cancelled safely. {files_created} files were created; "
                f"{passes} passed and {failures} failed before cancellation."
            )
        else:
            self.batch_progress_var.set(100.0)
            self.batch_percent_var.set("100%")
            self.batch_progress_text_var.set(f"{completed} of {count} devices completed")
            self.batch_stage_var.set("Production demo batch complete.")
            self.batch_status_var.set(
                f"Complete in {elapsed:.1f} s. {files_created} files created; "
                f"{passes} passed and {failures} failed. Open Production to review the results."
            )

        self._refresh_history()
        self._batch_state = None

    def _set_ready_state(self) -> None:
        """Reset the primary test result panel to its idle message."""

        self.overall_label.configure(style="StatusReady.TLabel")
        self.overall_var.set("Ready for device test")
        self.overall_detail_var.set("Enter the device information above and run the automated test sequence.")

    def _clear_faults(self) -> None:
        """Turn off every synthetic defect injection control."""

        for variable in self.fault_vars.values():
            variable.set(False)

    def _browse_external_source(self) -> None:
        """Let the user choose an external acquisition file."""

        path = filedialog.askopenfilename(
            title="Choose acquisition data",
            filetypes=(
                ("Supported acquisition files", "*.csv *.tdms *.jsonl"),
                ("CSV files", "*.csv"),
                ("TDMS files", "*.tdms"),
                ("Standardized JSONL", "*.jsonl"),
                ("All files", "*.*"),
            ),
        )
        if path:
            self.external_source_var.set(path)

    def _browse_mapping(self) -> None:
        """Let the user choose a JSON channel-mapping file."""

        path = filedialog.askopenfilename(
            title="Choose channel mapping",
            filetypes=(("JSON mapping", "*.json"), ("All files", "*.*")),
        )
        if path:
            self.external_mapping_var.set(path)

    def _selected_external_format(self) -> str:
        """Convert the source-format display value into an importer key.

        Returns:
            ``auto`` or a registered source-format name.
        """

        selected = self.external_format_var.get().strip()
        if selected == "Auto-detect":
            return "auto"
        return selected.split("|", 1)[0].strip()

    def _product_profile(self, name: str) -> ProductProfile:
        """Return one configured product profile by its display name.

        Args:
            name: Product profile name selected in the GUI.

        Returns:
            Matching configured product profile.

        Raises:
            ValueError: If the requested product profile is not configured.
        """

        try:
            return self.product_profiles_by_name[name]
        except KeyError as exc:
            raise ValueError(f"Unknown product profile: {name}") from exc

    def _run_test(self) -> None:
        """Simulate one selected product, verify it, and run manufacturing checks."""

        if self._product_batch_running:
            return
        device_id = self.device_id_var.get().strip()
        station_id = self.station_id_var.get().strip()
        if not device_id or not station_id:
            messagebox.showerror("Missing information", "Enter both a device ID and a test station ID.")
            return

        profile = self._product_profile(self.variant_var.get())
        self.overall_label.configure(style="StatusReady.TLabel")
        self.overall_var.set("Test in progress")
        self.overall_detail_var.set(f"Collecting and evaluating the {profile.name} product profile.")
        self.product_batch_status_var.set(f"Running selected product: {profile.name}...")
        self.update_idletasks()

        try:
            faults = FaultProfile(**{key: variable.get() for key, variable in self.fault_vars.items()})
            records = self.simulator.acquire(
                device_id, station_id, faults=faults, include_ecg=profile.include_ecg
            )
            disposition = self._evaluate_records(
                records,
                station_id=station_id,
                product_variant=profile.name,
                require_ecg=profile.include_ecg,
                status_label=self.overall_label,
                status_var=self.overall_var,
                detail_var=self.overall_detail_var,
                result_tree=self.result_tree,
            )
            self.product_batch_status_var.set(
                f"Selected product complete: {profile.name} | "
                f"{'PASS' if disposition.passed else 'FAIL'}"
            )
        except Exception as exc:
            self.product_batch_status_var.set("Selected product test could not be completed.")
            messagebox.showerror("Device test failed", str(exc))

    def _run_all_product_profiles(self) -> None:
        """Run one simulated product unit for every configured product profile."""

        if self._product_batch_running:
            return
        base_device_id = self.device_id_var.get().strip()
        station_id = self.station_id_var.get().strip()
        if not base_device_id or not station_id:
            messagebox.showerror("Missing information", "Enter both a Device ID prefix and a test station ID.")
            return

        faults = FaultProfile(**{key: variable.get() for key, variable in self.fault_vars.items()})
        self._product_batch_running = True
        self._product_batch_cancel_requested = False
        self._product_batch_state = {
            "profiles": list(self.product_profiles),
            "index": 0,
            "base_device_id": base_device_id,
            "station_id": station_id,
            "faults": faults,
            "passed": 0,
            "failed": 0,
        }
        self.run_test_button.configure(state="disabled")
        self.run_all_products_button.configure(state="disabled")
        self.cancel_product_batch_button.configure(state="normal")
        self.product_batch_progress.configure(maximum=max(1, len(self.product_profiles)), value=0)
        self.product_batch_status_var.set(
            f"Preparing {len(self.product_profiles)} configured product profiles..."
        )
        self.overall_label.configure(style="StatusReady.TLabel")
        self.overall_var.set("Product batch in progress")
        self.overall_detail_var.set(
            "Each configured product profile receives its own simulated DUT and traceability record."
        )
        self.after(40, self._run_next_product_profile)

    def _run_next_product_profile(self) -> None:
        """Run the next configured product profile and return control to Tk between runs."""

        state = self._product_batch_state
        if not self._product_batch_running or state is None:
            return
        if self._product_batch_cancel_requested:
            self._finish_product_batch(cancelled=True)
            return

        index = int(state["index"])
        profiles: list[ProductProfile] = state["profiles"]
        if index >= len(profiles):
            self._finish_product_batch(cancelled=False)
            return

        profile = profiles[index]
        device_id = f"{state['base_device_id']}-{profile.batch_suffix}"
        station_id = state["station_id"]
        self.product_batch_status_var.set(
            f"Running {index + 1} of {len(profiles)}: {profile.name} | Device {device_id}"
        )
        self.variant_var.set(profile.name)
        self.update_idletasks()

        try:
            records = self.simulator.acquire(
                device_id,
                station_id,
                faults=state["faults"],
                include_ecg=profile.include_ecg,
            )
            disposition = self._evaluate_records(
                records,
                station_id=station_id,
                product_variant=profile.name,
                require_ecg=profile.include_ecg,
                status_label=self.overall_label,
                status_var=self.overall_var,
                detail_var=self.overall_detail_var,
                result_tree=self.result_tree,
            )
            if disposition.passed:
                state["passed"] += 1
            else:
                state["failed"] += 1
        except Exception as exc:
            state["failed"] += 1
            self.product_batch_status_var.set(f"{profile.name} could not be completed: {exc}")

        state["index"] = index + 1
        self.product_batch_progress.configure(value=state["index"])
        self.after(80, self._run_next_product_profile)

    def _cancel_product_batch(self) -> None:
        """Request a safe stop after the current product profile finishes."""

        if self._product_batch_running:
            self._product_batch_cancel_requested = True
            self.product_batch_status_var.set("Cancel requested. Finishing the current product profile...")

    def _finish_product_batch(self, *, cancelled: bool) -> None:
        """Restore product controls and summarize a completed or cancelled batch.

        Args:
            cancelled: Whether the user requested cancellation before all profiles ran.
        """

        state = self._product_batch_state or {}
        completed = int(state.get("index", 0))
        total = len(state.get("profiles", []))
        passed = int(state.get("passed", 0))
        failed = int(state.get("failed", 0))
        self._product_batch_running = False
        self._product_batch_cancel_requested = False
        self._product_batch_state = None
        self.run_test_button.configure(state="normal")
        self.run_all_products_button.configure(state="normal")
        self.cancel_product_batch_button.configure(state="disabled")
        if cancelled:
            self.product_batch_status_var.set(
                f"Product batch cancelled safely: {completed} of {total} completed | {passed} passed | {failed} failed."
            )
            self.overall_var.set("Product batch cancelled")
        else:
            self.product_batch_status_var.set(
                f"All product profiles complete: {completed} tested | {passed} passed | {failed} failed."
            )
            self.overall_var.set("All product profiles complete")
        self.overall_detail_var.set(
            "Each completed profile has its own device record, raw standardized file, and production traceability entry."
        )
        self._refresh_history()

    def _run_external_test(self) -> None:
        """Import a user acquisition, normalize it, and run the same test engine."""

        source = self.external_source_var.get().strip()
        if not source:
            messagebox.showerror("No data file", "Choose an acquisition file to test.")
            return

        self.external_overall_label.configure(style="StatusReady.TLabel")
        self.external_overall_var.set("Import in progress")
        self.external_detail_var.set("Standardizing the source data and verifying record integrity.")
        self.update_idletasks()

        try:
            records = self.importer.import_file(
                source,
                device_id=self.external_device_var.get().strip(),
                station_id=self.external_station_var.get().strip(),
                source_format=self._selected_external_format(),
                mapping_path=self.external_mapping_var.get().strip() or None,
            )
            profile = self._product_profile(self.external_variant_var.get())
            self._evaluate_records(
                records,
                station_id=records[0].station_id,
                product_variant=profile.name,
                require_ecg=profile.include_ecg,
                status_label=self.external_overall_label,
                status_var=self.external_overall_var,
                detail_var=self.external_detail_var,
                result_tree=self.external_result_tree,
            )
        except Exception as exc:
            self.external_overall_label.configure(style="StatusFail.TLabel")
            self.external_overall_var.set("Import could not be tested")
            self.external_detail_var.set(str(exc))
            messagebox.showerror("Import failed", str(exc))

    def _evaluate_records(
        self,
        records: list[MeasurementRecord],
        *,
        station_id: str,
        product_variant: str,
        require_ecg: bool,
        status_label: ttk.Label,
        status_var: tk.StringVar,
        detail_var: tk.StringVar,
        result_tree: ttk.Treeview,
    ) -> DeviceDisposition:
        """Persist canonical records, verify them, and evaluate the DUT.

        Args:
            records: Canonical records from simulation or an external adapter.
            station_id: Station identifier stored with production traceability.
            product_variant: Product configuration associated with this test.
            require_ecg: Whether the selected product profile requires ECG data.
            status_label: GUI status label styled after evaluation.
            status_var: GUI variable for the primary result message.
            detail_var: GUI variable for supporting result text.
            result_tree: GUI table receiving individual test results.
        """

        if not records:
            raise ValueError("The acquisition did not produce any measurements.")

        session_id = records[0].session_id
        raw_path = self.records_dir / f"{session_id}.jsonl"
        write_jsonl(raw_path, records)

        # Re-reading the file makes checksum and sequence validation part of both paths.
        received = read_jsonl(raw_path, verify_checksum=True)
        primary_count, spooled_count = self.transport.send_many(received)
        disposition = self.engine.evaluate(received, require_ecg=require_ecg)
        self.db.save_disposition(
            disposition,
            station_id=station_id,
            product_variant=product_variant,
            raw_record_path=str(raw_path),
        )
        self.last_disposition = disposition

        self._show_disposition(
            disposition,
            status_label=status_label,
            status_var=status_var,
            detail_var=detail_var,
            result_tree=result_tree,
        )
        self._update_integrity(
            received,
            raw_path,
            primary_count=primary_count,
            spooled_count=spooled_count,
        )
        self._refresh_history()
        return disposition

    @staticmethod
    def _show_disposition(
        disposition: DeviceDisposition,
        *,
        status_label: ttk.Label,
        status_var: tk.StringVar,
        detail_var: tk.StringVar,
        result_tree: ttk.Treeview,
    ) -> None:
        """Render one device disposition in the result workspace.

        Args:
            disposition: Completed manufacturing test result.
            status_label: Label whose style communicates pass or fail.
            status_var: Variable receiving the headline result.
            detail_var: Variable receiving the operator explanation.
            result_tree: Table receiving step-level measurements and limits.
        """

        for item in result_tree.get_children():
            result_tree.delete(item)

        for result in disposition.results:
            passed = result.passed
            result_tree.insert(
                "",
                "end",
                values=(
                    result.name,
                    "PASS" if passed else "FAIL",
                    result.measured,
                    result.limit,
                ),
                tags=("pass" if passed else "fail",),
            )

        if disposition.passed:
            status_label.configure(style="StatusPass.TLabel")
            status_var.set("Pass: device accepted")
            detail_var.set("All required checks are within the simulated acceptance limits.")
            return

        status_label.configure(style="StatusFail.TLabel")
        failed_checks = [result.name for result in disposition.results if not result.passed]
        status_var.set("Fail: device needs review")
        detail_var.set("Outside acceptance limits: " + ", ".join(failed_checks) + ".")

    def _update_integrity(
        self,
        records: list[MeasurementRecord],
        raw_path: Path,
        *,
        primary_count: int,
        spooled_count: int,
    ) -> None:
        """Update integrity status after successful canonical validation.

        Args:
            records: Verified canonical records.
            raw_path: Normalized JSONL file retained for traceability.
            primary_count: Records written to the preferred destination.
            spooled_count: Records preserved in the recovery spool.
        """

        source_formats = sorted({record.source_format for record in records})
        self.integrity_vars["checksum"].set("CRC32 verified for every received record")
        self.integrity_vars["sequence"].set("Valid and monotonic within the test session")
        self.integrity_vars["records"].set(str(len(records)))
        self.integrity_vars["source"].set(", ".join(source_formats))
        self.integrity_vars["raw"].set(str(raw_path))
        if spooled_count:
            self.integrity_vars["transport"].set(
                f"{spooled_count} record(s) preserved locally; {primary_count} reached the primary destination"
            )
        else:
            self.integrity_vars["transport"].set(
                f"{primary_count} record(s) reached the primary destination; backup spool was not needed"
            )

    @staticmethod
    def _short_timezone_label(value: datetime) -> str:
        """Return a compact local timezone label suitable for the GUI.

        Args:
            value: Timezone-aware local datetime.

        Returns:
            Short timezone label such as CDT, EST, UTC, or an available name.
        """

        name = value.tzname() or "Local"
        known = {
            "Coordinated Universal Time": "UTC",
            "Greenwich Mean Time": "GMT",
        }
        if name in known:
            return known[name]
        if len(name) <= 6 and " " not in name:
            return name
        words = [word for word in name.replace("-", " ").split() if word]
        if 2 <= len(words) <= 5:
            abbreviation = "".join(word[0].upper() for word in words if word[0].isalpha())
            if 2 <= len(abbreviation) <= 5:
                return abbreviation
        return name

    @staticmethod
    def _utc_to_local_display(value: str) -> str:
        """Convert a stored UTC timestamp into compact local display time.

        Args:
            value: UTC timestamp stored by the production database.

        Returns:
            Human-readable local date and time. The table omits the timezone
            because the column is explicitly labeled as local time.
        """

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            local = parsed.astimezone()
            return local.strftime("%b %d  %I:%M:%S %p")
        except (TypeError, ValueError):
            return value

    def _manual_refresh_dashboard(self) -> None:
        """Refresh the Production dashboard after an explicit user request.

        The button is disabled during the refresh so repeated clicks cannot stack
        multiple redraws. A visible timestamp is updated only after the database
        queries, tables, KPI cards, and charts have all been refreshed.
        """

        button = getattr(self, "refresh_dashboard_button", None)
        if button is not None:
            button.state(["disabled"])
        if hasattr(self, "dashboard_refresh_var"):
            self.dashboard_refresh_var.set("Refreshing...")
        self.update_idletasks()

        try:
            self._refresh_history()
        except Exception as exc:
            if hasattr(self, "dashboard_refresh_var"):
                self.dashboard_refresh_var.set("Refresh failed")
            messagebox.showerror(
                "Dashboard refresh failed",
                f"WearTest could not refresh the production dashboard.\n\n{exc}",
            )
        finally:
            if button is not None:
                button.state(["!disabled"])

    def _refresh_history(self) -> None:
        """Refresh production metrics, charts, station results, and recent sessions."""

        if not hasattr(self, "history_tree"):
            return

        summary = self.db.summary()
        self.summary_vars["total"].set(str(summary["total"]))
        self.summary_vars["fpy"].set(f"{summary['fpy_percent']:.1f}%")
        self.summary_vars["failed"].set(str(summary["failed"]))
        self.summary_vars["sessions"].set(str(summary["sessions"]))

        if hasattr(self, "tester_health_tree"):
            for item in self.tester_health_tree.get_children():
                self.tester_health_tree.delete(item)
            tester_rows = self.db.tester_health_summary()
            configured_stations = sorted(self.golden_config.station_drift)
            latest_by_station = {row["station_id"]: row for row in tester_rows}
            for station_id in configured_stations:
                row = latest_by_station.get(station_id)
                if row is None:
                    self.tester_health_tree.insert(
                        "", "end",
                        values=(station_id, "Not checked", 0, "-", "-", "-"),
                    )
                    continue
                status = row["health_status"]
                tag = (
                    "healthy" if status == "Healthy"
                    else "warning" if status == "Warning"
                    else "attention"
                )
                self.tester_health_tree.insert(
                    "", "end",
                    values=(
                        station_id, status, row["check_count"],
                        self._utc_to_local_display(row["created_utc"]),
                        row["worst_metric"], f"{100.0 * row['max_bias_ratio']:.0f}%",
                    ),
                    tags=(tag,),
                )

        for item in self.station_tree.get_children():
            self.station_tree.delete(item)
        for row in self.db.station_summary():
            self.station_tree.insert(
                "", "end",
                values=(
                    row["station_id"], row["tested"], row["passed"],
                    row["failed"], f"{row['fpy_percent']:.1f}%",
                ),
            )

        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for row in self.db.recent_sessions(limit=12):
            result = row["overall_result"]
            local_time = self._utc_to_local_display(row["created_utc"])
            self.history_tree.insert(
                "", "end",
                values=(local_time, row["device_id"], row["station_id"], result),
                tags=("pass" if result == "PASS" else "fail",),
            )

        self._refresh_production_charts()

        if hasattr(self, "dashboard_refresh_var"):
            refreshed_at = datetime.now().astimezone().strftime("%I:%M:%S %p")
            self.dashboard_refresh_var.set(f"Last refreshed {refreshed_at}")

    def _reload_limits(self) -> None:
        """Reload INI limits so edits take effect without restarting the GUI."""

        try:
            specs = load_test_specifications(self.config_path)
            self.engine = ManufacturingTestEngine(specs=specs)
            self._refresh_limit_summary()
            messagebox.showinfo("Test limits reloaded", "The updated acceptance limits are now active.")
        except Exception as exc:
            messagebox.showerror("Could not load test limits", str(exc))

    def _refresh_limit_summary(self) -> None:
        """Refresh the human-readable values shown on the Test Limits page."""

        if not hasattr(self, "limit_vars"):
            return
        specs = self.engine.specs
        values = {
            "battery": f"{specs.battery_voltage_min_v:.2f} to {specs.battery_voltage_max_v:.2f} V",
            "accel_bias": f"≤ {specs.accel_axis_bias_max_g:.3f} g",
            "accel_noise": f"≤ {specs.accel_noise_rms_max_g:.3f} g RMS",
            "gyro_bias": f"≤ {specs.gyro_bias_max_dps:.2f} deg/s",
            "gyro_noise": f"≤ {specs.gyro_noise_rms_max_dps:.2f} deg/s RMS",
            "ppg_snr": f"≥ {specs.ppg_snr_min_db:.1f} dB",
            "ppg_sat": f"≤ {100 * specs.ppg_saturation_max_fraction:.1f}%",
            "temp_ref": f"{specs.skin_temp_reference_c:.1f} °C",
            "temp_error": f"± {specs.skin_temp_error_max_c:.2f} °C",
            "ecg_impedance": f"≤ {specs.ecg_electrode_impedance_max_kohm:.0f} kΩ",
        }
        for key, value in values.items():
            self.limit_vars[key].set(value)

    def _update_clock(self) -> None:
        """Refresh the top-right clock using the computer's local timezone."""

        now = datetime.now().astimezone()
        zone = self._short_timezone_label(now)
        self.clock_var.set(now.strftime("%b %d, %Y | %I:%M %p") + f" {zone}")
        self.after(15000, self._update_clock)


def main() -> None:
    """Launch the WearTest desktop application."""

    app = WearTestApp()
    app.mainloop()


if __name__ == "__main__":
    main()
