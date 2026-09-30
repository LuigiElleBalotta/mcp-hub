"""Setup of the built-in Rizzo Flow server + the fast-jev-compaction plugin fork.

GUI independent. Same layout, defaults and branches as
`team-skills/scripts/setup-rizzo-compaction.ps1`. Everything that touches the
outside world (commands, network, disk, hub settings) goes through `SetupEnv`,
so the logic is unit-testable with fakes.

Never writes `~/.claude*/settings.json`: the plugin userConfig is only built
as text for the user to copy.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from mcp_hub.config import BUILTIN_RIZZO, RizzoSettings

SERVICE_NAME = BUILTIN_RIZZO
DEFAULT_INSTALL_DIR = (
    Path("C:/repositories/mindicity/utils/claude-code/rizzo-compaction")
    if sys.platform == "win32" else Path.home() / "rizzo-compaction"
)
RIZZO_URL = "https://github.com/LuigiElleBalotta/rizzo-flow.git"
JEV_URL = "https://github.com/LuigiElleBalotta/fast-jev-compaction.git"
BRANCH = "custom"
PORT = 8017
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
GGUF_NAME = "spark-x2.5-4b-rizzo-flow-lora-q4_k_m.gguf"
# Measured on the pinned revisions (rizzo-flow b11081 CUDA runtime + q4_k_m weights).
WEIGHTS_BYTES = 2_600_224_416
RUNTIME_BYTES = 1_314_091_066
EXPECTED_DOWNLOAD_BYTES = WEIGHTS_BYTES + RUNTIME_BYTES
# q8_0 was never downloaded here: its size is an estimate, used only for the progress bar.
_WEIGHTS_BY_QUANT = {"q4_k_m": WEIGHTS_BYTES, "q8_0": 4_500_000_000}
MIN_FREE_BYTES = 6 * 1024**3
MIN_VRAM_MIB = 6000
REQUIRED_TOOLS = ("git", "uv", "node", "npm")
FUNCTION_HOOKS_ENV = "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1"
PLUGIN_MARKETPLACE_CMD = "/plugin marketplace add LuigiElleBalotta/fast-jev-compaction"
PLUGIN_INSTALL_CMD = "/plugin install fast-jev-compaction@fast-jev-compaction"


def gguf_name(quant: str) -> str:
    return f"spark-x2.5-4b-rizzo-flow-lora-{quant}.gguf"


def weights_bytes(quant: str) -> int:
    return _WEIGHTS_BY_QUANT.get(quant, WEIGHTS_BYTES)


def expected_download_bytes(quant: str) -> int:
    return weights_bytes(quant) + RUNTIME_BYTES


def download_args(quant: str) -> list[str]:
    return ["run", "rizzo", "download", "--size", "4b", "--quant", quant, "--weights", "flow"]


class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # already satisfied before the step ran


@dataclass(frozen=True)
class StepSpec:
    id: str
    title: str


STEP_SPECS: tuple[StepSpec, ...] = (
    StepSpec("prereqs", "Prerequisiti (git, uv, node, npm, GPU)"),
    StepSpec("clone_rizzo", "Clona rizzo-flow (server locale)"),
    StepSpec("uv_sync", "Dipendenze Python (uv sync)"),
    StepSpec("download", "Scarica pesi e runtime (~3,9 GB)"),
    StepSpec("clone_jev", "Clona fast-jev-compaction (plugin)"),
    StepSpec("npm_install", "Dipendenze del plugin (npm install)"),
    StepSpec("register_service", "Attiva Rizzo Flow nell'hub"),
    StepSpec("plugin_config", "Configurazione del plugin da copiare"),
)
STEP_IDS = tuple(s.id for s in STEP_SPECS)
# Steps a "repair" re-runs even when they look done (never the big download).
REPAIR_STEPS = frozenset({"uv_sync", "npm_install", "register_service"})


class SetupError(Exception):
    """A step failed; the message is shown to the user."""


class SetupCancelled(SetupError):
    pass


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------

def format_bytes(n: float) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    return f"{n / 1024:.0f} KB"


def format_progress(label: str, done: int, total: int, speed: float = 0.0) -> str:
    text = f"{label}: {format_bytes(done)} / {format_bytes(total)}"
    if total:
        text += f" ({min(100, 100 * done / total):.0f}%)"
    if speed > 0:
        text += f", {format_bytes(speed)}/s"
    return text


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def plugin_user_config(port: int = PORT) -> dict:
    return {
        "baseUrl": f"http://127.0.0.1:{port}/v1/systemone",
        "model": "rizzo-latest",
        "apiKey": "rizzo-local",
        "maxStateTokens": 6000,
        "maxRequestTokens": 14000,
        "maxQuestionsPerRequest": 64,
    }


def plugin_config_lines(port: int = PORT) -> str:
    cfg = plugin_user_config(port)
    width = max(len(k) for k in cfg)
    return "\n".join(f"{k.ljust(width)} = {v}" for k, v in cfg.items())


def plugin_instructions(port: int = PORT) -> str:
    return (
        f"Prima avvia Claude Code con la variabile {FUNCTION_HOOKS_ENV} (funzione sperimentale:\n"
        "senza, il plugin risulta installato ma non parte mai; riavvia le sessioni gia' aperte).\n\n"
        "Installa il plugin da Claude Code:\n"
        f"  {PLUGIN_MARKETPLACE_CMD}\n"
        f"  {PLUGIN_INSTALL_CMD}\n\n"
        "poi imposta la userConfig del plugin (la chiave e' un segnaposto: Rizzo la\n"
        "controlla solo se RIZZO_API_KEY e' impostata; maxRequestTokens deve restare\n"
        "sotto --ctx del server):\n\n  " + plugin_config_lines(port).replace("\n", "\n  ") + "\n"
    )


# --------------------------------------------------------------------------
# environment (everything outside the process goes through here)
# --------------------------------------------------------------------------

def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _default_query(args: list[str]) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10, creationflags=_no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _default_health(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:  # noqa: S310 (local URL)
            return 200 <= resp.status < 300
    except Exception:
        return False


def _default_disk_free(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _default_load_settings() -> RizzoSettings:
    from mcp_hub.config import load_config
    return load_config().rizzo


def _default_run_command(
    args: list[str], cwd: Path, on_line: Callable[[str], None], on_start: Callable[[int], None],
) -> int:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.Popen(
        args, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        creationflags=_no_window(),
    )
    on_start(proc.pid)
    assert proc.stdout is not None
    for line in proc.stdout:  # universal newlines: "\r" progress updates arrive as lines
        line = line.rstrip()
        if line:
            on_line(line)
    return proc.wait()


def _default_kill_tree(pid: int) -> None:
    import psutil
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


@dataclass
class SetupEnv:
    which: Callable[[str], str | None] = shutil.which
    query: Callable[[list[str]], str | None] = _default_query
    disk_free: Callable[[Path], int] = _default_disk_free
    dir_size: Callable[[Path], int] = dir_size
    health: Callable[[str], bool] = _default_health
    load_settings: Callable[[], RizzoSettings] = _default_load_settings
    # settings dict -> None; raises on failure. The GUI wires this to the hub API
    # (PUT /api/rizzo), falling back to config.json when the hub is unreachable.
    register: Callable[[dict], None] | None = None
    run_command: Callable[..., int] = _default_run_command
    kill_tree: Callable[[int], None] = _default_kill_tree
    platform: str = sys.platform


@dataclass
class SetupOptions:
    install_dir: Path | None = None  # None: installDir of the hub settings, else the default
    rizzo_url: str = RIZZO_URL
    jev_url: str = JEV_URL
    branch: str = BRANCH


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

@dataclass
class Prereq:
    name: str
    found: bool
    detail: str = ""
    required: bool = True


@dataclass
class StepStatus:
    done: bool
    detail: str = ""


@dataclass
class Detection:
    install_dir: Path
    prereqs: list[Prereq]
    gpu_name: str | None
    gpu_mib: int | None
    steps: dict[str, StepStatus]
    server_running: bool
    service_registered: bool
    settings: RizzoSettings = field(default_factory=RizzoSettings)
    warnings: list[str] = field(default_factory=list)

    @property
    def rizzo_dir(self) -> Path:
        return self.install_dir / "rizzo-flow"

    @property
    def jev_dir(self) -> Path:
        return self.install_dir / "fast-jev-compaction"

    @property
    def rizzo_ready(self) -> bool:
        """The server itself is installed on disk."""
        return all(self.steps[s].done for s in ("clone_rizzo", "uv_sync", "download"))

    @property
    def complete(self) -> bool:
        return all(self.steps[s].done for s in STEP_IDS if s != "prereqs")

    @property
    def missing(self) -> list[str]:
        return [s for s in STEP_IDS if not self.steps[s].done]

    @property
    def prereqs_ok(self) -> bool:
        return all(p.found for p in self.prereqs if p.required)

    @property
    def any_installed(self) -> bool:
        return any(self.steps[s].done for s in ("clone_rizzo", "uv_sync", "download"))


def _paths(install_dir: Path, quant: str = "q4_k_m") -> dict[str, Path]:
    rizzo = install_dir / "rizzo-flow"
    jev = install_dir / "fast-jev-compaction"
    return {
        "rizzo": rizzo,
        "jev": jev,
        "venv_python": rizzo.joinpath(".venv", *(("Scripts", "python.exe") if os.name == "nt" else ("bin", "python"))),
        "gguf": rizzo / "models" / "rizzo-flow" / gguf_name(quant),
        "runtimes": rizzo / "runtimes",
        "node_modules": jev / "node_modules",
    }


def _runtime_present(runtimes: Path) -> bool:
    if not runtimes.exists():
        return False
    return any(p.is_dir() and p.name.startswith("llama-") for p in runtimes.glob("*"))


def _weights_present(gguf: Path, quant: str = "q4_k_m") -> bool:
    try:
        return gguf.stat().st_size >= weights_bytes(quant) * 0.99
    except OSError:
        return False


def resolve_install_dir(options: SetupOptions, env: SetupEnv) -> Path:
    if options.install_dir is not None:
        return Path(options.install_dir)
    try:
        configured = env.load_settings().installDir
    except Exception:
        configured = None
    return Path(configured) if configured else DEFAULT_INSTALL_DIR


def _detect_mac_gpu(env: "SetupEnv", prereqs: list, warnings: list) -> tuple[str | None, int | None]:
    """Apple Silicon runs the model on the GPU through Metal with unified memory;
    an Intel Mac has no usable GPU for it (CPU only, slow)."""
    chip = (env.query(["sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip() or "Mac"
    arch = (env.query(["uname", "-m"]) or "").strip()
    mem = (env.query(["sysctl", "-n", "hw.memsize"]) or "").strip()
    gib = int(mem) // 1024**3 if mem.isdigit() else None
    if arch == "arm64":
        detail = f"{chip}, {gib} GB di memoria unificata" if gib else chip
        prereqs.append(Prereq("GPU (Metal)", True, detail, required=False))
        if gib is not None and gib < 8:
            warnings.append(f"Memoria {gib} GB: il server usa circa 4,1 GB, potrebbe non starci.")
        return chip, (gib * 1024 if gib else None)
    warnings.append("Mac Intel: nessuna GPU utilizzabile, il server gira su CPU e sara' lento.")
    prereqs.append(Prereq("GPU (Metal)", False, "Mac Intel: solo CPU", required=False))
    return chip, None


def detect(options: SetupOptions | None = None, env: SetupEnv | None = None) -> Detection:
    options = options or SetupOptions()
    env = env or SetupEnv()
    install_dir = resolve_install_dir(options, env)
    try:
        settings = env.load_settings()
    except Exception:
        settings = RizzoSettings()
    p = _paths(install_dir, settings.quant)

    prereqs: list[Prereq] = []
    for tool in REQUIRED_TOOLS:
        path = env.which(tool)
        prereqs.append(Prereq(tool, path is not None, path or "non trovato nel PATH"))

    gpu_name: str | None = None
    gpu_mib: int | None = None
    warnings: list[str] = []
    if env.platform == "darwin":
        gpu_name, gpu_mib = _detect_mac_gpu(env, prereqs, warnings)
    else:
        smi = env.which("nvidia-smi")
        out = env.query([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]) if smi else None
        if out and out.strip():
            parts = [x.strip() for x in out.strip().splitlines()[0].split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                gpu_name, gpu_mib = parts[0], int(parts[1])
        if gpu_mib is None:
            warnings.append("GPU NVIDIA non rilevata (nvidia-smi): il server e' pensato per CUDA.")
            prereqs.append(Prereq("GPU NVIDIA", False, "non rilevata", required=False))
        elif gpu_mib < MIN_VRAM_MIB:
            warnings.append(f"VRAM {gpu_mib} MiB < 6 GB: il server usa circa 4,1 GB, potrebbe non starci.")
            prereqs.append(Prereq("GPU NVIDIA", True, f"{gpu_name}, {gpu_mib} MiB (pochi)", required=False))
        else:
            prereqs.append(Prereq("GPU NVIDIA", True, f"{gpu_name}, {gpu_mib} MiB", required=False))

    registered = bool(settings.installDir) and Path(settings.installDir) == install_dir
    weights = _weights_present(p["gguf"], settings.quant)
    runtime = _runtime_present(p["runtimes"])
    if weights and runtime:
        download_detail = "pesi e runtime presenti"
    elif runtime:
        download_detail = "mancano i pesi"
    elif weights:
        download_detail = "manca il runtime"
    else:
        download_detail = "da scaricare"
    required_ok = all(x.found for x in prereqs if x.required)
    steps = {
        "prereqs": StepStatus(required_ok, "ok" if required_ok else
                              "mancano: " + ", ".join(x.name for x in prereqs if x.required and not x.found)),
        "clone_rizzo": StepStatus((p["rizzo"] / ".git").exists(), str(p["rizzo"])),
        "uv_sync": StepStatus(p["venv_python"].exists(), ".venv presente" if p["venv_python"].exists() else "da fare"),
        "download": StepStatus(weights and runtime, download_detail),
        "clone_jev": StepStatus((p["jev"] / ".git").exists(), str(p["jev"])),
        "npm_install": StepStatus(p["node_modules"].exists(),
                                  "node_modules presente" if p["node_modules"].exists() else "da fare"),
        "register_service": StepStatus(registered, "attivo nell'hub" if registered else "non ancora attivato"),
    }
    steps["plugin_config"] = StepStatus(
        steps["clone_jev"].done and steps["npm_install"].done and registered,
        "configurazione pronta" if steps["clone_jev"].done else "disponibile dopo il clone del plugin",
    )
    return Detection(
        install_dir=install_dir, prereqs=prereqs, gpu_name=gpu_name, gpu_mib=gpu_mib, steps=steps,
        server_running=env.health(f"http://127.0.0.1:{settings.port}/health"),
        service_registered=registered, settings=settings, warnings=warnings,
    )


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

def _noop(*_a, **_k) -> None:
    return None


class SetupRunner:
    """Runs the missing steps in order; meant to live in a worker thread.

    Events: `on_step(step_id, StepState, detail)`, `on_progress(step_id, done,
    total, bytes_per_s)` (total 0 = indeterminate), `on_log(line)`.
    """

    def __init__(
        self,
        options: SetupOptions | None = None,
        env: SetupEnv | None = None,
        on_step: Callable[[str, StepState, str], None] = _noop,
        on_progress: Callable[[str, int, int, float], None] = _noop,
        on_log: Callable[[str], None] = _noop,
        poll_interval: float = 1.0,
    ):
        self.options = options or SetupOptions()
        self.env = env or SetupEnv()
        self.on_step, self.on_progress, self.on_log = on_step, on_progress, on_log
        self.poll_interval = poll_interval
        self._cancelled = threading.Event()
        self._pid: int | None = None
        self._lock = threading.Lock()
        self.install_dir = resolve_install_dir(self.options, self.env)
        try:
            self.settings = self.env.load_settings()
        except Exception:
            self.settings = RizzoSettings()
        self.p = _paths(self.install_dir, self.settings.quant)
        self.expected = expected_download_bytes(self.settings.quant)

    # -- control ----------------------------------------------------------
    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            pid = self._pid
        if pid is not None:
            self.env.kill_tree(pid)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def run(self, force: frozenset[str] | set[str] = frozenset()) -> bool:
        """True when every step ended done/skipped."""
        for spec in STEP_SPECS:
            if self.cancelled:
                self._fail(spec.id, "annullato dall'utente")
                return False
            always_runs = spec.id in ("prereqs", "plugin_config")
            if not always_runs and spec.id not in force and self._is_done(spec.id):
                self.on_step(spec.id, StepState.SKIPPED, "gia' presente")
                continue
            self.on_step(spec.id, StepState.RUNNING, "")
            self.on_progress(spec.id, 0, 0, 0.0)
            try:
                detail = getattr(self, f"_step_{spec.id}")()
                if not always_runs and not self._is_done(spec.id):
                    raise SetupError("il passo e' terminato ma il risultato non e' sul disco")
            except SetupCancelled:
                self._fail(spec.id, "annullato dall'utente")
                return False
            except SetupError as exc:
                self._fail(spec.id, str(exc))
                return False
            except Exception as exc:  # unexpected: still must not kill the worker silently
                self._fail(spec.id, f"errore inatteso: {exc}")
                return False
            self.on_step(spec.id, StepState.DONE, detail or "")
        return True

    def _fail(self, step_id: str, detail: str) -> None:
        self.on_log(f"[errore] {detail}")
        self.on_step(step_id, StepState.FAILED, detail)

    # -- checks -------------------------------------------------------------
    def _is_done(self, step_id: str) -> bool:
        p = self.p
        if step_id == "prereqs":
            return all(self.env.which(t) for t in REQUIRED_TOOLS)
        if step_id == "clone_rizzo":
            return (p["rizzo"] / ".git").exists()
        if step_id == "uv_sync":
            return p["venv_python"].exists()
        if step_id == "download":
            return _weights_present(p["gguf"], self.settings.quant) and _runtime_present(p["runtimes"])
        if step_id == "clone_jev":
            return (p["jev"] / ".git").exists()
        if step_id == "npm_install":
            return p["node_modules"].exists()
        if step_id == "register_service":
            configured = self.env.load_settings().installDir
            return bool(configured) and Path(configured) == self.install_dir
        return True

    # -- commands -----------------------------------------------------------
    def _exec(self, args: list[str], cwd: Path) -> int:
        exe = self.env.which(args[0]) or args[0]
        full = [exe, *args[1:]]
        self.on_log(f"$ ({cwd}) {' '.join(args)}")

        def on_start(pid: int) -> None:
            with self._lock:
                self._pid = pid
            if self.cancelled:  # cancel() raced with the spawn
                self.env.kill_tree(pid)

        try:
            rc = self.env.run_command(full, cwd, self.on_log, on_start)
        finally:
            with self._lock:
                self._pid = None
        if self.cancelled:
            raise SetupCancelled()
        return rc

    def _checked(self, args: list[str], cwd: Path, what: str) -> None:
        rc = self._exec(args, cwd)
        if rc != 0:
            raise SetupError(f"{what} fallito (codice {rc}): vedi il log")

    # -- steps ----------------------------------------------------------------
    def _step_prereqs(self) -> str:
        missing = [t for t in REQUIRED_TOOLS if not self.env.which(t)]
        if missing:
            raise SetupError("mancano nel PATH: " + ", ".join(missing))
        return "ok"

    def _step_clone_rizzo(self) -> str:
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self._checked(["git", "clone", "--branch", self.options.branch, self.options.rizzo_url, "rizzo-flow"],
                      self.install_dir, "git clone")
        return str(self.p["rizzo"])

    def _step_uv_sync(self) -> str:
        self._checked(["uv", "sync", "--locked"], self.p["rizzo"], "uv sync")
        return ".venv pronto"

    def _step_download(self) -> str:
        models, runtimes = self.p["rizzo"] / "models", self.p["runtimes"]
        have = self.env.dir_size(models) + self.env.dir_size(runtimes)
        need = max(0, self.expected - have)
        free = self.env.disk_free(self.install_dir)
        if free < max(MIN_FREE_BYTES, need):
            raise SetupError(
                f"spazio libero insufficiente: {format_bytes(free)} liberi, "
                f"servono almeno {format_bytes(max(MIN_FREE_BYTES, need))}")
        stop = threading.Event()

        def poll() -> None:
            last, last_t, speed = have, time.monotonic(), 0.0
            while not stop.wait(self.poll_interval):
                now = time.monotonic()
                cur = self.env.dir_size(models) + self.env.dir_size(runtimes)
                dt = max(now - last_t, 1e-6)
                inst = max(0.0, (cur - last) / dt)
                speed = inst if speed == 0 else 0.6 * speed + 0.4 * inst
                last, last_t = cur, now
                # Capped below 100%: the last bytes (hash check, unzip) are still to come.
                self.on_progress("download", min(cur, int(self.expected * 0.99)), self.expected, speed)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        try:
            self._checked(["uv", *download_args(self.settings.quant)], self.p["rizzo"], "download dei pesi")
        finally:
            stop.set()
            thread.join(timeout=5)
        self.on_progress("download", self.expected, self.expected, 0.0)
        return "pesi e runtime scaricati"

    def _step_clone_jev(self) -> str:
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self._checked(["git", "clone", "--branch", self.options.branch, self.options.jev_url, "fast-jev-compaction"],
                      self.install_dir, "git clone")
        return str(self.p["jev"])

    def _step_npm_install(self) -> str:
        if not self.p["jev"].exists():
            raise SetupError("fast-jev-compaction non e' stato clonato")
        self._checked(["npm", "install", "--no-audit", "--no-fund"], self.p["jev"], "npm install")
        return "node_modules pronto"

    def _step_register_service(self) -> str:
        if self.env.register is None:
            raise SetupError("nessun meccanismo di attivazione configurato")
        new = replace(self.settings, installDir=str(self.install_dir))
        try:
            self.env.register(asdict(new))
        except Exception as exc:
            raise SetupError(f"attivazione nell'hub fallita: {exc}") from exc
        self.settings = new
        self.on_log(f"Rizzo Flow attivato nell'hub (cartella {self.install_dir}); avvio manuale")
        return "attivo nell'hub (avvio manuale)"

    def _step_plugin_config(self) -> str:
        self.on_log("Configurazione del plugin pronta: copiala dal pannello 'Usa con Claude Code (Jev)'.")
        return "pronta (non scritta su disco)"


# --------------------------------------------------------------------------
# connection test ("Usa con Claude Code (Jev)")
# --------------------------------------------------------------------------

@dataclass
class ConnectionResult:
    ok: bool
    latency_ms: float = 0.0
    noul: float | None = None
    model: str | None = None
    vram_bytes: int | None = None
    error: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"KO: {self.error}"
        parts = [f"OK in {self.latency_ms:.0f} ms", f"modello {self.model}"]
        if self.vram_bytes:
            parts.append(f"VRAM {self.vram_bytes / 1024**3:.1f} GB")
        return ", ".join(parts)


def _default_post(url: str, body: dict, headers: dict, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (local URL)
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check_connection(
    port: int = PORT,
    post: Callable[[str, dict, dict, float], tuple[int, str]] = _default_post,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = 30.0,
) -> ConnectionResult:
    """POSTs one tiny `noul` question to the local System One endpoint, exactly
    like the plugin does, and reports latency / model / VRAM. The key is a
    placeholder (Rizzo checks it only when RIZZO_API_KEY is set)."""
    body = {
        "model": "rizzo-latest",
        "state": {"goal": "connection test", "history": []},
        "questions": {"ping": {"type": "noul", "instructions": "This is only a connection test: is the server answering?"}},
    }
    headers = {"authorization": "Bearer rizzo-local", "content-type": "application/json"}
    started = clock()
    try:
        status, text = post(f"http://127.0.0.1:{port}/v1/systemone", body, headers, timeout)
    except Exception as exc:
        return ConnectionResult(False, error=f"nessuna risposta su :{port} ({exc})")
    latency = (clock() - started) * 1000
    if status != 200:
        return ConnectionResult(False, latency, error=f"HTTP {status}: {text[:160]}")
    try:
        data = json.loads(text)
        noul = float(data["answers"]["ping"]["noul"])
    except (ValueError, KeyError, TypeError):
        return ConnectionResult(False, latency, error="risposta senza answers.ping.noul (non e' un server Jev-compatibile?)")
    vram = None
    try:
        vram = int(data["x_rizzo"]["timing"]["peak_device_bytes"])
    except (KeyError, TypeError, ValueError):
        pass
    return ConnectionResult(True, latency, noul, data.get("model"), vram)


# --------------------------------------------------------------------------
# panel state (pure: what the Rizzo Flow tab shows at a glance)
# --------------------------------------------------------------------------

class Phase(str, Enum):
    CHECKING = "checking"
    NOT_INSTALLED = "not_installed"
    PARTIAL = "partial"          # some steps done, server not usable yet
    INSTALLING = "installing"
    INACTIVE = "inactive"        # on disk but not activated in the hub yet
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RUNNING_EXTERNAL = "running_external"  # answers on the port but is not run by the hub
    ERROR = "error"
    HUB_DOWN = "hub_down"


PHASE_LABELS = {
    Phase.CHECKING: "Verifica in corso...",
    Phase.NOT_INSTALLED: "Non installato",
    Phase.PARTIAL: "Installazione incompleta",
    Phase.INSTALLING: "Installazione in corso",
    Phase.INACTIVE: "Installato, da attivare nell'hub",
    Phase.STOPPED: "Installato, fermo",
    Phase.STARTING: "In avvio...",
    Phase.RUNNING: "In esecuzione",
    Phase.RUNNING_EXTERNAL: "In esecuzione (avviato fuori dall'hub)",
    Phase.ERROR: "Errore: il server si e' fermato (vedi il log)",
    Phase.HUB_DOWN: "Hub non raggiungibile",
}


def derive_phase(
    detection: Detection | None,
    hub_status: str | None,
    hub_reachable: bool,
    installing: bool,
) -> Phase:
    if installing:
        return Phase.INSTALLING
    if detection is None:
        return Phase.CHECKING
    if not detection.any_installed:
        return Phase.NOT_INSTALLED
    if not detection.rizzo_ready:
        return Phase.PARTIAL
    if not detection.service_registered:
        return Phase.INACTIVE
    if not hub_reachable:
        return Phase.RUNNING_EXTERNAL if detection.server_running else Phase.HUB_DOWN
    if hub_status == "starting":
        return Phase.STARTING
    if hub_status == "running":
        return Phase.RUNNING
    if hub_status == "crashed":
        return Phase.ERROR
    return Phase.RUNNING_EXTERNAL if detection.server_running else Phase.STOPPED
