# tests/test_rizzo_setup.py
"""Rizzo setup backend: detection, step ordering/skipping, failure, cancel, progress."""
import threading
from pathlib import Path

import pytest

from mcp_hub import rizzo_setup as rs
from dataclasses import replace

from mcp_hub.config import RizzoSettings
from mcp_hub.rizzo_setup import (
    SetupEnv, SetupOptions, SetupRunner, StepState, check_connection, detect, format_progress,
    plugin_config_lines, plugin_instructions, plugin_user_config,
)

GB = 1024**3


@pytest.fixture(autouse=True)
def tiny_weights(monkeypatch):
    """truncate() on NTFS writes real zeros: keep the fake weights a few KB
    instead of the real 2.6 GB (the size check is what is being tested)."""
    monkeypatch.setattr(rs, "_WEIGHTS_BY_QUANT", {"q4_k_m": 4096, "q8_0": 8192})


def make_env(tmp_path, *, tools=("git", "uv", "node", "npm", "nvidia-smi"), settings=None,
             health=False, free=50 * GB, run_command=None, registered=None):
    registered = registered if registered is not None else []
    box = {"settings": settings or RizzoSettings()}

    def register(cfg):
        registered.append(cfg)
        box["settings"] = RizzoSettings(**cfg)

    return SetupEnv(
        which=lambda t: f"C:/bin/{t}.exe" if t in tools else None,
        query=lambda args: "NVIDIA GeForce RTX 3060 Laptop GPU, 6144\n",
        disk_free=lambda p: free,
        dir_size=rs.dir_size,
        health=lambda url: health,
        load_settings=lambda: box["settings"],
        register=register,
        run_command=run_command or (lambda args, cwd, on_line, on_start: 0),
        kill_tree=lambda pid: None,
    ), box, registered


def installed_settings(root: Path, **kw) -> RizzoSettings:
    return RizzoSettings(installDir=str(root), **kw)


def install_everything(root: Path):
    """Lay out a complete install on disk (what the real steps would leave)."""
    rizzo, jev = root / "rizzo-flow", root / "fast-jev-compaction"
    (rizzo / ".git").mkdir(parents=True)
    (rizzo / ".venv" / "Scripts").mkdir(parents=True)
    (rizzo / ".venv" / "Scripts" / "python.exe").write_text("x")
    gguf = rizzo / "models" / "rizzo-flow" / rs.GGUF_NAME
    gguf.parent.mkdir(parents=True)
    with gguf.open("wb") as f:
        f.truncate(rs.weights_bytes("q4_k_m"))
    (rizzo / "runtimes" / "llama-b11081-win32-x64-cuda").mkdir(parents=True)
    (jev / ".git").mkdir(parents=True)
    (jev / "node_modules").mkdir()


# ---------------------------------------------------------------- detect

def test_detect_empty_dir(tmp_path):
    env, _, _ = make_env(tmp_path)
    d = detect(SetupOptions(install_dir=tmp_path / "rizzo"), env)
    assert not d.rizzo_ready and not d.complete
    assert d.prereqs_ok
    assert d.gpu_name == "NVIDIA GeForce RTX 3060 Laptop GPU" and d.gpu_mib == 6144
    assert d.missing == [s for s in rs.STEP_IDS if s != "prereqs"]
    assert not d.server_running and not d.service_registered


def test_detect_partial_install(tmp_path):
    (tmp_path / "rizzo-flow" / ".git").mkdir(parents=True)
    env, _, _ = make_env(tmp_path)
    d = detect(SetupOptions(install_dir=tmp_path), env)
    assert d.steps["clone_rizzo"].done
    assert not d.steps["uv_sync"].done and not d.rizzo_ready


def test_detect_partial_weights_do_not_count(tmp_path):
    install_everything(tmp_path)
    gguf = tmp_path / "rizzo-flow" / "models" / "rizzo-flow" / rs.GGUF_NAME
    with gguf.open("r+b") as f:
        f.truncate(100)  # interrupted download
    env, _, _ = make_env(tmp_path)
    d = detect(SetupOptions(install_dir=tmp_path), env)
    assert not d.steps["download"].done and d.steps["download"].detail == "mancano i pesi"


def test_detect_full_install_and_running(tmp_path):
    install_everything(tmp_path)
    env, _, _ = make_env(tmp_path, settings=installed_settings(tmp_path), health=True)
    d = detect(SetupOptions(install_dir=tmp_path), env)
    assert d.rizzo_ready and d.complete and d.missing == []
    assert d.service_registered and d.server_running


def test_detect_finds_install_dir_from_hub_settings(tmp_path):
    install_everything(tmp_path)
    env, _, _ = make_env(tmp_path, settings=installed_settings(tmp_path))
    d = detect(None, env)
    assert d.install_dir == tmp_path and d.rizzo_ready


def test_detect_uses_the_configured_port_for_the_health_check(tmp_path):
    seen = []
    env, _, _ = make_env(tmp_path, settings=installed_settings(tmp_path, port=9100))
    env.health = lambda url: seen.append(url) or True
    assert detect(None, env).server_running and seen == ["http://127.0.0.1:9100/health"]


def test_detect_defaults_when_nothing_registered(tmp_path):
    env, _, _ = make_env(tmp_path)
    assert detect(None, env).install_dir == rs.DEFAULT_INSTALL_DIR


def test_detect_missing_tools_and_gpu_warnings(tmp_path):
    env, _, _ = make_env(tmp_path, tools=("git",))
    d = detect(SetupOptions(install_dir=tmp_path), env)
    assert not d.prereqs_ok
    assert "uv" in d.steps["prereqs"].detail and "node" in d.steps["prereqs"].detail
    assert any("GPU NVIDIA non rilevata" in w for w in d.warnings)


def test_detect_low_vram_is_a_warning_not_a_failure(tmp_path):
    env, _, _ = make_env(tmp_path)
    env.query = lambda args: "GeForce GTX 1050, 2048\n"
    d = detect(SetupOptions(install_dir=tmp_path), env)
    assert d.prereqs_ok and any("VRAM 2048" in w for w in d.warnings)


# ---------------------------------------------------------------- runner

class Recorder:
    def __init__(self):
        self.steps, self.progress, self.log = [], [], []

    def kwargs(self):
        return dict(
            on_step=lambda s, st, d: self.steps.append((s, st, d)),
            on_progress=lambda s, d, t, v: self.progress.append((s, d, t, v)),
            on_log=self.log.append,
            poll_interval=0.01,
        )

    def states(self):
        final = {}
        for step, state, _ in self.steps:
            final[step] = state
        return final

    def order(self):
        seen = []
        for step, state, _ in self.steps:
            if state in (StepState.RUNNING, StepState.SKIPPED) and step not in seen:
                seen.append(step)
        return seen


def fake_commands(tmp_path, calls, fail_on=None, on_download=None):
    """run_command that leaves on disk what each real command would."""
    def run(args, cwd, on_line, on_start):
        name = Path(args[0]).stem
        sub = args[1] if len(args) > 1 else ""
        key = f"{name} {sub}" + (f" {args[3]}" if name == "uv" and sub == "run" and len(args) > 3 else "")
        calls.append((key, Path(cwd)))
        on_start(1234)
        on_line(f"running {key}")
        if fail_on and fail_on in key:
            on_line("boom")
            return 1
        if name == "git" and sub == "clone":
            (Path(cwd) / args[-1] / ".git").mkdir(parents=True)
        elif name == "uv" and sub == "sync":
            (Path(cwd) / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (Path(cwd) / ".venv" / "Scripts" / "python.exe").write_text("x")
        elif name == "uv" and sub == "run" and "download" in args:
            if on_download:
                on_download(Path(cwd))
            gguf = Path(cwd) / "models" / "rizzo-flow" / rs.GGUF_NAME
            gguf.parent.mkdir(parents=True, exist_ok=True)
            with gguf.open("wb") as f:
                f.truncate(rs.weights_bytes("q4_k_m"))
            (Path(cwd) / "runtimes" / "llama-b11081-win32-x64-cuda").mkdir(parents=True)
        elif name == "npm":
            (Path(cwd) / "node_modules").mkdir(exist_ok=True)
        return 0
    return run


def test_full_run_order_and_state(tmp_path):
    calls, rec = [], Recorder()
    env, box, registered = make_env(tmp_path, run_command=fake_commands(tmp_path, calls))
    runner = SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs())
    assert runner.run() is True
    assert rec.order() == list(rs.STEP_IDS)
    assert set(rec.states().values()) == {StepState.DONE}
    assert [c[0] for c in calls] == [
        "git clone", "uv sync", "uv run download", "git clone", "npm install"]
    assert len(registered) == 1
    cfg = registered[0]  # the built-in settings: no command/args/cwd typed by anyone
    assert cfg["installDir"] == str(tmp_path) and cfg["port"] == 8017 and cfg["ctx"] == 16384
    assert cfg["kvType"] == "q8_0" and cfg["autostart"] is False
    assert box["settings"].installDir == str(tmp_path)
    assert detect(SetupOptions(install_dir=tmp_path), env).complete


def test_second_run_skips_everything_already_done(tmp_path):
    calls, rec = [], Recorder()
    env, _, registered = make_env(tmp_path, run_command=fake_commands(tmp_path, calls))
    SetupRunner(SetupOptions(install_dir=tmp_path), env, **Recorder().kwargs()).run()
    calls.clear()
    registered.clear()
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run() is True
    assert calls == [] and registered == []
    st = rec.states()
    assert st["prereqs"] == StepState.DONE and st["plugin_config"] == StepState.DONE
    assert all(st[s] == StepState.SKIPPED for s in (
        "clone_rizzo", "uv_sync", "download", "clone_jev", "npm_install", "register_service"))


def test_resume_runs_only_missing_steps(tmp_path):
    install_everything(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "fast-jev-compaction" / "node_modules")
    calls, rec = [], Recorder()
    env, _, registered = make_env(tmp_path, run_command=fake_commands(tmp_path, calls))
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run() is True
    assert [c[0] for c in calls] == ["npm install"]
    assert len(registered) == 1  # not registered yet


def test_repair_reruns_sync_install_and_register_but_not_download(tmp_path):
    install_everything(tmp_path)
    calls, rec = [], Recorder()
    env, _, registered = make_env(tmp_path, settings=installed_settings(tmp_path),
                                  run_command=fake_commands(tmp_path, calls))
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run(force=rs.REPAIR_STEPS)
    assert [c[0] for c in calls] == ["uv sync", "npm install"]
    assert len(registered) == 1
    assert rec.states()["download"] == StepState.SKIPPED


def test_failure_stops_the_chain_and_marks_the_step(tmp_path):
    calls, rec = [], Recorder()
    env, _, registered = make_env(
        tmp_path, run_command=fake_commands(tmp_path, calls, fail_on="uv sync"))
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run() is False
    st = rec.states()
    assert st["clone_rizzo"] == StepState.DONE and st["uv_sync"] == StepState.FAILED
    assert "download" not in st and "register_service" not in st  # never started: still pending
    assert [c[0] for c in calls] == ["git clone", "uv sync"]
    assert registered == []
    assert "boom" in rec.log
    failed = [d for s, state, d in rec.steps if state == StepState.FAILED][0]
    assert "uv sync fallito" in failed


def test_missing_prerequisite_fails_first_step(tmp_path):
    calls, rec = [], Recorder()
    env, _, _ = make_env(tmp_path, tools=("git", "uv"), run_command=fake_commands(tmp_path, calls))
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run() is False
    assert rec.states() == {"prereqs": StepState.FAILED}
    assert calls == []


def test_step_that_leaves_nothing_on_disk_is_failed(tmp_path):
    env, _, _ = make_env(tmp_path, run_command=lambda a, c, l, s: 0)  # "succeeds" but does nothing
    rec = Recorder()
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run() is False
    assert rec.states()["clone_rizzo"] == StepState.FAILED


def test_download_refused_when_disk_is_too_small(tmp_path):
    install_everything(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "rizzo-flow" / "runtimes")
    calls, rec = [], Recorder()
    env, _, _ = make_env(tmp_path, free=2 * GB, run_command=fake_commands(tmp_path, calls))
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run() is False
    assert rec.states()["download"] == StepState.FAILED
    assert calls == []  # never invoked the 3.9 GB download
    detail = [d for s, st, d in rec.steps if st == StepState.FAILED][0]
    assert "spazio libero insufficiente" in detail


def test_download_progress_is_driven_by_bytes_on_disk(tmp_path):
    rizzo_models = tmp_path / "rizzo-flow" / "models"
    sizes = iter([0, 500_000_000, 1_500_000_000, 3_000_000_000])
    state = {"n": 0}

    def on_download(cwd):
        import time
        time.sleep(0.15)  # let the poller tick a few times

    env, _, _ = make_env(tmp_path, run_command=fake_commands(tmp_path, [], on_download=on_download))
    real_size = env.dir_size

    def fake_size(path):
        if Path(path).name == "models":
            state["n"] += 1
            return next(sizes, 3_000_000_000)
        return real_size(path)

    env.dir_size = fake_size
    rec = Recorder()
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run() is True
    dl = [p for p in rec.progress if p[0] == "download" and p[2] > 0]
    assert len(dl) >= 3
    expected = rs.expected_download_bytes("q4_k_m")
    assert all(p[2] == expected for p in dl)
    mid = [p for p in dl[:-1]]
    assert all(p[1] <= expected * 0.99 for p in mid)  # capped until the step ends
    assert mid[-1][1] > mid[0][1]
    assert any(p[3] > 0 for p in mid)  # speed reported
    assert dl[-1][1] == expected  # 100% only when done


def test_cancel_kills_the_process_tree_and_stops(tmp_path):
    started, release, killed = threading.Event(), threading.Event(), []
    calls = []

    def run(args, cwd, on_line, on_start):
        calls.append(args[1])
        on_start(4321)
        started.set()
        release.wait(5)  # blocks like a long download until killed
        return 1

    env, _, _ = make_env(tmp_path, run_command=run)
    env.kill_tree = lambda pid: (killed.append(pid), release.set())
    rec = Recorder()
    runner = SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs())
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", runner.run()))
    t.start()
    assert started.wait(5)
    runner.cancel()
    t.join(5)
    assert not t.is_alive()
    assert result["ok"] is False and killed == [4321]
    assert rec.states()["clone_rizzo"] == StepState.FAILED
    assert [d for s, st, d in rec.steps if st == StepState.FAILED][0] == "annullato dall'utente"
    assert calls == ["clone"]  # nothing after the cancel


def test_register_failure_is_reported(tmp_path):
    install_everything(tmp_path)
    env, _, _ = make_env(tmp_path)
    env.register = lambda cfg: (_ for _ in ()).throw(RuntimeError("hub down"))
    rec = Recorder()
    assert SetupRunner(SetupOptions(install_dir=tmp_path), env, **rec.kwargs()).run() is False
    detail = [d for s, st, d in rec.steps if st == StepState.FAILED][0]
    assert "hub down" in detail


# ---------------------------------------------------------------- helpers

def test_format_progress():
    text = format_progress("Download", 1_288_490_189, 3_914_315_482, 8_808_038)
    assert text == "Download: 1.2 GB / 3.6 GB (33%), 8 MB/s"


def test_plugin_config_text_and_values(tmp_path):
    cfg = plugin_user_config()
    assert cfg["baseUrl"] == "http://127.0.0.1:8017/v1/systemone" and cfg["model"] == "rizzo-latest"
    assert (cfg["maxStateTokens"], cfg["maxRequestTokens"], cfg["maxQuestionsPerRequest"]) == (12000, 14000, 64)
    text = plugin_instructions(9100)
    assert "/plugin marketplace add LuigiElleBalotta/fast-jev-compaction" in text
    assert "/plugin install fast-jev-compaction@fast-jev-compaction" in text
    assert "baseUrl" in plugin_config_lines() and ":9100/v1/systemone" in text
    assert "settings.json" not in text.replace("non", "")  # never tells to edit settings.json


def test_builtin_service_is_built_from_settings_and_starts_manually(tmp_path):
    sc = installed_settings(tmp_path, port=9100, ctx=8192, kvType="q4_0").to_service()
    assert sc.is_service and sc.enabled and not sc.starts_with_hub
    assert sc.cwd == str(tmp_path / "rizzo-flow") and sc.command == "uv"
    assert sc.args == ["run", "rizzo", "serve", "--size", "4b", "--quant", "q4_k_m", "--device", "cuda",
                       "--ctx", "8192", "--kv-type", "q4_0", "--port", "9100"]
    assert sc.healthUrl == "http://127.0.0.1:9100/health" and sc.effective_port == 9100
    assert RizzoSettings().to_service() is None  # not installed: the hub runs nothing


def test_other_quant_uses_its_own_weights_file_and_download_args(tmp_path):
    settings = installed_settings(tmp_path, quant="q8_0")
    assert rs.gguf_name("q8_0").endswith("lora-q8_0.gguf")
    assert rs.download_args("q8_0")[5:7] == ["--quant", "q8_0"]
    install_everything(tmp_path)  # only q4_k_m weights are on disk
    env, _, _ = make_env(tmp_path, settings=settings)
    assert not detect(SetupOptions(install_dir=tmp_path), env).steps["download"].done


def test_connection_ok_reports_latency_model_and_vram():
    body = ('{"model":"rizzo-flow-4b-q4_k_m","answers":{"ping":{"type":"noul","noul":0.7}},'
            '"x_rizzo":{"timing":{"peak_device_bytes":4219469824}}}')
    clock = iter([10.0, 10.75]).__next__
    seen = {}

    def post(url, payload, headers, timeout):
        seen.update(url=url, payload=payload, headers=headers)
        return 200, body

    res = check_connection(8017, post, clock)
    assert res.ok and round(res.latency_ms) == 750 and res.noul == 0.7
    assert seen["url"] == "http://127.0.0.1:8017/v1/systemone"
    assert seen["payload"]["model"] == "rizzo-latest" and seen["payload"]["questions"]["ping"]["type"] == "noul"
    assert res.summary() == "OK in 750 ms, modello rizzo-flow-4b-q4_k_m, VRAM 3.9 GB"


def test_connection_failures_are_reported_not_raised():
    def refused(url, payload, headers, timeout):
        raise ConnectionRefusedError("refused")

    assert "nessuna risposta su :8017" in check_connection(8017, refused).error
    assert "HTTP 422" in check_connection(8017, lambda *a: (422, "bad")).error
    assert "Jev-compatibile" in check_connection(8017, lambda *a: (200, "{}")).error
    assert not check_connection(8017, lambda *a: (200, "not json")).ok
