"""Bundled llama.cpp engine: manages llama-server child processes.

Two servers, started on demand:
  chat  — the answer model + its vision projector (mmproj), so the same weights read
          your questions *and* look at images / video frames / scanned pages.
  embed — the search embedding model, pinned to the CPU (it's tiny) so the GPU's
          memory stays free for the chat model, and for your games.

llama.cpp does the hard parts itself: `--fit` (on by default) measures free VRAM and
offloads as many layers as will fit, and `--sleep-idle-seconds` unloads the weights
after a quiet spell and reloads them on the next request.
"""
from __future__ import annotations

import collections
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx

from .config import IS_WINDOWS, LOG_DIR, get_settings, popen_kwargs, resource_root


class EngineError(RuntimeError):
    pass


# ------------------------------------------------------------------ binary

def server_binary() -> str | None:
    exe = "llama-server.exe" if IS_WINDOWS else "llama-server"
    env = os.environ.get("LOREKEEPER_LLAMA_SERVER")
    candidates = [Path(env)] if env else []
    candidates += [resource_root() / "vendor" / "llama" / exe]
    for c in candidates:
        if c and c.exists():
            return str(c)
    import shutil
    return shutil.which("llama-server")


def engine_version() -> str | None:
    b = server_binary()
    if not b:
        return None
    try:
        r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20,
                           env=_child_env(b), **popen_kwargs())
        m = re.search(r"build (\d+)", (r.stdout or "") + (r.stderr or ""))
        return f"b{m.group(1)}" if m else None
    except Exception:
        return None


def _child_env(binary: str) -> dict:
    env = dict(os.environ)
    if not IS_WINDOWS:   # linux tarball ships its .so files next to the binary
        d = str(Path(binary).parent)
        env["LD_LIBRARY_PATH"] = d + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ windows job object

_job_handle = None


def _attach_to_job(proc: subprocess.Popen) -> None:
    """Put the child in a Job Object with KILL_ON_JOB_CLOSE, so if Lorekeeper crashes or is
    killed from Task Manager, Windows takes the llama-server processes down with it."""
    global _job_handle
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _job_handle is None:
            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            job = k32.CreateJobObjectW(None, None)
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return
            _job_handle = job
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        if h:
            k32.AssignProcessToJobObject(_job_handle, h)
            k32.CloseHandle(h)
    except Exception:
        pass


# ------------------------------------------------------------------ one server

_RE_DEVICE = re.compile(r"using device (\S+) \((.+?)\) \(.*?\) - (\d+) MiB free")
_RE_OFFLOAD = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")
_RE_SLOTS = re.compile(r"n_slots = (\d+), n_ctx_slot = (\d+)")
_RE_ERROR = re.compile(r"^\s*[\d.]+ E |error:|failed to|exception", re.I)


class Server:
    def __init__(self, name: str):
        self.name = name
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.key = secrets.token_urlsafe(18)
        self.signature: tuple | None = None
        self.lock = threading.RLock()
        self.log: collections.deque[str] = collections.deque(maxlen=400)
        self.state = "stopped"            # stopped | loading | ready | sleeping | error
        self.error: str | None = None
        self.devices: list[dict] = []
        self.offload: tuple[int, int] | None = None
        self.slots: tuple[int, int] | None = None
        self.model_path: str | None = None
        self.mmproj_path: str | None = None
        self.started_at = 0.0
        self.ready_at = 0.0
        self.last_used = 0.0
        self._logfile = None

    # ---------------------------------------------------------- lifecycle
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self, args: list[str], model: str, mmproj: str | None, timeout: float = 300) -> "Server":
        """Start (or restart, if the configuration changed) and wait until the model is loaded."""
        sig = (tuple(args), model, mmproj)
        with self.lock:
            if self.alive() and self.signature == sig and self.state in ("ready", "sleeping"):
                self.last_used = time.time()
                return self
            if not (self.alive() and self.signature == sig and self.state == "loading"):
                self._start(args, model, mmproj, sig)
        self._wait_ready(timeout)
        self.last_used = time.time()
        return self

    def _start(self, args: list[str], model: str, mmproj: str | None, sig: tuple) -> None:
        self.stop()
        binary = server_binary()
        if not binary:
            raise EngineError("The built-in AI engine (llama-server) is missing from this install. "
                              "Reinstall Lorekeeper, or switch to an Ollama server in Settings.")
        if not Path(model).exists():
            raise EngineError(f"Model file not found: {model}")
        self.port = _free_port()
        cmd = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(self.port), "--api-key", self.key,
               "--no-webui", "--log-colors", "off", *args]
        if mmproj:
            cmd[3:3] = ["--mmproj", mmproj]
        self.log.clear()
        self.log.append("$ " + " ".join(Path(c).name if os.sep in c or "/" in c else c
                                        for c in cmd).replace(self.key, "•••"))
        self.devices, self.offload, self.slots, self.error = [], None, None, None
        self.model_path, self.mmproj_path = model, mmproj
        try:
            self._logfile = open(LOG_DIR / f"engine-{self.name}.log", "w", encoding="utf-8", buffering=1)
        except Exception:
            self._logfile = None
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", errors="replace", env=_child_env(binary),
                                     cwd=str(Path(binary).parent), **popen_kwargs())
        _attach_to_job(self.proc)
        self.signature = sig
        self.state = "loading"
        self.started_at = time.time()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True, name=f"engine-{self.name}").start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if not line:
                continue
            self.log.append(line)
            if self._logfile:
                try:
                    self._logfile.write(line + "\n")
                except Exception:
                    pass
            if m := _RE_DEVICE.search(line):
                self.devices.append({"id": m.group(1), "name": m.group(2).strip(), "free_mib": int(m.group(3))})
            elif m := _RE_OFFLOAD.search(line):
                self.offload = (int(m.group(1)), int(m.group(2)))
            elif m := _RE_SLOTS.search(line):
                self.slots = (int(m.group(1)), int(m.group(2)))
            elif "llama_server: model loaded" in line or "server is exiting sleeping state" in line:
                if proc is self.proc:
                    self.state = "ready"
                    self.error = None
                    self.ready_at = time.time()
            elif "server is entering sleeping state" in line:
                if proc is self.proc:
                    self.state = "sleeping"
            elif _RE_ERROR.search(line) and self.state == "loading":
                self.error = line.split(": ", 1)[-1][:300]
        code = proc.wait()
        if proc is self.proc and self.state != "stopped":
            self.state = "error"
            if not self.error:
                self.error = f"engine exited (code {code})"
            tail = [l for l in list(self.log)[-12:] if l.strip()]
            for l in reversed(tail):
                if _RE_ERROR.search(l):
                    self.error = l.split(": ", 1)[-1][:300]
                    break

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state == "ready" or self.state == "sleeping":
                return
            if self.state == "error" or not self.alive():
                raise EngineError(f"The AI engine couldn't load the model: {self.error or 'it stopped unexpectedly'}. "
                                  f"See Settings → AI → Engine log.")
            try:
                r = httpx.get(self.url + "/health", timeout=2)
                if r.status_code == 200:
                    self.state = "ready"
                    self.ready_at = self.ready_at or time.time()
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise EngineError("The AI engine is taking too long to load the model (over "
                          f"{int(timeout)} s). A smaller model or 'CPU only' may help.")

    def stop(self) -> None:
        with self.lock:
            p, self.proc = self.proc, None
            self.state = "stopped"
            self.signature = None
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
            if self._logfile:
                try:
                    self._logfile.close()
                except Exception:
                    pass
                self._logfile = None

    # ---------------------------------------------------------- info
    def info(self) -> dict:
        gpu = [d for d in self.devices if not d["id"].upper().startswith("CPU")]
        return {
            "state": self.state if (self.alive() or self.state in ("stopped", "error")) else "stopped",
            "error": self.error,
            "model": Path(self.model_path).name if self.model_path else None,
            "mmproj": Path(self.mmproj_path).name if self.mmproj_path else None,
            "devices": gpu,
            "offload": list(self.offload) if self.offload else None,
            "backend": (gpu[0]["id"].rstrip("0123456789") if gpu else "CPU"),
            "slots": list(self.slots) if self.slots else None,
            "load_s": round(self.ready_at - self.started_at, 1) if self.ready_at and self.ready_at >= self.started_at else None,
            "pid": self.proc.pid if self.alive() else None,
        }


chat = Server("chat")
embed = Server("embed")


def stop_all() -> None:
    for s in (chat, embed):
        s.stop()


# ------------------------------------------------------------------ argument builders

def chat_args() -> list[str]:
    s = get_settings()
    args = ["-c", str(int(s["num_ctx"]))]
    if s.get("gpu_mode") == "cpu":
        args += ["--device", "none"]
    if int(s.get("threads") or 0) > 0:
        args += ["-t", str(int(s["threads"]))]
    idle = int(s.get("idle_sleep_min") or 0)
    if idle > 0:
        args += ["--sleep-idle-seconds", str(idle * 60)]
    return args


def embed_args() -> list[str]:
    # whole passage must fit in one micro-batch: embedding models attend bidirectionally
    args = ["--embeddings", "-c", "4096", "-b", "2048", "-ub", "2048", "--device", "none"]
    threads = max(1, min(4, (os.cpu_count() or 4) // 2))
    idle = int(get_settings().get("idle_sleep_min") or 0)
    if idle > 0:
        args += ["--sleep-idle-seconds", str(idle * 60)]
    return args + ["-t", str(threads)]
