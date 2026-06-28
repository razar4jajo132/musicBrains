"""
jobs.py
=======

A tiny in-memory job manager that backs the fake-SABnzbd download client.

When Lidarr "grabs" a release it tells SABnzbd to download an NZB. We translate
that into a Job: resolve the MusicBrainz release, download every track from
YouTube, tag it, and move the finished album into the completed directory.

Lidarr then polls SABnzbd's ``queue`` (in-progress) and ``history`` (finished)
endpoints; we synthesize those views from the Job objects here.

State is intentionally in-memory: a restart loses queue history, which is fine
— Lidarr re-queries and anything already imported stays imported. If you later
want durability, swap the dict for SQLite behind the same JobStore API.
"""

from __future__ import annotations

import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config
import core


# Status values mirror the SABnzbd vocabulary Lidarr expects.
QUEUED = "Queued"
DOWNLOADING = "Downloading"
COMPLETED = "Completed"
FAILED = "Failed"

ACTIVE = (QUEUED, DOWNLOADING)
FINISHED = (COMPLETED, FAILED)

# Per-track size estimate (MB) for the queue display. Shared with the Newznab
# size calc via config so the two stay consistent. See config.EST_MB_PER_TRACK.
EST_MB_PER_TRACK = config.EST_MB_PER_TRACK


@dataclass
class Job:
    nzo_id: str
    mbid: str
    name: str                      # "Artist - Album", from the release title
    category: str
    status: str = QUEUED
    done: int = 0
    total: int = 0
    storage: str = ""              # final folder, reported to Lidarr on success
    error: str = ""

    @property
    def total_mb(self) -> float:
        return max(1.0, (self.total or 1) * EST_MB_PER_TRACK)

    @property
    def percentage(self) -> int:
        if not self.total:
            return 0
        return int(100 * self.done / self.total)

    @property
    def mb_left(self) -> float:
        return round(self.total_mb * (1 - (self.percentage / 100.0)), 1)


class JobStore:
    """Thread-safe registry of jobs plus the worker pool that runs them."""

    def __init__(self, max_workers: int = 2):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="dl")

    # --- queries used by the SABnzbd endpoint ---------------------------
    def get(self, nzo_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(nzo_id)

    def active(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status in ACTIVE]

    def finished(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status in FINISHED]

    def remove(self, nzo_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(nzo_id, None) is not None

    # --- enqueue --------------------------------------------------------
    def enqueue(self, mbid: str, name: str, category: Optional[str] = None) -> str:
        nzo_id = f"SABnzbd_nzo_{uuid.uuid4().hex[:12]}"
        job = Job(
            nzo_id=nzo_id,
            mbid=mbid,
            name=name or mbid,
            category=category or config.CATEGORY,
        )
        with self._lock:
            self._jobs[nzo_id] = job
        self._pool.submit(self._run, nzo_id)
        return nzo_id

    # --- the actual work ------------------------------------------------
    def _set(self, nzo_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(nzo_id)
            if job:
                for k, v in fields.items():
                    setattr(job, k, v)

    def _run(self, nzo_id: str) -> None:
        job = self.get(nzo_id)
        if not job:
            return
        try:
            self._set(nzo_id, status=DOWNLOADING)
            album = core.resolve_album(job.mbid)
            self._set(nzo_id, total=len(album.tracks),
                      name=f"{album.artist} - {album.album}")

            if album.pick_count == 0:
                self._set(nzo_id, status=FAILED,
                          error="No YouTube matches found for any track")
                return

            folder = album.folder_name()
            staging = config.INCOMPLETE_DIR / nzo_id / folder
            final = config.COMPLETED_DIR / job.category / folder

            def _progress(done: int, total: int) -> None:
                self._set(nzo_id, done=done, total=total)

            report = core.download_album(album, staging, progress=_progress)

            if not report.any_success:
                self._set(nzo_id, status=FAILED,
                          error="All track downloads failed")
                shutil.rmtree(staging.parent, ignore_errors=True)
                return

            # Atomically reveal the finished album to Lidarr.
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            shutil.move(str(staging), str(final))
            shutil.rmtree(staging.parent, ignore_errors=True)

            self._set(nzo_id, status=COMPLETED, storage=str(final),
                      done=job.total, error="")
        except Exception as exc:  # noqa: BLE001 — surface as a failed job
            self._set(nzo_id, status=FAILED, error=str(exc))


# Module-level singleton the server imports.
store = JobStore()
