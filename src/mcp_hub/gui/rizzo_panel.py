"""The built-in "Rizzo Flow / Jev" tab: state at a glance, install wizard,
start/stop, settings and the "use with Claude Code (Jev)" helper.

Nothing here asks for command/args/cwd/port-check URLs: the hub builds the
service from `RizzoSettings` (see `config.RizzoSettings.to_service`).
"""
from __future__ import annotations

import time
from dataclasses import asdict, replace
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QFrame, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QStyle, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from mcp_hub import rizzo_setup as rs
from mcp_hub.config import (
    BUILTIN_RIZZO, RIZZO_KV_TYPES, RIZZO_QUANTS, RizzoSettings, load_config, save_config,
)
from mcp_hub.rizzo_setup import Phase, StepState

_PHASE_COLOR = {
    Phase.CHECKING: "#757575", Phase.NOT_INSTALLED: "#757575", Phase.PARTIAL: "#ef6c00",
    Phase.INSTALLING: "#1565c0", Phase.INACTIVE: "#ef6c00", Phase.STOPPED: "#455a64",
    Phase.STARTING: "#f9a825", Phase.RUNNING: "#2e7d32", Phase.RUNNING_EXTERNAL: "#2e7d32",
    Phase.ERROR: "#c62828", Phase.HUB_DOWN: "#c62828",
}
# text marker (pending/running) or a standard style icon (done/failed/skipped):
# glyphs such as the check mark are missing from some fonts, style icons are not.
_STATE_ICON = {
    StepState.PENDING: ("○", None, "#757575"), StepState.RUNNING: ("▶", None, "#1565c0"),
    StepState.DONE: ("", QStyle.StandardPixmap.SP_DialogApplyButton, "#2e7d32"),
    StepState.FAILED: ("", QStyle.StandardPixmap.SP_DialogCancelButton, "#c62828"),
    StepState.SKIPPED: ("", QStyle.StandardPixmap.SP_DialogApplyButton, "#9e9e9e"),
}
_MODEL_LABEL = "rizzo-latest (Spark-X2.5-4B"


class _SetupWorker(QThread):
    """Runs the setup steps off the GUI thread; the runner's callbacks become signals."""
    step = Signal(str, str, str)          # step id, StepState value, detail
    progress = Signal(str, int, int, float)
    log = Signal(str)
    finished_run = Signal(bool)

    def __init__(self, runner_factory, options, env, force, parent=None):
        super().__init__(parent)
        self.runner = runner_factory(
            options, env,
            on_step=lambda s, st, d: self.step.emit(s, st.value, d),
            on_progress=lambda s, d, t, v: self.progress.emit(s, int(d), int(t), float(v)),
            on_log=self.log.emit,
        )
        self._force = frozenset(force)

    def run(self) -> None:
        try:
            ok = self.runner.run(force=self._force)
        except Exception as exc:  # never leave the UI waiting
            self.log.emit(f"[errore] {exc}")
            ok = False
        self.finished_run.emit(ok)


class _DetectWorker(QThread):
    """Detection (subprocess + disk + health) and server log, off the GUI thread."""
    done = Signal(object, object, object)  # Detection, persisted RizzoSettings, server log lines | None

    def __init__(self, client, form: RizzoSettings, install_dir: Path, want_log: bool, parent=None):
        super().__init__(parent)
        self._client, self._form, self._dir, self._want_log = client, form, install_dir, want_log

    def run(self) -> None:
        persisted = _read_persisted(self._client)
        effective = replace(self._form, installDir=persisted.installDir)
        env = rs.SetupEnv(load_settings=lambda: effective)
        detection = rs.detect(rs.SetupOptions(install_dir=self._dir), env)
        lines = None
        if self._want_log and detection.service_registered:
            try:
                lines = self._client.logs(BUILTIN_RIZZO)
            except Exception:
                lines = None
        self.done.emit(detection, persisted, lines)


class _ConnWorker(QThread):
    done = Signal(object)

    def __init__(self, port: int, parent=None):
        super().__init__(parent)
        self._port = port

    def run(self) -> None:
        self.done.emit(rs.check_connection(self._port))


def _read_persisted(client) -> RizzoSettings:
    try:
        return RizzoSettings(**client.rizzo()["settings"])
    except Exception:
        try:
            return load_config().rizzo
        except Exception:
            return RizzoSettings()


def save_settings(client, settings: dict) -> None:
    """Through the hub (it rebuilds the service); straight into config.json only
    when the hub cannot be reached. A validation error from the hub is raised."""
    import httpx
    try:
        client.set_rizzo(settings)
    except (httpx.TransportError, httpx.TimeoutException):
        config = load_config()
        config.rizzo = RizzoSettings(**settings)
        save_config(config)


class RizzoPanel(QWidget):
    state_changed = Signal(str)  # short text for the tab title

    def __init__(self, client, parent=None, *, initial: RizzoSettings | None = None,
                 runner_factory=rs.SetupRunner, auto_poll: bool = True):
        super().__init__(parent)
        self.client = client
        self._runner_factory = runner_factory
        self._detection: rs.Detection | None = None
        self._persisted = initial if initial is not None else self._load_initial()
        self._hub_info: dict | None = None
        self._hub_reachable = True
        self._installing = False
        self._setup_worker: _SetupWorker | None = None
        self._detect_worker: _DetectWorker | None = None
        self._conn_worker: _ConnWorker | None = None
        self._action_workers: list = []
        self._busy = False
        self._step_state: dict[str, tuple[StepState, str]] = {}
        self._current_step: str | None = None
        self._failed = False
        self._log_mode = "setup"
        self._log_chosen = False  # the user picked a log: stop auto-switching
        self._setup_log: list[str] = []
        self._server_log: list[str] = []
        self._last_health: tuple[float, bool] | None = None

        self._build_ui()
        self._load_form(self._persisted)
        self._render()

        self.timer = QTimer(self)
        # Only polls while the tab is actually on screen (nvidia-smi + disk + health
        # are not worth running every 3s in the background); one check at startup
        # keeps the tab title honest.
        self.timer.timeout.connect(lambda: self.isVisible() and self.refresh_detection())
        if auto_poll:
            self.timer.start(3000)
            QTimer.singleShot(500, self.refresh_detection)

    # ------------------------------------------------------------------ UI
    def _load_initial(self) -> RizzoSettings:
        try:
            return load_config().rizzo
        except Exception:
            return RizzoSettings()

    def _build_ui(self) -> None:
        # Everything lives in a scroll area: the tab must stay usable in a small window.
        content = QWidget()
        root = QVBoxLayout(content)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        # 1. state at a glance
        self.state_label = QLabel()
        self.state_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 4px 0;")
        self.details_label = QLabel()
        self.details_label.setWordWrap(True)
        self.details_label.setStyleSheet("color: #555;")
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("background-color: #fff3cd; padding: 4px;")
        self.warning_label.setVisible(False)
        root.addWidget(self.state_label)
        root.addWidget(self.details_label)
        root.addWidget(self.warning_label)

        # 2. actions
        row = QHBoxLayout()
        self.install_btn = QPushButton("Installa Rizzo Flow")
        self.start_btn = QPushButton("Avvia")
        self.stop_btn = QPushButton("Ferma")
        self.recheck_btn = QPushButton("Ricontrolla")
        self.repair_btn = QPushButton("Ripara")
        self.log_btn = QPushButton("Apri log del server")
        self.cancel_btn = QPushButton("Annulla")
        for b in (self.install_btn, self.start_btn, self.stop_btn, self.recheck_btn,
                  self.repair_btn, self.log_btn, self.cancel_btn):
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)
        self.install_btn.clicked.connect(lambda: self.start_install())
        self.start_btn.clicked.connect(self._start_server)
        self.stop_btn.clicked.connect(self._stop_server)
        self.recheck_btn.clicked.connect(self.refresh_detection)
        self.repair_btn.clicked.connect(self._repair)
        self.log_btn.clicked.connect(self._toggle_log)
        self.cancel_btn.clicked.connect(self._cancel)

        # 3. install steps
        self.install_box = QGroupBox("Installazione")
        ibox = QVBoxLayout(self.install_box)
        self.steps = QTreeWidget()
        self.steps.setColumnCount(3)
        self.steps.setHeaderLabels(["", "Passo", "Stato"])
        self.steps.setRootIsDecorated(False)
        self.steps.setMaximumHeight(210)
        self._step_items: dict[str, QTreeWidgetItem] = {}
        for spec in rs.STEP_SPECS:
            item = QTreeWidgetItem(["", spec.title, ""])
            self.steps.addTopLevelItem(item)
            self._step_items[spec.id] = item
        self.steps.setColumnWidth(0, 28)
        self.steps.setColumnWidth(1, 330)
        ibox.addWidget(self.steps)
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, len(rs.STEP_IDS))
        self.overall_bar.setFormat("%v / %m passi")
        self.current_label = QLabel("")
        self.current_bar = QProgressBar()
        self.current_bar.setRange(0, 1000)
        ibox.addWidget(self.overall_bar)
        ibox.addWidget(self.current_label)
        ibox.addWidget(self.current_bar)
        root.addWidget(self.install_box)

        # 4. settings
        self.settings_toggle = QToolButton()
        self.settings_toggle.setText("Impostazioni")
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.settings_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_toggle.setAutoRaise(True)
        self.settings_box = QGroupBox()
        form = QFormLayout()
        folder_row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        browse = QPushButton("Sfoglia...")
        browse.clicked.connect(self._browse_dir)
        folder_row.addWidget(self.dir_edit, 1)
        folder_row.addWidget(browse)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.quant_combo = QComboBox()
        self.quant_combo.addItems(RIZZO_QUANTS)
        self.ctx_spin = QSpinBox()
        self.ctx_spin.setRange(2048, 262144)
        self.ctx_spin.setSingleStep(1024)
        self.kv_combo = QComboBox()
        self.kv_combo.addItems(RIZZO_KV_TYPES)
        self.autostart_check = QCheckBox("Avvia Rizzo Flow insieme all'hub")
        form.addRow("Cartella di installazione", folder_row)
        form.addRow("Porta", self.port_spin)
        form.addRow("Quantizzazione pesi", self.quant_combo)
        form.addRow("Contesto (--ctx)", self.ctx_spin)
        form.addRow("Cache KV", self.kv_combo)
        form.addRow("", self.autostart_check)
        btns = QHBoxLayout()
        self.save_btn = QPushButton("Salva impostazioni")
        self.defaults_btn = QPushButton("Valori predefiniti")
        self.save_btn.clicked.connect(self._save_settings)
        self.defaults_btn.clicked.connect(lambda: self._load_form(
            RizzoSettings(installDir=self._persisted.installDir)))
        btns.addWidget(self.save_btn)
        btns.addWidget(self.defaults_btn)
        btns.addStretch(1)
        sbox = QVBoxLayout(self.settings_box)
        sbox.addLayout(form)
        sbox.addLayout(btns)
        hint = QLabel("Porta, contesto e cache KV diventano parametri del server al prossimo avvio.")
        hint.setStyleSheet("color: #666;")
        sbox.addWidget(hint)
        self.settings_box.setVisible(False)
        self.settings_toggle.toggled.connect(self._toggle_settings)
        root.addWidget(self.settings_toggle)
        root.addWidget(self.settings_box)

        # 5. Jev
        self.jev_box = QGroupBox("Usa con Claude Code (Jev)")
        jbox = QVBoxLayout(self.jev_box)
        test_row = QHBoxLayout()
        self.test_btn = QPushButton("Prova la connessione")
        self.test_label = QLabel("Invia una domanda di prova a Rizzo, come fa il plugin.")
        self.test_label.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_label, 1)
        jbox.addLayout(test_row)
        self.test_btn.clicked.connect(self._test_connection)
        jbox.addWidget(QLabel("1. Installa il plugin in Claude Code (a mano, dentro Claude Code):"))
        self.cmd_edits: list[QLineEdit] = []
        for cmd in (rs.PLUGIN_MARKETPLACE_CMD, rs.PLUGIN_INSTALL_CMD):
            r = QHBoxLayout()
            edit = QLineEdit(cmd)
            edit.setReadOnly(True)
            copy = QPushButton("Copia")
            copy.clicked.connect(lambda _=False, e=edit: self._copy(e.text()))
            r.addWidget(edit, 1)
            r.addWidget(copy)
            jbox.addLayout(r)
            self.cmd_edits.append(edit)
        jbox.addWidget(QLabel("2. Imposta la userConfig del plugin:"))
        self.config_text = QPlainTextEdit()
        self.config_text.setReadOnly(True)
        self.config_text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.config_text.setMaximumHeight(118)
        jbox.addWidget(self.config_text)
        copy_cfg = QPushButton("Copia configurazione")
        copy_cfg.clicked.connect(lambda: self._copy(self.config_text.toPlainText()))
        self.copy_cfg_btn = copy_cfg
        jbox.addWidget(copy_cfg)
        self.jev_note = QLabel("mcp-hub non modifica i settings.json di Claude Code: il plugin si installa a mano.")
        self.jev_note.setStyleSheet("color: #666;")
        self.jev_note.setWordWrap(True)
        jbox.addWidget(self.jev_note)
        root.addWidget(self.jev_box)

        # 6. log
        self.log_label = QLabel("Log installazione")
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(5000)
        self.log_text.setMinimumHeight(170)
        root.addWidget(self.log_label)
        root.addWidget(self.log_text, 1)

    # ------------------------------------------------------------- settings
    def _load_form(self, s: RizzoSettings) -> None:
        self.dir_edit.setText(s.installDir or str(rs.DEFAULT_INSTALL_DIR))
        self.port_spin.setValue(s.port)
        self.quant_combo.setCurrentText(s.quant)
        self.ctx_spin.setValue(s.ctx)
        self.kv_combo.setCurrentText(s.kvType)
        self.autostart_check.setChecked(s.autostart)

    def form_settings(self) -> RizzoSettings:
        """The form as settings; installDir is the *persisted* one (None until activated)."""
        return RizzoSettings(
            installDir=self._persisted.installDir, port=self.port_spin.value(),
            quant=self.quant_combo.currentText(), ctx=self.ctx_spin.value(),
            kvType=self.kv_combo.currentText(), autostart=self.autostart_check.isChecked(),
        )

    def install_dir(self) -> Path:
        text = self.dir_edit.text().strip()
        return Path(text) if text else rs.DEFAULT_INSTALL_DIR

    def _toggle_settings(self, open_: bool) -> None:
        self.settings_box.setVisible(open_)
        self.settings_toggle.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Cartella di installazione", self.dir_edit.text())
        if path:
            self.dir_edit.setText(path)

    def _save_settings(self) -> None:
        settings = self.form_settings()
        running = self._hub_status() in ("starting", "running")
        if self._persisted.installDir is None:
            # Not activated yet: the settings are stored when the install activates Rizzo.
            QMessageBox.information(self, "Impostazioni",
                                    "Le impostazioni saranno salvate quando Rizzo Flow verra' attivato dall'installazione.")
            return
        if running and QMessageBox.question(
                self, "Impostazioni",
                "Salvando le impostazioni il server in esecuzione viene fermato. Continuare?",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            save_settings(self.client, asdict(settings))
        except Exception as exc:
            QMessageBox.warning(self, "Impostazioni", f"Impostazioni non valide o non salvate: {exc}")
            return
        self._persisted = settings
        self.refresh_detection()

    # ---------------------------------------------------------------- state
    def _hub_status(self) -> str | None:
        return self._hub_info["status"] if self._hub_info else None

    def phase(self) -> Phase:
        return rs.derive_phase(self._detection, self._hub_status(), self._hub_reachable, self._installing)

    def on_hub_status(self, info: dict | None, reachable: bool = True) -> None:
        """Pushed by the main window on every status tick."""
        before = self._hub_status()
        self._hub_info = info
        self._hub_reachable = reachable
        after = self._hub_status()
        if after != before:
            if (self._detection is not None and after in ("stopped", "crashed")
                    and before in ("starting", "running")):
                # The hub itself just stopped it: the last health check is stale.
                self._detection.server_running = False
            if not self._installing:
                self.refresh_detection()
        self._render()

    def refresh_detection(self) -> None:
        if self._installing or (self._detect_worker is not None and self._detect_worker.isRunning()):
            return
        self._detect_worker = _DetectWorker(
            self.client, self.form_settings(), self.install_dir(), self._log_mode == "server", self)
        self._detect_worker.done.connect(self.apply_detection)
        self._detect_worker.start()

    def apply_detection(self, detection: rs.Detection, persisted: RizzoSettings, server_log=None) -> None:
        self._detection = detection
        self._persisted = persisted
        self._last_health = (time.time(), detection.server_running)
        if server_log is not None:
            self._server_log = list(server_log)
        if not self._installing and not self._failed:
            for spec in rs.STEP_SPECS:
                st = detection.steps[spec.id]
                if st.done:
                    self._step_state[spec.id] = (StepState.SKIPPED, "gia' presente" if spec.id != "plugin_config"
                                                 else st.detail)
                else:
                    self._step_state[spec.id] = (StepState.PENDING, st.detail)
        self._render()

    def _render(self) -> None:
        phase = self.phase()
        d = self._detection
        if not self._log_chosen and not self._installing:
            active = d is not None and d.service_registered and d.rizzo_ready
            self._log_mode = "server" if active else "setup"
        label = rs.PHASE_LABELS[phase]
        if phase == Phase.INSTALLING and self._current_step:
            title = next(s.title for s in rs.STEP_SPECS if s.id == self._current_step)
            n = sum(1 for st, _ in self._step_state.values() if st in (StepState.DONE, StepState.SKIPPED))
            label = f"{label}: {title} ({n}/{len(rs.STEP_IDS)})"
        self.state_label.setText(label)
        self.state_label.setStyleSheet(
            f"font-size: 16px; font-weight: bold; padding: 4px 0; color: {_PHASE_COLOR[phase]};")

        s = self.form_settings()
        parts = []
        if d is not None and d.gpu_name:
            parts.append(f"GPU: {d.gpu_name} ({d.gpu_mib} MiB)")
        elif d is not None:
            parts.append("GPU: non rilevata")
        parts += [f"Porta {s.port}", f"Modello {_MODEL_LABEL} {s.quant})", f"ctx {s.ctx}", f"KV {s.kvType}"]
        if self._last_health is not None and d is not None and d.service_registered:
            when = time.strftime("%H:%M:%S", time.localtime(self._last_health[0]))
            parts.append(f"ultimo health check {when}: {'OK' if self._last_health[1] else 'nessuna risposta'}")
        self.details_label.setText("  ·  ".join(parts))
        warnings = list(d.warnings) if d else []
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))

        installed_ok = d is not None and d.rizzo_ready
        active = d is not None and d.service_registered and d.rizzo_ready
        hub = self._hub_status()
        busy = self._busy
        self.install_btn.setVisible(not self._installing and d is not None and not d.complete)
        self.install_btn.setText({
            Phase.NOT_INSTALLED: "Installa Rizzo Flow", Phase.PARTIAL: "Riprendi installazione",
        }.get(phase, "Completa installazione"))
        self.cancel_btn.setVisible(self._installing)
        self.start_btn.setVisible(active)
        self.stop_btn.setVisible(active)
        self.start_btn.setEnabled(active and not busy and self._hub_reachable and hub in ("stopped", "crashed", None)
                                  and phase != Phase.RUNNING_EXTERNAL)
        self.stop_btn.setEnabled(active and not busy and hub in ("starting", "running"))
        self.recheck_btn.setEnabled(not self._installing)
        self.repair_btn.setVisible(installed_ok and not self._installing)
        self.log_btn.setText("Mostra log installazione" if self._log_mode == "server" else "Apri log del server")
        self.log_btn.setEnabled(active or self._log_mode == "server")
        self.install_box.setVisible(
            self._installing or self._failed or d is None or not (d.complete and phase != Phase.INSTALLING))

        self._render_steps()
        self._render_log()
        self.config_text.setPlainText(rs.plugin_config_lines(s.port))
        self.test_btn.setEnabled(self._conn_worker is None or not self._conn_worker.isRunning())
        self.state_changed.emit(self.tab_title())

    def tab_title(self) -> str:
        short = {
            Phase.RUNNING: "in esecuzione", Phase.RUNNING_EXTERNAL: "in esecuzione", Phase.STOPPED: "fermo",
            Phase.STARTING: "in avvio", Phase.ERROR: "errore", Phase.INSTALLING: "installazione",
            Phase.NOT_INSTALLED: "da installare", Phase.PARTIAL: "incompleto", Phase.INACTIVE: "da attivare",
            Phase.HUB_DOWN: "hub offline", Phase.CHECKING: "...",
        }[self.phase()]
        return f"Rizzo Flow / Jev — {short}"

    def _render_steps(self) -> None:
        done = 0
        for spec in rs.STEP_SPECS:
            state, detail = self._step_state.get(spec.id, (StepState.PENDING, ""))
            text, std_icon, color = _STATE_ICON[state]
            item = self._step_items[spec.id]
            item.setText(0, text)
            item.setIcon(0, self.style().standardIcon(std_icon) if std_icon is not None else QIcon())
            item.setData(0, Qt.ItemDataRole.UserRole, state.value)
            item.setText(2, detail or {StepState.PENDING: "da fare", StepState.RUNNING: "in corso..."}.get(state, ""))
            item.setForeground(0, QColor(color))
            item.setForeground(2, QColor(color if state in (StepState.FAILED, StepState.RUNNING) else "#555555"))
            if state in (StepState.DONE, StepState.SKIPPED):
                done += 1
        self.overall_bar.setValue(done)

    def _render_log(self) -> None:
        lines = self._server_log if self._log_mode == "server" else self._setup_log
        text = "\n".join(lines)
        self.log_label.setText("Log del server" if self._log_mode == "server" else "Log installazione")
        if text != self.log_text.toPlainText():
            bar = self.log_text.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum() - 2
            prev = bar.value()
            self.log_text.setPlainText(text)
            bar.setValue(bar.maximum() if at_bottom else prev)

    def _toggle_log(self) -> None:
        self._log_chosen = True
        self._log_mode = "setup" if self._log_mode == "server" else "server"
        self.refresh_detection()
        self._render_log()
        self._render()

    # ------------------------------------------------------------- install
    def _env(self) -> rs.SetupEnv:
        form = self.form_settings()
        persisted_dir = self._persisted.installDir
        return rs.SetupEnv(
            load_settings=lambda: replace(form, installDir=_read_persisted(self.client).installDir or persisted_dir),
            register=lambda d: save_settings(self.client, d),
        )

    def start_install(self, force=frozenset()) -> None:
        if self._installing:
            return
        self._installing = True
        self._failed = False
        self._current_step = None
        self._step_state = {sid: (StepState.PENDING, "") for sid in rs.STEP_IDS}
        self._setup_log.append(f"--- {time.strftime('%H:%M:%S')} avvio installazione in {self.install_dir()} ---")
        self._log_mode = "setup"
        self._log_chosen = False
        self.settings_toggle.setChecked(False)
        worker = _SetupWorker(self._runner_factory, rs.SetupOptions(install_dir=self.install_dir()),
                              self._env(), force, self)
        worker.step.connect(self._on_step)
        worker.progress.connect(self._on_progress)
        worker.log.connect(self._on_log)
        worker.finished_run.connect(self._on_finished)
        self._setup_worker = worker
        self._render()
        worker.start()

    def _repair(self) -> None:
        if QMessageBox.question(
            self, "Ripara",
            "Rilancia uv sync, npm install e l'attivazione nell'hub (non riscarica i pesi).\n\nContinuare?",
        ) == QMessageBox.StandardButton.Yes:
            self.start_install(force=rs.REPAIR_STEPS)

    def _cancel(self) -> None:
        if self._setup_worker is not None:
            self._on_log("Annullamento in corso...")
            self._setup_worker.runner.cancel()

    def _on_step(self, step_id: str, state: str, detail: str) -> None:
        st = StepState(state)
        self._step_state[step_id] = (st, detail)
        if st == StepState.RUNNING:
            self._current_step = step_id
            spec_title = next(s.title for s in rs.STEP_SPECS if s.id == step_id)
            self.current_label.setText(spec_title + "...")
            self.current_bar.setRange(0, 0)  # busy until the step reports a size
        elif st == StepState.FAILED:
            self._failed = True
            self.current_label.setText(f"Passo fallito: {detail}")
            self.current_bar.setRange(0, 1000)
            self.current_bar.setValue(0)
        elif st in (StepState.DONE, StepState.SKIPPED):
            self.current_bar.setRange(0, 1000)
            self.current_bar.setValue(1000)
        self._render()

    def _on_progress(self, step_id: str, done: int, total: int, speed: float) -> None:
        if total <= 0:
            self.current_bar.setRange(0, 0)
            return
        self.current_bar.setRange(0, 1000)
        self.current_bar.setValue(int(1000 * min(done, total) / total))
        self.current_label.setText(rs.format_progress("Download pesi e runtime", done, total, speed))

    def _on_log(self, line: str) -> None:
        self._setup_log.append(line)
        if self._log_mode == "setup":
            self.log_text.appendPlainText(line)

    def _on_finished(self, ok: bool) -> None:
        self._installing = False
        self._current_step = None
        if ok:
            self.current_label.setText("Installazione completata.")
            self.current_bar.setRange(0, 1000)
            self.current_bar.setValue(1000)
            self._failed = False
        self._render()
        self._persisted = _read_persisted(self.client)
        self.refresh_detection()

    # ------------------------------------------------------ start / stop
    def _start_server(self) -> None:
        self._run_action("start")

    def _stop_server(self) -> None:
        self._run_action("stop")

    def _run_action(self, which: str) -> None:
        from mcp_hub.gui.main_window import _ActionWorker
        if self._busy:
            return
        self._busy = True
        action = getattr(self.client, which)
        worker = _ActionWorker(action, BUILTIN_RIZZO, self)
        worker.finished_ok.connect(self._on_action_result)
        self._action_workers.append(worker)
        worker.start()
        self._render()

    def _on_action_result(self, name: str, success: bool, error: str) -> None:
        self._busy = False
        self._action_workers = [w for w in self._action_workers if w.isRunning()]
        if not success:
            QMessageBox.warning(self, "Rizzo Flow", f"Operazione fallita: {error}")
        self._render()
        self.refresh_detection()

    # ------------------------------------------------------------------ Jev
    def _copy(self, text: str) -> None:
        QApplication.clipboard().setText(text)

    def _test_connection(self) -> None:
        if self._conn_worker is not None and self._conn_worker.isRunning():
            return
        self.test_label.setText("Prova in corso...")
        self._conn_worker = _ConnWorker(self.port_spin.value(), self)
        self._conn_worker.done.connect(self.show_connection_result)
        self._conn_worker.start()
        self.test_btn.setEnabled(False)

    def show_connection_result(self, result: rs.ConnectionResult) -> None:
        self.test_label.setText(result.summary())
        self.test_label.setStyleSheet(f"color: {'#2e7d32' if result.ok else '#c62828'};")
        self.test_btn.setEnabled(True)
