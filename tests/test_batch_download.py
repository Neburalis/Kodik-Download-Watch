import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import batch_download
from batch_download import (
    BatchDownloadManager,
    BatchQueueFullError,
    validate_batch_selection,
)


class BatchSelectionValidationTests(unittest.TestCase):
    def setUp(self):
        self.serial_data = {
            "series_count": 24,
            "translations": [
                {"id": "610", "name": "AniLibria.TV", "series_range": [1, 24]},
                {"id": "999", "name": "Movie Dub", "series_range": [0, 0]},
            ],
        }

    def test_accepts_translation_and_range_from_server_metadata(self):
        translation = validate_batch_selection(self.serial_data, "610", 1, 24)
        self.assertEqual(translation["name"], "AniLibria.TV")

    def test_rejects_unknown_translation_and_out_of_range_episodes(self):
        with self.assertRaises(ValueError):
            validate_batch_selection(self.serial_data, "404", 1, 1)
        with self.assertRaises(ValueError):
            validate_batch_selection(self.serial_data, "610", 0, 24)
        with self.assertRaises(ValueError):
            validate_batch_selection(self.serial_data, "610", 1, 25)

    def test_accepts_episode_zero_for_movie_metadata(self):
        movie_data = {
            "series_count": 0,
            "translations": [
                {"id": "999", "name": "Movie Dub", "series_range": [0, 0]},
            ],
        }
        translation = validate_batch_selection(movie_data, "999", 0, 0)
        self.assertEqual(translation["id"], "999")


class BatchDownloadManagerTests(unittest.TestCase):
    def wait_for_terminal_status(self, manager, job_id, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = manager.get_status(job_id)
            if status["status"] in {"completed", "completed_with_errors"}:
                return status
            time.sleep(0.01)
        self.fail(f"job {job_id} did not finish")

    def test_successfully_downloads_episode_to_season_filename(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "download.mp4"
            source.write_bytes(b"video-data")
            calls = []

            def downloader(**request):
                calls.append(request)
                return source

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            job_id = manager.start_job(
                serv="sh",
                anime_id="123",
                translation_id="610",
                translation_name="AniLibria.TV",
                quality="720",
                episodes=[1],
                anime_title="Example",
                token="test",
            )
            status = self.wait_for_terminal_status(manager, job_id)

            output = Path(root) / "123" / "S01E01 - AniLibria.TV - 720p.mp4"
            self.assertEqual(output.read_bytes(), b"video-data")
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["completed_count"], 1)
            self.assertEqual(status["failed_count"], 0)
            self.assertEqual(status["progress"], 100)
            self.assertEqual(status["episodes"][0]["state"], "completed")
            self.assertEqual(status["episodes"][0]["status"], "completed")
            self.assertEqual(status["episodes"][0]["file"], str(output))
            self.assertEqual(
                calls,
                [{
                    "serv": "sh",
                    "anime_id": "123",
                    "episode": 1,
                    "translation_id": "610",
                    "quality": "720",
                    "token": "test",
                    "anime_title": "Example",
                    "metadata": {
                        "episode_id": "1",
                        "episode_sort": "1",
                        "season_number": "1",
                        "track": "1",
                    },
                }],
            )
            json.dumps(status)
            manager.shutdown()

    def test_rejects_unsafe_anime_ids_before_scheduling(self):
        with TemporaryDirectory() as root:
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: None)
            for unsafe_id in ("../escape", "nested/id", "nested\\id", ".", "..", "/absolute"):
                with self.subTest(anime_id=unsafe_id):
                    with self.assertRaises(ValueError):
                        manager.start_job("sh", unsafe_id, "610", "Dub", "720", [1])
            manager.shutdown()

    def test_rejects_symlink_destination_that_escapes_anime_directory(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as outside:
            (Path(root) / "123").symlink_to(outside, target_is_directory=True)
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: Path(outside))

            try:
                with self.assertRaises(ValueError):
                    manager.start_job("sh", "123", "610", "Dub", "720", [1])
            finally:
                manager.shutdown()

    def test_rejects_destination_symlink_created_while_job_is_queued(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as outside, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            first_started = threading.Event()
            release_first = threading.Event()

            def downloader(**request):
                if request["anime_id"] == "blocker":
                    first_started.set()
                    self.assertTrue(release_first.wait(2))
                return source

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            first_id = manager.start_job("sh", "blocker", "610", "Dub", "720", [1])
            self.assertTrue(first_started.wait(1))
            second_id = manager.start_job("sh", "victim", "610", "Dub", "720", [1])
            (Path(root) / "victim").symlink_to(outside, target_is_directory=True)
            release_first.set()

            self.wait_for_terminal_status(manager, first_id)
            status = self.wait_for_terminal_status(manager, second_id)
            self.assertEqual(status["status"], "completed_with_errors")
            self.assertEqual(status["episodes"][0]["state"], "failed")
            self.assertEqual(list(Path(outside).iterdir()), [])
            manager.shutdown()

    def test_rejects_cross_service_reuse_of_existing_anime_directory(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            calls = []

            def downloader(**request):
                calls.append(request["serv"])
                return source

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            first_id = manager.start_job("sh", "same-id", "610", "Dub", "720", [1])
            first = self.wait_for_terminal_status(manager, first_id)
            second_id = manager.start_job("kp", "same-id", "610", "Dub", "720", [2])
            second = self.wait_for_terminal_status(manager, second_id)

            self.assertEqual(first["status"], "completed")
            self.assertEqual(second["status"], "completed_with_errors")
            self.assertIn("source", second["episodes"][0]["error"].lower())
            self.assertEqual(calls, ["sh"])
            self.assertEqual((Path(root) / "same-id" / ".kodik-source").read_text(), "sh\n")
            manager.shutdown()

    def test_forwards_episode_specific_media_metadata_to_downloader(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            calls = []

            def downloader(**request):
                calls.append(request)
                return source

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            job_id = manager.start_job(
                "sh",
                "17895",
                "610",
                "Dub",
                "720",
                [7],
                media_metadata={"title": "Golden Time", "date": "2013-10-04"},
            )
            status = self.wait_for_terminal_status(manager, job_id)

            self.assertEqual(status["status"], "completed")
            self.assertEqual(calls[0]["metadata"]["title"], "Golden Time")
            self.assertEqual(calls[0]["metadata"]["date"], "2013-10-04")
            self.assertEqual(calls[0]["metadata"]["episode_id"], "7")
            self.assertEqual(calls[0]["metadata"]["episode_sort"], "7")
            self.assertEqual(calls[0]["metadata"]["season_number"], "1")
            manager.shutdown()

    def test_validates_server_quality_translation_and_episodes(self):
        with TemporaryDirectory() as root:
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: Path(root))
            invalid_calls = [
                ("other", "123", "610", "Dub", "720", [1]),
                ("sh", "123", "610", "Dub", "1080", [1]),
                ("sh", "123", "", "Dub", "720", [1]),
                ("sh", "123", "610", "", "720", [1]),
                ("sh", "123", "610", "Dub", "720", []),
                ("sh", "123", "610", "Dub", "720", [-1]),
                ("sh", "123", "610", "Dub", "720", [1, 1]),
            ]
            for args in invalid_calls:
                with self.subTest(args=args):
                    with self.assertRaises((TypeError, ValueError)):
                        manager.start_job(*args)
            manager.shutdown()

    def test_skips_existing_nonempty_final_file(self):
        with TemporaryDirectory() as root:
            destination = Path(root) / "123"
            destination.mkdir()
            output = destination / "S01E01 - Dub - 720p.mp4"
            output.write_bytes(b"existing")
            calls = []

            def downloader(**request):
                calls.append(request)
                raise AssertionError("downloader must not be called")

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            job_id = manager.start_job("kp", "123", "610", "Dub", "720", [1])
            status = self.wait_for_terminal_status(manager, job_id)

            self.assertEqual(output.read_bytes(), b"existing")
            self.assertEqual(calls, [])
            self.assertEqual(status["episodes"][0]["state"], "skipped")
            self.assertEqual(status["episodes"][0]["file"], str(output))
            self.assertEqual(status["completed_count"], 0)
            self.assertEqual(status["skipped_count"], 1)
            self.assertEqual(status["progress"], 100)
            manager.shutdown()

    def test_continues_after_episode_failure_and_reports_terminal_counts(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source_dir = Path(sources)
            for episode in (1, 3):
                (source_dir / f"{episode}.mp4").write_bytes(f"episode-{episode}".encode())
            calls = []

            def downloader(**request):
                episode = request["episode"]
                calls.append(episode)
                if episode == 2:
                    raise RuntimeError("upstream unavailable")
                return source_dir / f"{episode}.mp4"

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            job_id = manager.start_job("sh", "123", "610", "Dub", "480", [1, 2, 3])
            status = self.wait_for_terminal_status(manager, job_id)

            self.assertEqual(calls, [1, 2, 3])
            self.assertEqual(status["status"], "completed_with_errors")
            self.assertEqual(status["completed_count"], 2)
            self.assertEqual(status["failed_count"], 1)
            self.assertEqual(status["progress"], 100)
            self.assertIsNone(status["current_episode"])
            self.assertEqual([item["state"] for item in status["episodes"]], ["completed", "failed", "completed"])
            self.assertEqual(status["episodes"][1]["error"], "upstream unavailable")
            self.assertFalse((Path(root) / "123" / "S01E02 - Dub - 480p.mp4").exists())
            manager.shutdown()

    def test_sanitizes_translation_component_in_final_filename(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: source)

            job_id = manager.start_job("sh", "123", "610", ' Dub/Bad:*?"<>|\n ', "360", [1])
            status = self.wait_for_terminal_status(manager, job_id)

            output = Path(root) / "123" / "S01E01 - Dub_Bad - 360p.mp4"
            self.assertTrue(output.is_file())
            self.assertEqual(status["translation"], ' Dub/Bad:*?"<>|\n ')
            self.assertEqual(status["episodes"][0]["file"], str(output))
            manager.shutdown()

    def test_long_unicode_translation_stays_within_filesystem_name_limit(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: source)

            job_id = manager.start_job("sh", "123", "610", "Я" * 300, "720", [1])
            status = self.wait_for_terminal_status(manager, job_id)

            file_path = Path(status["episodes"][0]["file"])
            self.assertEqual(status["status"], "completed")
            self.assertLessEqual(len(file_path.name.encode("utf-8")), 255)
            self.assertEqual(file_path.read_bytes(), b"video")
            manager.shutdown()

    def test_episode_zero_uses_movie_filename(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "movie.mp4"
            source.write_bytes(b"movie")
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: source)

            job_id = manager.start_job("kp", "42", "7", "Original", "720", [0])
            status = self.wait_for_terminal_status(manager, job_id)

            output = Path(root) / "42" / "Movie - Original - 720p.mp4"
            self.assertEqual(output.read_bytes(), b"movie")
            self.assertEqual(status["episodes"][0]["file"], str(output))
            manager.shutdown()

    def test_deduplicates_active_jobs_and_bounds_the_queue(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            started = threading.Event()
            release = threading.Event()

            def downloader(**request):
                started.set()
                self.assertTrue(release.wait(2))
                return source

            manager = BatchDownloadManager(
                root, episode_downloader=downloader, max_active_jobs=2
            )
            first = manager.start_job("sh", "1", "610", "Dub", "720", [1])
            self.assertTrue(started.wait(1))
            duplicate = manager.start_job("sh", "1", "610", "Dub", "720", [1])
            second = manager.start_job("sh", "2", "610", "Dub", "720", [1])

            self.assertEqual(duplicate, first)
            with self.assertRaises(BatchQueueFullError):
                manager.start_job("sh", "3", "610", "Dub", "720", [1])

            release.set()
            self.wait_for_terminal_status(manager, first)
            self.wait_for_terminal_status(manager, second)
            manager.shutdown()

    def test_evicts_old_completed_jobs_from_bounded_history(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            manager = BatchDownloadManager(
                root,
                episode_downloader=lambda **request: source,
                max_active_jobs=2,
                max_history=2,
            )
            job_ids = []
            for anime_id in ("1", "2", "3"):
                job_id = manager.start_job("sh", anime_id, "610", "Dub", "720", [1])
                self.wait_for_terminal_status(manager, job_id)
                job_ids.append(job_id)

            with self.assertRaises(KeyError):
                manager.get_status(job_ids[0])
            self.assertEqual(manager.get_status(job_ids[1])["status"], "completed")
            self.assertEqual(manager.get_status(job_ids[2])["status"], "completed")
            manager.shutdown()

    def test_jobs_are_serialized_and_status_snapshots_are_deep_copies(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            first_started = threading.Event()
            release_first = threading.Event()
            calls = []

            def downloader(**request):
                calls.append(request["anime_id"])
                if request["anime_id"] == "1":
                    first_started.set()
                    self.assertTrue(release_first.wait(2))
                return source

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            first_id = manager.start_job("sh", "1", "610", "Dub", "720", [1])
            self.assertTrue(first_started.wait(1))
            second_id = manager.start_job("sh", "2", "610", "Dub", "720", [1])

            first = manager.get_status(first_id)
            second = manager.get_status(second_id)
            self.assertEqual(first["status"], "running")
            self.assertEqual(first["current_episode"], 1)
            self.assertEqual(first["episodes"][0]["state"], "downloading")
            self.assertEqual(second["status"], "queued")
            first["episodes"][0]["state"] = "tampered"
            self.assertEqual(manager.get_status(first_id)["episodes"][0]["state"], "downloading")
            self.assertEqual(calls, ["1"])

            release_first.set()
            self.wait_for_terminal_status(manager, first_id)
            self.wait_for_terminal_status(manager, second_id)
            self.assertEqual(calls, ["1", "2"])
            manager.shutdown()

    def test_atomic_publish_failure_leaves_no_final_or_temporary_file(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: source)

            with mock.patch("batch_download.os.link", side_effect=OSError("disk failure")):
                job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1])
                status = self.wait_for_terminal_status(manager, job_id)

            destination = Path(root) / "123"
            final = destination / "S01E01 - Dub - 720p.mp4"
            self.assertEqual(status["episodes"][0]["state"], "failed")
            self.assertEqual(status["episodes"][0]["error"], "disk failure")
            self.assertFalse(final.exists())
            self.assertEqual(
                [path.name for path in destination.iterdir()],
                [".kodik-source"],
            )
            manager.shutdown()

    def test_atomic_publish_never_replaces_destination_created_during_download(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"new")
            destination = Path(root) / "123"

            def downloader(**request):
                destination.mkdir(exist_ok=True)
                (destination / ".kodik-source").write_text("sh\n", encoding="ascii")
                (destination / "S01E01 - Dub - 720p.mp4").write_bytes(b"existing")
                return source

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1])
            status = self.wait_for_terminal_status(manager, job_id)

            final = destination / "S01E01 - Dub - 720p.mp4"
            self.assertEqual(final.read_bytes(), b"existing")
            self.assertEqual(status["episodes"][0]["state"], "skipped")
            self.assertEqual(status["skipped_count"], 1)
            self.assertEqual(status["completed_count"], 0)
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                [".kodik-source", "S01E01 - Dub - 720p.mp4"],
            )
            manager.shutdown()

    def test_destination_creation_failure_finishes_job_with_errors(self):
        with TemporaryDirectory() as parent:
            root = Path(parent) / "not-a-directory"
            root.write_text("occupied", encoding="utf-8")
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: root)

            job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1, 2])
            status = self.wait_for_terminal_status(manager, job_id)

            self.assertEqual(status["status"], "completed_with_errors")
            self.assertEqual(status["failed_count"], 2)
            self.assertEqual(status["progress"], 100)
            self.assertTrue(all(item["state"] == "failed" for item in status["episodes"]))
            self.assertTrue(all(item["error"] for item in status["episodes"]))
            manager.shutdown()

    def test_atomic_publish_rejects_symlink_source(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            real_source = Path(sources) / "real.mp4"
            real_source.write_bytes(b"video")
            source = Path(sources) / "source.mp4"
            source.symlink_to(real_source)
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    BatchDownloadManager._atomic_publish(source, directory_fd, "final.mp4")
            finally:
                os.close(directory_fd)
            self.assertFalse((Path(root) / "final.mp4").exists())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_atomic_temporary_name_does_not_extend_long_final_filename(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            destination_name = "a" * 250 + ".mp4"
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            original_open = os.open

            def linux_name_limit_open(path, flags, mode=0o777, *, dir_fd=None):
                if dir_fd is not None and len(os.fsencode(path)) > 255:
                    raise OSError("filename component exceeds Linux NAME_MAX")
                return original_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with mock.patch.object(
                    batch_download.os, "open", side_effect=linux_name_limit_open
                ):
                    BatchDownloadManager._atomic_publish(
                        source, directory_fd, destination_name
                    )
            finally:
                os.close(directory_fd)

            self.assertEqual((Path(root) / destination_name).read_bytes(), b"video")

    def test_episode_padding_uses_widest_number_with_two_digit_minimum(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: source)

            job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1, 100])
            self.wait_for_terminal_status(manager, job_id)

            destination = Path(root) / "123"
            self.assertTrue((destination / "S01E001 - Dub - 720p.mp4").is_file())
            self.assertTrue((destination / "S01E100 - Dub - 720p.mp4").is_file())
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
