import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import batch_download
from batch_download import (
    BatchDownloadManager,
    BatchQueueFullError,
    validate_batch_selection,
)


def TemporaryDirectory():
    return tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())


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

    def test_rejects_translation_range_beyond_title_series_count(self):
        serial_data = {
            "series_count": 2,
            "translations": [
                {"id": "610", "name": "AniLibria.TV", "series_range": [1, 99]},
            ],
        }

        translation = validate_batch_selection(serial_data, "610", 1, 2)
        self.assertEqual(translation["id"], "610")
        with self.assertRaises(ValueError):
            validate_batch_selection(serial_data, "610", 3, 3)

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

            output = Path(root) / "123" / "S01E01 - AniLibria.TV [610-01ce4b291ad3ecd2] - 720p.mp4"
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

    def test_copies_an_open_source_after_its_path_is_removed(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source_path = Path(sources) / "download.mp4"
            source_path.write_bytes(b"video-data")

            def downloader(**request):
                source = source_path.open("rb")
                source_path.unlink()
                return source

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1])
            status = self.wait_for_terminal_status(manager, job_id)

            output = Path(root) / "123" / "S01E01 - Dub [610-01ce4b291ad3ecd2] - 720p.mp4"
            self.assertEqual(status["status"], "completed")
            self.assertEqual(output.read_bytes(), b"video-data")
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

    def test_rejects_symlinked_ancestor_of_configured_media_root(self):
        with TemporaryDirectory() as parent, TemporaryDirectory() as outside:
            configured_parent = Path(parent) / "configured"
            configured_parent.mkdir()
            (configured_parent / "linked").symlink_to(outside, target_is_directory=True)
            media_root = configured_parent / "linked" / "shows"
            manager = BatchDownloadManager(
                media_root, episode_downloader=lambda **request: Path(outside)
            )

            job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1])
            status = self.wait_for_terminal_status(manager, job_id)

            self.assertEqual(status["status"], "completed_with_errors")
            self.assertEqual(status["episodes"][0]["state"], "failed")
            self.assertFalse((Path(outside) / "shows").exists())
            manager.shutdown()

    def test_media_root_creation_fsyncs_each_parent_before_descending(self):
        with TemporaryDirectory() as parent, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            media_root = Path(parent).resolve() / "level-one" / "level-two"
            events = []
            directory_fds = {}
            original_mkdir = os.mkdir
            original_open = os.open
            original_fsync = os.fsync

            def track_mkdir(path, mode=0o777, *, dir_fd=None):
                if path in {"level-one", "level-two", "123"}:
                    events.append(f"mkdir:{path}")
                return original_mkdir(path, mode, dir_fd=dir_fd)

            def track_open(path, flags, mode=0o777, *, dir_fd=None):
                result = original_open(path, flags, mode, dir_fd=dir_fd)
                if path in {"level-one", "level-two", "123"}:
                    events.append(f"open:{path}")
                    directory_fds[path] = result
                return result

            def track_fsync(file_descriptor):
                events.append("fsync")
                return original_fsync(file_descriptor)

            def downloader(**request):
                for file_descriptor in directory_fds.values():
                    os.fstat(file_descriptor)
                return source

            manager = BatchDownloadManager(media_root, episode_downloader=downloader)
            with mock.patch.object(batch_download.os, "mkdir", side_effect=track_mkdir), \
                    mock.patch.object(batch_download.os, "open", side_effect=track_open), \
                    mock.patch.object(batch_download.os, "fsync", side_effect=track_fsync):
                job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1])
                status = self.wait_for_terminal_status(manager, job_id)

            for component in ("level-one", "level-two", "123"):
                mkdir_index = events.index(f"mkdir:{component}")
                open_index = events.index(f"open:{component}")
                self.assertEqual(events[mkdir_index + 1], "fsync")
                self.assertLess(mkdir_index, open_index)
            self.assertEqual(status["status"], "completed")
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

    def test_source_marker_is_complete_before_atomic_publication(self):
        with TemporaryDirectory() as root:
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            original_link = os.link
            observed = []

            def inspect_link(source, destination, **kwargs):
                with open(Path(root) / source, "rb") as temporary:
                    observed.append(temporary.read())
                self.assertFalse((Path(root) / destination).exists())
                return original_link(source, destination, **kwargs)

            try:
                with mock.patch.object(batch_download.os, "link", side_effect=inspect_link):
                    BatchDownloadManager._claim_destination_source(directory_fd, "sh")
            finally:
                os.close(directory_fd)

            self.assertEqual(observed, [b"sh\n"])
            self.assertEqual((Path(root) / ".kodik-source").read_bytes(), b"sh\n")
            self.assertEqual([path.name for path in Path(root).iterdir()], [".kodik-source"])

    def test_source_marker_accepts_concurrent_same_source_winner(self):
        with TemporaryDirectory() as root:
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            calls = []

            def publish_winner(_source, destination, **kwargs):
                calls.append(destination)
                (Path(root) / destination).write_bytes(b"sh\n")
                raise FileExistsError(destination)

            try:
                with mock.patch.object(batch_download.os, "link", side_effect=publish_winner):
                    BatchDownloadManager._claim_destination_source(directory_fd, "sh")
            finally:
                os.close(directory_fd)

            self.assertEqual(calls, [".kodik-source"])
            self.assertEqual((Path(root) / ".kodik-source").read_bytes(), b"sh\n")

    def test_source_marker_retry_retries_directory_durability(self):
        with TemporaryDirectory() as root:
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            original_fsync = os.fsync
            directory_syncs = 0

            def fail_first_directory_sync(file_descriptor):
                nonlocal directory_syncs
                if batch_download.stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
                    directory_syncs += 1
                    if directory_syncs == 1:
                        raise OSError("post-link fsync failed")
                return original_fsync(file_descriptor)

            try:
                with mock.patch.object(
                    batch_download.os, "fsync", side_effect=fail_first_directory_sync
                ):
                    with self.assertRaisesRegex(OSError, "post-link fsync failed"):
                        BatchDownloadManager._claim_destination_source(directory_fd, "sh")

                self.assertEqual((Path(root) / ".kodik-source").read_bytes(), b"sh\n")
                with mock.patch.object(
                    batch_download.os,
                    "fsync",
                    side_effect=OSError("retry fsync failed"),
                ):
                    with self.assertRaisesRegex(OSError, "retry fsync failed"):
                        BatchDownloadManager._claim_destination_source(directory_fd, "sh")
            finally:
                os.close(directory_fd)

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
            output = destination / "S01E01 - Dub [610-01ce4b291ad3ecd2] - 720p.mp4"
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
            self.assertFalse((Path(root) / "123" / "S01E02 - Dub [610-01ce4b291ad3ecd2] - 480p.mp4").exists())
            manager.shutdown()

    def test_sanitizes_translation_component_in_final_filename(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: source)

            job_id = manager.start_job("sh", "123", "610", ' Dub/Bad:*?"<>|\n ', "360", [1])
            status = self.wait_for_terminal_status(manager, job_id)

            output = Path(root) / "123" / "S01E01 - Dub_Bad [610-01ce4b291ad3ecd2] - 360p.mp4"
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

    def test_translation_ids_prevent_sanitized_and_truncated_filename_collisions(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"video")
            manager = BatchDownloadManager(
                root, episode_downloader=lambda **request: source, max_active_jobs=4
            )

            jobs = [
                manager.start_job("sh", "123", "dub/one", "Same/Name", "720", [1]),
                manager.start_job("sh", "123", "dub:two", "Same:Name", "720", [1]),
                manager.start_job("sh", "123", "long-one", "Я" * 300 + "A", "720", [2]),
                manager.start_job("sh", "123", "long-two", "Я" * 300 + "B", "720", [2]),
            ]
            statuses = [self.wait_for_terminal_status(manager, job) for job in jobs]

            filenames = [Path(status["episodes"][0]["file"]).name for status in statuses]
            self.assertEqual(len(set(filenames)), 4)
            self.assertTrue(all(status["completed_count"] == 1 for status in statuses))
            self.assertTrue(all(status["skipped_count"] == 0 for status in statuses))
            self.assertTrue(all(len(name.encode("utf-8")) <= 255 for name in filenames))
            self.assertTrue(any("dub_one" in name for name in filenames))
            self.assertTrue(any("dub_two" in name for name in filenames))
            manager.shutdown()

    def test_episode_zero_uses_movie_filename(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "movie.mp4"
            source.write_bytes(b"movie")
            manager = BatchDownloadManager(root, episode_downloader=lambda **request: source)

            job_id = manager.start_job("kp", "42", "7", "Original", "720", [0])
            status = self.wait_for_terminal_status(manager, job_id)

            output = Path(root) / "42" / "Movie - Original [7-7902699be42c8a8e] - 720p.mp4"
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

            original_link = os.link

            def fail_episode_link(source_name, destination_name, **kwargs):
                if source_name.startswith(".batch-"):
                    raise OSError("disk failure")
                return original_link(source_name, destination_name, **kwargs)

            with mock.patch("batch_download.os.link", side_effect=fail_episode_link):
                job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1])
                status = self.wait_for_terminal_status(manager, job_id)

            destination = Path(root) / "123"
            final = destination / "S01E01 - Dub [610-01ce4b291ad3ecd2] - 720p.mp4"
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
                (destination / "S01E01 - Dub [610-01ce4b291ad3ecd2] - 720p.mp4").write_bytes(b"existing")
                return source

            manager = BatchDownloadManager(root, episode_downloader=downloader)
            job_id = manager.start_job("sh", "123", "610", "Dub", "720", [1])
            status = self.wait_for_terminal_status(manager, job_id)

            final = destination / "S01E01 - Dub [610-01ce4b291ad3ecd2] - 720p.mp4"
            self.assertEqual(final.read_bytes(), b"existing")
            self.assertEqual(status["episodes"][0]["state"], "skipped")
            self.assertEqual(status["skipped_count"], 1)
            self.assertEqual(status["completed_count"], 0)
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                [".kodik-source", "S01E01 - Dub [610-01ce4b291ad3ecd2] - 720p.mp4"],
            )
            manager.shutdown()

    def test_atomic_publish_rejects_unusable_existing_winner(self):
        creators = {
            "empty": lambda path, _target: path.write_bytes(b""),
            "symlink": lambda path, target: path.symlink_to(target),
            "directory": lambda path, _target: path.mkdir(),
        }
        for name, create_winner in creators.items():
            with self.subTest(winner=name), TemporaryDirectory() as root, TemporaryDirectory() as sources:
                source = Path(sources) / "source.mp4"
                source.write_bytes(b"new")
                target = Path(sources) / "target.mp4"
                target.write_bytes(b"target")
                final = Path(root) / "final.mp4"
                create_winner(final, target)
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(ValueError):
                        BatchDownloadManager._atomic_publish(source, directory_fd, final.name)
                finally:
                    os.close(directory_fd)

    def test_atomic_publish_syncs_directory_for_existing_winner(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"new")
            (Path(root) / "final.mp4").write_bytes(b"winner")
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            synced = []
            original_fsync = os.fsync

            def track_fsync(file_descriptor):
                synced.append(file_descriptor)
                return original_fsync(file_descriptor)

            try:
                with mock.patch.object(batch_download.os, "fsync", side_effect=track_fsync):
                    published = BatchDownloadManager._atomic_publish(
                        source, directory_fd, "final.mp4"
                    )
            finally:
                os.close(directory_fd)

            self.assertFalse(published)
            self.assertIn(directory_fd, synced)

    def test_atomic_publish_syncs_directory_after_failed_cleanup(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as sources:
            source = Path(sources) / "source.mp4"
            source.write_bytes(b"new")
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            synced = []
            original_fsync = os.fsync

            def track_fsync(file_descriptor):
                synced.append(file_descriptor)
                return original_fsync(file_descriptor)

            try:
                with mock.patch.object(
                    batch_download.os, "link", side_effect=OSError("disk failure")
                ), mock.patch.object(batch_download.os, "fsync", side_effect=track_fsync):
                    with self.assertRaisesRegex(OSError, "disk failure"):
                        BatchDownloadManager._atomic_publish(source, directory_fd, "final.mp4")
            finally:
                os.close(directory_fd)

            self.assertIn(directory_fd, synced)

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
            self.assertTrue((destination / "S01E001 - Dub [610-01ce4b291ad3ecd2] - 720p.mp4").is_file())
            self.assertTrue((destination / "S01E100 - Dub [610-01ce4b291ad3ecd2] - 720p.mp4").is_file())
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
