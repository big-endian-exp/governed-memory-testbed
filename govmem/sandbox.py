"""Host-side sandbox runner: bubblewrap (no Docker/podman on this host) with no network, private PID/IPC/UTS
namespaces, cleared environment, read-only /usr, a per-task /work dir and the gateway socket dir only."""
import os, json, shutil, subprocess, tarfile, time, uuid
AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox_agent.py")

def bwrap_cmd(workdir, gwdir):
    return ["bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", workdir, "/work", "--bind", gwdir, "/gw", "--unshare-all", "--die-with-parent",
            "--new-session", "--clearenv", "--setenv", "PATH", "/usr/bin", "--chdir", "/work",
            "/usr/bin/python3", "-u", "/work/agent.py"]

class SandboxRun:
    def __init__(self, base, gwdir, task_cfg, workdir=None):
        self.workdir = workdir or os.path.join(base, "work", uuid.uuid4().hex[:10])
        os.makedirs(self.workdir, exist_ok=True)
        shutil.copy(AGENT, os.path.join(self.workdir, "agent.py"))
        json.dump(task_cfg, open(os.path.join(self.workdir, "task.json"), "w"))
        self.gwdir, self.lines, self.cfg = gwdir, [], task_cfg

    def run(self, kill_at=None, log_path=None, timeout=60):
        t0 = time.perf_counter(); self.t_ready = self.t_first_step = None
        p = subprocess.Popen(bwrap_cmd(self.workdir, self.gwdir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        killed = False
        for line in p.stdout:
            line = line.rstrip(); self.lines.append(line)
            if line == "READY" and self.t_ready is None: self.t_ready = time.perf_counter() - t0
            if line.startswith("BEGIN") and self.t_first_step is None: self.t_first_step = time.perf_counter() - t0
            if kill_at is not None and line == f"BEGIN {kill_at}":
                p.kill(); killed = True; break
        p.wait(timeout=timeout); self.elapsed = time.perf_counter() - t0
        if log_path:   # host-retained sandbox stdout log
            open(log_path, "w").write("\n".join(self.lines) + "\n")
        self.killed = killed
        return self

    def snapshot(self, path):
        t0 = time.perf_counter()
        with tarfile.open(path, "w:gz") as tf: tf.add(self.workdir, arcname=".")
        return time.perf_counter() - t0, os.path.getsize(path)

    @staticmethod
    def restore(path, workdir):
        t0 = time.perf_counter(); os.makedirs(workdir, exist_ok=True)
        with tarfile.open(path) as tf: tf.extractall(workdir, filter="data")
        return time.perf_counter() - t0

    def destroy(self): shutil.rmtree(self.workdir, ignore_errors=True)

def plain_cmd():   # ungoverned baseline: ordinary subprocess, host env + filesystem + network
    return ["/usr/bin/python3", "-u"]

class PlainRun(SandboxRun):
    def run(self, kill_at=None, log_path=None, timeout=60):
        import subprocess as sp
        env = dict(os.environ, WORK=self.workdir, GW=self.gwdir)
        p = sp.run(plain_cmd() + [os.path.join(self.workdir, "agent.py")], env=env, capture_output=True, text=True, timeout=timeout)
        self.lines = (p.stdout + p.stderr).splitlines(); return self


# ======================= Docker backend =======================
import shlex, io, tarfile as _tar
DOCKER = shlex.split(os.environ.get("GOVMEM_DOCKER", "docker"))
BASE_IMAGE = os.environ.get("GOVMEM_BASE_IMAGE", "python:3.13-slim")
IMAGE = "govmem-sbx:latest"   # BASE_IMAGE + world-writable /work (needed for commit-based snapshots)
UIDGID = f"{os.getuid()}:{os.getgid()}"
BOOT = ("import sys,tarfile;tarfile.open(fileobj=sys.stdin.buffer,mode='r|').extractall('/work',filter='data');"
        "sys.argv=['agent'];exec(compile(open('/work/agent.py').read(),'agent','exec'))")
HARDEN = ["--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--cpus", "1",
          "--memory", "256m", "--pids-limit", "128", "--user", UIDGID]

def dk(*a, check=True, **kw):
    return subprocess.run(DOCKER + list(a), capture_output=True, check=check, **kw)

def _payload(cfg):
    buf = io.BytesIO()
    with _tar.open(fileobj=buf, mode="w") as tf:
        for name, data in (("agent.py", open(AGENT, "rb").read()), ("task.json", json.dumps(cfg).encode())):
            ti = _tar.TarInfo(name); ti.size = len(data); ti.mode = 0o644; tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()

class DockerRun(SandboxRun):
    """storage: 'tmpfs' (read-only rootfs + tmpfs /work, ephemeral), 'bind' (read-only rootfs + host dir /work,
    exportable as tarball), 'layer' (writable container layer /work, snapshotted with docker commit)."""
    def __init__(self, base, gwdir, task_cfg, workdir=None, storage="tmpfs", image=None, container=None):
        self.gwdir, self.cfg, self.lines, self.storage = gwdir, task_cfg, [], storage
        self.image, self.container = image or IMAGE, container   # container => warm exec in a pooled container
        self.workdir = workdir
        if storage == "bind":
            self.workdir = workdir or os.path.join(base, "work", uuid.uuid4().hex[:10]); os.makedirs(self.workdir, exist_ok=True)
        self.name = "gm-" + uuid.uuid4().hex[:12]

    def cmd(self):
        if self.container:
            return DOCKER + ["exec", "-i", self.container, "python3", "-u", "-c", BOOT]
        c = DOCKER + ["run", "-i", "--name", self.name] + HARDEN + ["-v", f"{self.gwdir}:/gw"]
        if self.storage == "tmpfs": c += ["--read-only", "--tmpfs", "/work:rw,mode=1777,size=64m", "--tmpfs", "/tmp:rw,size=16m"]
        elif self.storage == "bind": c += ["--read-only", "-v", f"{self.workdir}:/work", "--tmpfs", "/tmp:rw,size=16m"]
        else: c += ["--workdir", "/"]       # 'layer': writable rootfs so docker commit captures /work
        return c + [self.image, "python3", "-u", "-c", BOOT]

    def run(self, kill_at=None, log_path=None, timeout=120):
        t0 = time.perf_counter(); self.t_ready = self.t_first_step = None
        c = self.cmd()
        p = subprocess.Popen(c, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False)
        p.stdin.write(_payload(self.cfg)); p.stdin.close()
        killed = False
        for raw in p.stdout:
            line = raw.decode(errors="ignore").rstrip(); self.lines.append(line)
            if line == "READY" and self.t_ready is None: self.t_ready = time.perf_counter() - t0
            if line.startswith("BEGIN") and self.t_first_step is None: self.t_first_step = time.perf_counter() - t0
            if kill_at is not None and line == f"BEGIN {kill_at}":
                if self.container: dk("exec", self.container, "pkill", "-9", "-f", "agent", check=False)
                else: dk("kill", self.name, check=False)
                killed = True; break
        p.wait(timeout=timeout); self.elapsed = time.perf_counter() - t0; self.killed = killed
        if log_path: open(log_path, "w").write("\n".join(self.lines) + "\n")
        return self

    def commit(self, ref):
        t0 = time.perf_counter(); dk("commit", self.name, ref)
        size = int(dk("image", "inspect", "-f", "{{.Size}}", ref).stdout.decode().strip())
        return time.perf_counter() - t0, size

    def destroy(self):
        if not self.container: dk("rm", "-f", self.name, check=False)
        if self.storage == "bind" and self.workdir: shutil.rmtree(self.workdir, ignore_errors=True)

def docker_pool_container(gwdir):
    name = "gm-pool-" + uuid.uuid4().hex[:8]
    dk("run", "-d", "--name", name, *HARDEN, "-v", f"{gwdir}:/gw", "--read-only",
       "--tmpfs", "/work:rw,mode=1777,size=64m", "--tmpfs", "/tmp:rw,size=16m", IMAGE, "sleep", "infinity")
    return name

def ensure_image():
    df = f"FROM {BASE_IMAGE}\nRUN mkdir -p /work && chmod 1777 /work\n".encode()
    dk("build", "-q", "-t", IMAGE, "-", input=df)

def docker_available():
    try: return dk("info", check=False).returncode == 0
    except FileNotFoundError: return False
