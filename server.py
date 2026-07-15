"""On-demand decensor HTTP API (default container mode).

Drives the Stash UI button: the browser POSTs a scene id, this server runs the
pipeline on the GPU, imports a reviewable preview scene, and exposes live
progress. The user then replaces the original or discards the preview.

Endpoints (all JSON; send X-Decensor-Token if WORKER_TOKEN is set):
  GET  /api/health                     -> {ok, gpu, backend}
  POST /api/decensor                   -> {job_id}     body: {scene_id, ...overrides}
  GET  /api/jobs                        -> [job, ...]
  GET  /api/jobs/<id>                   -> job
  POST /api/jobs/<id>/replace           -> job         (replace original in place)
  POST /api/jobs/<id>/discard           -> job         (delete the preview)

Jobs run one at a time on a single worker thread so they don't contend for the
GPU. States: queued, running, review_ready, replacing, replaced, discarding,
discarded, error.
"""

import os
import re
import sys
import json
import uuid
import queue
import logging
import mimetypes
import posixpath
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

TOKEN = os.environ.get("WORKER_TOKEN", "")
PORT = core._int(os.environ.get("PORT", "8710"), 8710)
WEBUI_DIR = os.environ.get(
    "WEBUI_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
)

# Per-request overrides the UI may send -> cfg keys.
OVERRIDES = {
    "backend": "backend",
    "post_upscale": "postUpscale",
    "realesrgan_model": "realEsrganModel",
    "realesrgan_scale": "realEsrganScale",
    "mask_threshold": "maskThreshold",
    "gpu_id": "gpuId",
    "face_enhance": "realEsrganFaceEnhance",
}

_jobs = {}
_jobs_lock = threading.Lock()
_work = queue.Queue()
_stash = None
_stash_lock = threading.Lock()


def get_stash():
    """Lazily build (and cache) the StashInterface on the worker thread."""
    global _stash
    if _stash is None:
        _stash = core.stash_from_env()
    return _stash


def new_job(scene_id, overrides):
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "scene_id": scene_id,
        "state": "queued",
        "progress": 0.0,
        "message": "Queued",
        "review_scene_id": None,
        "output_path": None,
        "error": None,
        "_overrides": overrides,
        "_info": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def public(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def set_job(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


def progress_cb(job_id):
    def cb(frac, msg=None):
        fields = {"progress": round(float(frac), 3)}
        if msg:
            fields["message"] = msg
        set_job(job_id, **fields)
    return cb


# --------------------------------------------------------------------------- #
# worker thread
# --------------------------------------------------------------------------- #

def job_config(job):
    cfg = core.config_from_env()
    for req_key, cfg_key in OVERRIDES.items():
        val = job["_overrides"].get(req_key)
        if val is not None and val != "":
            cfg[cfg_key] = val
    return cfg


def do_process(job):
    job_id = job["id"]
    set_job(job_id, state="running", message="Starting")
    cfg = job_config(job)
    stash = get_stash()
    info = core.process_to_review(stash, cfg, job["scene_id"], progress=progress_cb(job_id))
    set_job(
        job_id, state="review_ready", progress=1.0, message="Preview ready to review",
        review_scene_id=info.get("review_scene_id"), output_path=info.get("output_path"),
        _info=info,
    )


def do_replace(job):
    job_id = job["id"]
    info = job.get("_info")
    if not info:
        raise RuntimeError("Nothing to replace (no preview).")
    set_job(job_id, state="replacing", progress=0.0, message="Replacing original")
    cfg = job_config(job)
    core.replace_original(get_stash(), cfg, info, progress=progress_cb(job_id))
    set_job(job_id, state="replaced", progress=1.0, message="Original replaced")


def do_discard(job):
    job_id = job["id"]
    info = job.get("_info")
    set_job(job_id, state="discarding", message="Discarding preview")
    if info:
        core.discard_review(get_stash(), job_config(job), info)
    set_job(job_id, state="discarded", message="Preview discarded")


ACTIONS = {"process": do_process, "replace": do_replace, "discard": do_discard}


def worker_loop():
    while True:
        action, job_id = _work.get()
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            continue
        try:
            ACTIONS[action](job)
        except Exception as exc:  # noqa: BLE001
            logging.exception(f"Job {job_id} action {action} failed")
            set_job(job_id, state="error", message=str(exc), error=str(exc))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "decensor/1.0"

    def log_message(self, fmt, *args):  # quieter access log
        logging.debug("%s - %s", self.address_string(), fmt % args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Decensor-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not TOKEN:
            return True
        return self.headers.get("X-Decensor-Token", "") == TOKEN

    def _body(self):
        length = core._int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def serve_static(self, raw):
        """Serve the bundled WebUI SPA for any GET not under /api."""
        rel = posixpath.normpath("/" + raw).lstrip("/") or "index.html"
        full = os.path.join(WEBUI_DIR, rel.replace("/", os.sep))
        root = os.path.abspath(WEBUI_DIR)
        if not os.path.abspath(full).startswith(root) or not os.path.isfile(full):
            full = os.path.join(WEBUI_DIR, "index.html")  # SPA fallback for unknown routes
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "WebUI not installed on this worker"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        raw = self.path.split("?", 1)[0]
        if not raw.startswith("/api"):
            return self.serve_static(raw)
        path = raw.rstrip("/")
        if path == "/api/health":
            return self._send(200, {
                "ok": True,
                "gpu": os.environ.get("GPU_ID", "0"),
                "backend": os.environ.get("BACKEND", "deepmosaics"),
                "postUpscale": core.env_bool("POST_UPSCALE", False),
            })
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if path == "/api/jobs":
            with _jobs_lock:
                data = [public(j) for j in _jobs.values()]
            return self._send(200, data)
        m = re.match(r"^/api/jobs/([0-9a-f]+)$", path)
        if m:
            with _jobs_lock:
                job = _jobs.get(m.group(1))
            return self._send(200, public(job)) if job else self._send(404, {"error": "no such job"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._authed():
            return self._send(401, {"error": "bad token"})

        if path == "/api/decensor":
            body = self._body()
            scene_id = body.get("scene_id") or body.get("sceneId")
            if not scene_id:
                return self._send(400, {"error": "scene_id required"})
            overrides = {k: body[k] for k in OVERRIDES if k in body}
            job = new_job(str(scene_id), overrides)
            _work.put(("process", job["id"]))
            return self._send(202, public(job))

        m = re.match(r"^/api/jobs/([0-9a-f]+)/(replace|discard)$", path)
        if m:
            job_id, action = m.group(1), m.group(2)
            new_state = "replacing" if action == "replace" else "discarding"
            # Check state and claim the job atomically under the lock so a
            # concurrent/duplicate request (e.g. a double-clicked "Replace" or a
            # second tab) gets a 409 instead of both enqueuing — which would drive
            # an already-replaced job into a bogus error/discarded terminal state.
            with _jobs_lock:
                job = _jobs.get(job_id)
                if not job:
                    return self._send(404, {"error": "no such job"})
                if job["state"] != "review_ready":
                    return self._send(409, {"error": f"job is {job['state']}, not review_ready"})
                job["state"] = new_state
                job["message"] = f"Queued {action}"
                snapshot = public(job)
            _work.put((action, job_id))
            return self._send(202, snapshot)

        return self._send(404, {"error": "not found"})


def main():
    # Fail fast on obvious misconfig, but stay up so the UI can show errors.
    try:
        core.validate(core.config_from_env())
    except ValueError as exc:
        logging.warning(f"Config not fully valid yet: {exc}")
    if not os.environ.get("STASH_URL"):
        logging.warning("STASH_URL not set — jobs will fail until it is configured.")

    threading.Thread(target=worker_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info(f"decensor server listening on :{PORT} (token {'on' if TOKEN else 'off'})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
