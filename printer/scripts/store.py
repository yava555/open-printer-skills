"""Persistent device identities and submission receipts (never document bytes)."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from ipp import PrinterError


def normal_uuid(value):
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value).lower().removeprefix("urn:uuid:")))
    except ValueError:
        return None


def endpoint_key(endpoint):
    return json.dumps([endpoint["backend"], endpoint["uri"], endpoint.get("socket_path")],
                      ensure_ascii=False)


class Store:
    def __init__(self, directory=None):
        if directory is None:
            try:
                from platformdirs import user_data_path
            except ImportError as exc:
                raise PrinterError("Install printer/requirements.txt in a virtual environment",
                                   code="missing_dependency", dependency="platformdirs") from exc
            directory = user_data_path("open-printer-skills", appauthor=False)
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "state.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS devices
              (id TEXT PRIMARY KEY, name TEXT NOT NULL, physical_uuid TEXT, last_seen REAL);
            CREATE TABLE IF NOT EXISTS endpoints
              (key TEXT PRIMARY KEY, device_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            PRAGMA user_version = 1;
        """)

    def close(self):
        self.db.close()

    def upsert(self, endpoint, name=None):
        endpoint = dict(endpoint)
        physical = normal_uuid(endpoint.get("physical_uuid"))
        endpoint["physical_uuid"] = physical
        key = endpoint_key(endpoint)
        with self.db:
            # BEGIN IMMEDIATE makes identity selection + insert atomic across agents.
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM devices WHERE physical_uuid=?", (physical,)).fetchone() if physical else None
            existing = self.db.execute("SELECT device_id,data FROM endpoints WHERE key=?", (key,)).fetchone()
            if row is None and existing:
                old = json.loads(existing["data"])
                previous_uuid = normal_uuid(old.get("physical_uuid"))
                if previous_uuid == physical or previous_uuid is None:
                    row = self.db.execute("SELECT * FROM devices WHERE id=?", (existing["device_id"],)).fetchone()
            if existing and not endpoint.get("verified"):
                old = json.loads(existing["data"])
                if (old.get("verified") or old.get("physical_uuid") or old.get("info")) and (not physical or physical == old.get("physical_uuid")):
                    # Keep identity evidence on a temporarily offline endpoint.
                    endpoint = dict(old, verified=False, diagnostic=endpoint.get("diagnostic"))
                    physical = normal_uuid(old.get("physical_uuid"))
                    row = self.db.execute("SELECT * FROM devices WHERE id=?", (existing["device_id"],)).fetchone()
            device_id = row["id"] if row else str(uuid.uuid4())
            label = name or (row["name"] if row else endpoint.get("name") or endpoint["uri"])
            stamp = time.time()
            endpoint["last_seen"] = stamp
            self.db.execute("INSERT OR REPLACE INTO devices VALUES (?,?,?,?)",
                            (device_id, label, physical, stamp))
            self.db.execute("INSERT OR REPLACE INTO endpoints VALUES (?,?,?)",
                            (key, device_id, json.dumps(endpoint, ensure_ascii=False)))
        return self.device(device_id)

    def device(self, device_id):
        row = self.db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise PrinterError("Unknown device_id; run list or discover", code="device_not_found", device_id=device_id)
        result = dict(row)
        result["device_id"] = result.pop("id")
        result["endpoints"] = [json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM endpoints WHERE device_id=?", (device_id,))]
        result["default"] = self.default() == device_id
        return result

    def devices(self):
        return [self.device(row[0]) for row in self.db.execute("SELECT id FROM devices ORDER BY name,id")]

    def default(self):
        row = self.db.execute("SELECT value FROM preferences WHERE key='default' ").fetchone()
        return row[0] if row else None

    def set_default(self, device_id):
        self.device(device_id)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES ('default',?)", (device_id,))

    def new_job(self, device_id, endpoint, filename, job_ref=None, job_name=None):
        job_ref = job_ref or str(uuid.uuid4())
        job = {"job_ref": job_ref, "device_id": device_id, "endpoint": endpoint,
               "backend": endpoint["backend"], "filename": filename, "job_name": job_name or filename,
               "created_at": time.time(),
               "state": "submission_unknown", "submission_unknown": True, "submitted": False}
        # Committed before network I/O. A crash must never look like a safe retry.
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES (?,?)", (job_ref, json.dumps(job, ensure_ascii=False)))
        return job

    def save_job(self, job):
        with self.db:
            self.db.execute("UPDATE jobs SET data=? WHERE id=?",
                            (json.dumps(job, ensure_ascii=False), job["job_ref"]))

    def job(self, job_ref):
        row = self.db.execute("SELECT data FROM jobs WHERE id=?", (job_ref,)).fetchone()
        if row is None:
            raise PrinterError("Unknown job_ref; use jobs to obtain a receipt", code="job_not_found")
        return json.loads(row[0])

    def jobs(self, device_id):
        return [job for row in self.db.execute("SELECT data FROM jobs")
                if (job := json.loads(row[0]))["device_id"] == device_id]
