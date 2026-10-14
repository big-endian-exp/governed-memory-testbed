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
