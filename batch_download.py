"""Asynchronous whole-season downloads with confined, atomic publication.

An injected ``episode_downloader`` is called once per non-skipped episode with
keyword arguments ``serv``, ``anime_id``, ``episode``, ``translation_id``,
``quality``, ``token``, ``anime_title``, and ``metadata``. It must return the path of a
finished source video. The manager copies that source into the season directory
atomically; the downloader does not write the final destination itself.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Optional


class BatchQueueFullError(ValueError):
    """Raised when the bounded batch queue has no free slot."""


def validate_batch_selection(
    serial_data: dict,
    translation_id: str,
    first_episode: int,
    last_episode: int,
) -> dict:
    """Validate a requested translation/range against server-provided metadata."""
    if not isinstance(serial_data, dict):
        raise ValueError("Некорректные данные сериала")
    translations = serial_data.get("translations")
    if not isinstance(translations, list):
        raise ValueError("Сервер не вернул список переводов")
    translation = next(
        (
            item
            for item in translations
            if isinstance(item, dict) and str(item.get("id")) == str(translation_id)
        ),
        None,
    )
    if translation is None:
        raise ValueError("Выбранный перевод недоступен для этого аниме")

    series_count = serial_data.get("series_count")
    if isinstance(series_count, bool) or not isinstance(series_count, int) or series_count < 0:
        raise ValueError("Сервер вернул некорректное число серий")
    if series_count == 0:
        allowed_first, allowed_last = 0, 0
    else:
        series_range = translation.get("series_range")
        if (
            isinstance(series_range, (list, tuple))
            and len(series_range) == 2
            and all(type(value) is int for value in series_range)
            and 1 <= series_range[0] <= series_range[1]
        ):
            allowed_first, allowed_last = series_range
        else:
            allowed_first, allowed_last = 1, series_count
    if first_episode < allowed_first or last_episode > allowed_last:
        raise ValueError(
            f"Для выбранного перевода доступны серии {allowed_first}–{allowed_last}"
        )
    return translation


class BatchDownloadManager:
    """Queue batch download jobs on one worker to limit server load."""

    def __init__(
        self,
        anime_directory: os.PathLike | str,
        episode_downloader: Optional[Callable[..., os.PathLike | str]] = None,
        max_active_jobs: int = 3,
        max_history: int = 50,
    ) -> None:
        if max_active_jobs < 1 or max_history < max_active_jobs:
            raise ValueError("queue and history limits must be positive and consistent")
        self._anime_directory = Path(anime_directory)
        self._episode_downloader = episode_downloader or self._default_episode_downloader
        self._max_active_jobs = max_active_jobs
        self._max_history = max_history
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch-download")
        self._jobs = {}
        self._active_keys = {}
        self._job_keys = {}
        self._lock = threading.RLock()

    def start_job(
        self,
        serv: str,
        anime_id: str,
        translation_id: str,
        translation_name: str,
        quality: str,
        episodes: Iterable[int],
        anime_title: Optional[str] = None,
        token: Optional[str] = None,
        media_metadata: Optional[dict[str, str]] = None,
    ) -> str:
        if serv not in {"sh", "kp"}:
            raise ValueError("serv must be 'sh' or 'kp'")
        if quality not in {"360", "480", "720"}:
            raise ValueError("quality must be '360', '480', or '720'")
        if not str(translation_id).strip():
            raise ValueError("translation_id may not be empty")
        if not isinstance(translation_name, str) or not translation_name.strip():
            raise ValueError("translation_name may not be empty")
        anime_id = str(anime_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", anime_id):
            raise ValueError("anime_id must be a safe single path component")
        episode_numbers = list(episodes)
        if not episode_numbers:
            raise ValueError("episodes may not be empty")
        if any(isinstance(number, bool) or not isinstance(number, int) or number < 0 for number in episode_numbers):
            raise ValueError("episodes must contain non-negative integers")
        if len(set(episode_numbers)) != len(episode_numbers):
            raise ValueError("episodes may not contain duplicates")
        if media_metadata is None:
            media_metadata = {}
        if not isinstance(media_metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in media_metadata.items()
        ):
            raise ValueError("media_metadata must contain only string keys and values")
        media_metadata = dict(media_metadata)
        destination = self._anime_directory / anime_id
        base_resolved = self._anime_directory.resolve()
        if destination.is_symlink():
            raise ValueError("anime destination may not be a symbolic link")
        try:
            destination.resolve().relative_to(base_resolved)
        except ValueError as exc:
            raise ValueError("anime destination escapes anime_directory") from exc
        width = max(2, max((len(str(number)) for number in episode_numbers), default=2))
        episode_statuses = [
            {
                "number": number,
                "state": "queued",
                "status": "queued",
                "file": None,
                "error": None,
            }
            for number in episode_numbers
        ]
        job_key = (serv, anime_id, str(translation_id), quality, tuple(episode_numbers))
        with self._lock:
            duplicate_id = self._active_keys.get(job_key)
            if duplicate_id is not None:
                return duplicate_id
            if len(self._active_keys) >= self._max_active_jobs:
                raise BatchQueueFullError("batch download queue is full")
            while len(self._jobs) >= self._max_history:
                completed_id = next(
                    (
                        candidate_id
                        for candidate_id, candidate in self._jobs.items()
                        if candidate["status"] in {"completed", "completed_with_errors"}
                    ),
                    None,
                )
                if completed_id is None:
                    raise BatchQueueFullError("batch download history is full")
                self._jobs.pop(completed_id, None)
                self._job_keys.pop(completed_id, None)

            job_id = uuid.uuid4().hex
            snapshot = {
                "job_id": job_id,
                "status": "queued",
                "anime_id": str(anime_id),
                "quality": quality,
                "translation": translation_name,
                "destination": str(destination),
                "total": len(episode_numbers),
                "completed_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "progress": 0,
                "current_episode": None,
                "episodes": episode_statuses,
            }
            self._jobs[job_id] = snapshot
            self._active_keys[job_key] = job_id
            self._job_keys[job_id] = job_key
            try:
                self._executor.submit(
                    self._run_job,
                    job_id,
                    serv,
                    str(anime_id),
                    str(translation_id),
                    translation_name,
                    quality,
                    episode_numbers,
                    width,
                    anime_title,
                    token,
                    media_metadata,
                )
            except Exception:
                self._jobs.pop(job_id, None)
                self._active_keys.pop(job_key, None)
                self._job_keys.pop(job_id, None)
                raise
        return job_id

    def get_status(self, job_id: str) -> dict:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self._jobs[job_id])

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    def _run_job(
        self,
        job_id: str,
        serv: str,
        anime_id: str,
        translation_id: str,
        translation_name: str,
        quality: str,
        episodes: list[int],
        width: int,
        anime_title: Optional[str],
        token: Optional[str],
        media_metadata: dict[str, str],
    ) -> None:
        destination = self._anime_directory / anime_id
        try:
            destination_fd = self._open_destination_directory(anime_id, serv)
        except Exception as exc:
            with self._lock:
                job = self._jobs[job_id]
                for item in job["episodes"]:
                    item["state"] = "failed"
                    item["status"] = "failed"
                    item["error"] = str(exc)
                job["failed_count"] = len(episodes)
                job["progress"] = 100
                job["current_episode"] = None
                job["status"] = "completed_with_errors"
            self._release_active_job(job_id)
            return

        try:
            with self._lock:
                self._jobs[job_id]["status"] = "running"
            for index, episode in enumerate(episodes):
                filename = self._episode_filename(episode, translation_name, quality, width)
                final_path = destination / filename
                if self._is_nonempty_regular_file(destination_fd, filename):
                    with self._lock:
                        item = self._jobs[job_id]["episodes"][index]
                        item["state"] = "skipped"
                        item["status"] = "skipped"
                        item["file"] = str(final_path)
                        self._jobs[job_id]["skipped_count"] += 1
                        processed = (
                            self._jobs[job_id]["completed_count"]
                            + self._jobs[job_id]["skipped_count"]
                            + self._jobs[job_id]["failed_count"]
                        )
                        self._jobs[job_id]["progress"] = round(processed * 100 / len(episodes))
                        self._jobs[job_id]["current_episode"] = None
                    continue
                with self._lock:
                    item = self._jobs[job_id]["episodes"][index]
                    item["state"] = "downloading"
                    item["status"] = "downloading"
                    self._jobs[job_id]["current_episode"] = episode
                try:
                    episode_metadata = dict(media_metadata)
                    if episode > 0:
                        episode_metadata.update(
                            {
                                "episode_id": str(episode),
                                "episode_sort": str(episode),
                                "season_number": "1",
                                "track": str(episode),
                            }
                        )
                    source = Path(
                        self._episode_downloader(
                            serv=serv,
                            anime_id=anime_id,
                            episode=episode,
                            translation_id=translation_id,
                            quality=quality,
                            token=token,
                            anime_title=anime_title,
                            metadata=episode_metadata,
                        )
                    )
                    published = self._atomic_publish(source, destination_fd, filename)
                    with self._lock:
                        item["file"] = str(final_path)
                        if published:
                            item["state"] = "completed"
                            item["status"] = "completed"
                            self._jobs[job_id]["completed_count"] += 1
                        else:
                            item["state"] = "skipped"
                            item["status"] = "skipped"
                            self._jobs[job_id]["skipped_count"] += 1
                except Exception as exc:
                    with self._lock:
                        item["state"] = "failed"
                        item["status"] = "failed"
                        item["error"] = str(exc)
                        self._jobs[job_id]["failed_count"] += 1
                with self._lock:
                    processed = (
                        self._jobs[job_id]["completed_count"]
                        + self._jobs[job_id]["skipped_count"]
                        + self._jobs[job_id]["failed_count"]
                    )
                    self._jobs[job_id]["progress"] = round(processed * 100 / len(episodes))
                    self._jobs[job_id]["current_episode"] = None
            with self._lock:
                job = self._jobs[job_id]
                job["current_episode"] = None
                job["status"] = "completed_with_errors" if job["failed_count"] else "completed"
        except Exception as exc:
            with self._lock:
                job = self._jobs[job_id]
                newly_failed = 0
                for item in job["episodes"]:
                    if item["state"] in {"queued", "downloading"}:
                        item["state"] = "failed"
                        item["status"] = "failed"
                        item["error"] = str(exc)
                        newly_failed += 1
                job["failed_count"] += newly_failed
                job["progress"] = 100
                job["current_episode"] = None
                job["status"] = "completed_with_errors"
        finally:
            os.close(destination_fd)
            self._release_active_job(job_id)

    def _release_active_job(self, job_id: str) -> None:
        with self._lock:
            job_key = self._job_keys.get(job_id)
            if job_key is not None and self._active_keys.get(job_key) == job_id:
                self._active_keys.pop(job_key, None)

    def _open_destination_directory(self, anime_id: str, serv: str) -> int:
        """Open and claim the configured destination without following symlinks."""
        self._anime_directory.mkdir(parents=True, exist_ok=True)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(self._anime_directory, directory_flags)
        try:
            try:
                os.mkdir(anime_id, mode=0o755, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            destination_fd = os.open(anime_id, directory_flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        try:
            self._claim_destination_source(destination_fd, serv)
            return destination_fd
        except Exception:
            os.close(destination_fd)
            raise

    @staticmethod
    def _claim_destination_source(destination_fd: int, serv: str) -> None:
        marker_name = ".kodik-source"
        marker_flags = os.O_RDONLY | os.O_NOFOLLOW
        try:
            marker_fd = os.open(marker_name, marker_flags, dir_fd=destination_fd)
        except FileNotFoundError:
            try:
                marker_fd = os.open(
                    marker_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=destination_fd,
                )
            except FileExistsError:
                marker_fd = os.open(marker_name, marker_flags, dir_fd=destination_fd)
            else:
                try:
                    os.write(marker_fd, (serv + "\n").encode("ascii"))
                    os.fsync(marker_fd)
                finally:
                    os.close(marker_fd)
                os.fsync(destination_fd)
                return
        try:
            existing_serv = os.read(marker_fd, 16).decode("ascii", errors="strict").strip()
        finally:
            os.close(marker_fd)
        if existing_serv != serv:
            raise ValueError(
                f"anime destination belongs to source {existing_serv!r}, not {serv!r}"
            )

    @staticmethod
    def _is_nonempty_regular_file(directory_fd: int, filename: str) -> bool:
        try:
            file_stat = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return stat.S_ISREG(file_stat.st_mode) and file_stat.st_size > 0

    @staticmethod
    def _episode_filename(episode: int, translation: str, quality: str, width: int) -> str:
        safe_translation = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', "_", translation)
        safe_translation = re.sub(r"\s+", " ", safe_translation).strip(" ._") or "Translation"
        prefix = "Movie - " if episode == 0 else f"S01E{episode:0{width}d} - "
        suffix = f" - {quality}p.mp4"
        max_translation_bytes = 255 - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
        encoded = safe_translation.encode("utf-8")[:max_translation_bytes]
        safe_translation = encoded.decode("utf-8", errors="ignore").rstrip(" ._") or "Translation"
        return f"{prefix}{safe_translation}{suffix}"

    @staticmethod
    def _atomic_publish(source: Path, destination_fd: int, destination_name: str) -> bool:
        temporary_name = f".batch-{uuid.uuid4().hex}.part"
        source_fd = None
        temporary_fd = None
        try:
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
                raise ValueError("downloaded source must be a non-empty regular file")
            open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            temporary_fd = os.open(
                temporary_name, open_flags, 0o644, dir_fd=destination_fd
            )
            os.fchmod(temporary_fd, 0o644)
            with os.fdopen(temporary_fd, "wb") as temporary:
                temporary_fd = None
                with os.fdopen(source_fd, "rb") as source_file:
                    source_fd = None
                    shutil.copyfileobj(source_file, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.link(
                    temporary_name,
                    destination_name,
                    src_dir_fd=destination_fd,
                    dst_dir_fd=destination_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                return False
            os.fsync(destination_fd)
            os.unlink(
                temporary_name,
                dir_fd=destination_fd,
            )
            temporary_name = None
            os.fsync(destination_fd)
            return True
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=destination_fd)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _default_episode_downloader(**request) -> Path:
        from fast_download import fast_download, get_path

        download_hash, _ = fast_download(
            request["anime_id"],
            request["serv"],
            request["episode"],
            request["translation_id"],
            request["quality"],
            request["token"],
            metadata=request["metadata"],
        )
        return Path(get_path(download_hash))
