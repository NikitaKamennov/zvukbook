"""Backend client for the private, authenticated Go worker supervisor."""
import json
import os
import shutil
import subprocess
import time
from urllib.request import Request, build_opener, ProxyHandler

from ..storage import job_dir, write_json


def _request(path, payload, timeout=5):
    address = os.environ["ZVUKBOOK_SUPERVISOR_URL"]
    if not address.startswith("http://127.0.0.1:"):
        raise RuntimeError("Supervisor должен находиться на loopback.")
    call = Request(address + path, data=json.dumps(payload).encode("utf-8"),
                   headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["ZVUKBOOK_SUPERVISOR_TOKEN"]})
    # Never route book-related local IPC through a machine's HTTP proxy.
    with build_opener(ProxyHandler({})).open(call, timeout=timeout) as response:
        value = json.loads(response.read(64 * 1024))
    if value.get("error"):
        if value.get('resource_paused'):
            raise MemoryError(value['error'])
        raise RuntimeError(value["error"])
    return value


def request(action, payload):
    return _request('/worker/' + action, payload, timeout=90 if action == 'start' else 5)


def settings(device=None):
    return _request('/settings/get' if device is None else '/settings/set', {} if device is None else {'device': device})


class RemoteProcess:
    def __init__(self, job_id, pid):
        self.job_id, self.pid, self.returncode = job_id, pid, None
        self.reason = ""
        self.resource_paused = False

    def poll(self):
        value = request("state", {"job_id": self.job_id})
        if value["done"]:
            self.returncode = value["exit_code"]
            self.reason = value.get("reason", "")
            self.resource_paused = value.get('resource_paused', False)
        return self.returncode

    def wait(self, timeout):
        until = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() > until:
                raise subprocess.TimeoutExpired("supervised-worker", timeout)
            time.sleep(0.1)
        return self.returncode

    def cancel(self):
        request("cancel", {"job_id": self.job_id})


def start(job, config, source, log):
    directory = job_dir(job["id"])
    inputs = directory / "input"
    inputs.mkdir(mode=0o700, exist_ok=True)
    name = "source.json" if job["kind"] == "render" else "source." + job["format"]
    shutil.copyfile(source, inputs / name)
    shutil.copyfile(config, inputs / "job.json")
    value = request("start", {"job_id": job["id"], "kind": job["kind"]})
    return RemoteProcess(job["id"], value["pid"])
