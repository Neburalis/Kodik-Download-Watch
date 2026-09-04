import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


# fast_download imports these helpers from getters at import time. The tests
# exercise only local download/assembly behavior, so no Kodik API is needed.
fake_getters = types.ModuleType("getters")
fake_getters.get_download_link = lambda *args, **kwargs: ""
fake_getters.get_url_data = lambda *args, **kwargs: ""
sys.modules.setdefault("getters", fake_getters)
fast_download = importlib.import_module("fast_download")


class FastDownloadAssemblyTests(unittest.TestCase):
    def test_combine_segments_creates_mp4_on_posix(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            segment = directory_path / "0.ts"
            subprocess.run(
                [
                    "ffmpeg", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=1000",
                    "-t", "1", "-c:v", "libx264", "-c:a", "aac",
                    "-f", "mpegts", str(segment),
                ],
                check=True,
            )

            fast_download.combine_segments(
                directory + os.sep,
                segments_count=1,
                name="assembled",
                metadata={
                    "title": "Golden Time",
                    "original_title": "Golden Time",
                    "japanese_title": "ゴールデンタイム",
                    "date": "2013-10-04",
                },
            )

            output = directory_path / "assembled.mp4"
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            probe = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe", "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", str(output),
                    ],
                    text=True,
                )
            )
            video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
            audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual(video["pix_fmt"], "yuv420p")
            self.assertEqual(video["avg_frame_rate"], "24000/1001")
            self.assertEqual(audio["codec_name"], "aac")
            self.assertEqual(audio["sample_rate"], "48000")
            tags = probe["format"]["tags"]
            self.assertEqual(tags["title"], "Golden Time")
            self.assertEqual(tags["original_title"], "Golden Time")
            self.assertEqual(tags["japanese_title"], "ゴールデンタイム")
            self.assertEqual(tags["date"], "2013-10-04")

    def test_combine_segments_uses_requested_normalization_profile(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            fast_download.subprocess, "run"
        ) as run:
            Path(directory, "0.ts").write_bytes(b"segment")
            fast_download.combine_segments(
                directory + os.sep,
                segments_count=1,
                name="normalized",
                metadata={"title": "Golden Time"},
            )

        command = run.call_args.args[0]
        self.assertIn(["-map", "0:v:0", "-map", "0:a:0"], [command[index:index + 4] for index in range(len(command) - 3)])
        expected_pairs = {
            "-vf": "settb=AVTB,setpts=PTS-STARTPTS,fps=24000/1001",
            "-c:v": "libx264",
            "-preset": "medium",
            "-crf": "18",
            "-pix_fmt": "yuv420p",
            "-r:v": "24000/1001",
            "-fps_mode:v": "cfr",
            "-af": "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
            "-c:a": "aac",
            "-b:a": "160k",
            "-ar": "48000",
        }
        for flag, value in expected_pairs.items():
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn("copy", command)
        self.assertIn("+faststart", command[command.index("-movflags") + 1])
        self.assertIn("use_metadata_tags", command[command.index("-movflags") + 1])
        self.assertEqual(command[command.index("-safe") + 1], "1")

    def test_cache_hash_changes_with_normalization_profile_and_metadata(self):
        plain = fast_download.build_cache_hash("1", "sh", "610", 1, "720", {})
        tagged = fast_download.build_cache_hash(
            "1", "sh", "610", 1, "720", {"title": "Golden Time"}
        )
        self.assertNotEqual(plain, tagged)
        self.assertNotEqual(plain, fast_download.md5(b"1sh6101720").hexdigest() + "~")


class FastDownloadNetworkTests(unittest.TestCase):
    def test_get_path_never_exposes_hidden_or_empty_partial_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                cache_directory = Path("tmp") / "hash~"
                cache_directory.mkdir(parents=True)
                (cache_directory / ".encode-partial.mp4").write_bytes(b"partial")
                (cache_directory / "result.mp4").write_bytes(b"")

                with self.assertRaises(FileNotFoundError):
                    fast_download.get_path("hash~")

                (cache_directory / "result.mp4").write_bytes(b"complete")
                self.assertEqual(
                    Path(fast_download.get_path("hash~")).read_bytes(),
                    b"complete",
                )
            finally:
                os.chdir(previous_directory)

    def test_download_segment_retries_transient_http_failures(self):
        class Handler(BaseHTTPRequestHandler):
            attempts = 0

            def do_GET(self):
                Handler.attempts += 1
                if Handler.attempts < 3:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"temporary failure")
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"segment-data")

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "segment.ts"
                url = f"http://127.0.0.1:{server.server_port}/segment.ts"
                fast_download.download_segment(url, str(output))
                self.assertEqual(output.read_bytes(), b"segment-data")
                self.assertEqual(Handler.attempts, 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_manifest_fetch_retries_with_timeout(self):
        timeout_error = fast_download.requests.exceptions.Timeout("slow CDN")
        response = mock.Mock()
        response.text = "manifest"
        response.raise_for_status.return_value = None
        with mock.patch.object(
            fast_download.requests, "get", side_effect=[timeout_error, response]
        ) as request_get, mock.patch.object(fast_download.time, "sleep"):
            result = fast_download.get_url_data_with_retries(
                "https://cdn.example/manifest.m3u8", attempts=2
            )

        self.assertEqual(result, "manifest")
        self.assertEqual(request_get.call_count, 2)
        request_get.assert_called_with("https://cdn.example/manifest.m3u8", timeout=30)

    def test_fast_download_propagates_segment_failure(self):
        segment_error = fast_download.requests.exceptions.SSLError("expired certificate")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            fast_download, "get_download_link", return_value=("//cdn.example/", None, [])
        ), mock.patch.object(
            fast_download, "get_url_data_with_retries", return_value="manifest"
        ), mock.patch.object(
            fast_download, "get_segments", return_value=[["https://cdn.example/0.ts", "0"]]
        ), mock.patch.object(
            fast_download, "download_segment", side_effect=segment_error
        ), mock.patch.object(
            fast_download, "combine_segments"
        ), mock.patch.object(
            fast_download, "check_ffmpeg"
        ):
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                with self.assertRaises(fast_download.requests.exceptions.SSLError):
                    fast_download.fast_download("1", "sh", 1, "610", "720", None)
                expected_hash = fast_download.build_cache_hash(
                    "1", "sh", "610", 1, "720", {}
                )
                self.assertFalse((Path("tmp") / expected_hash).exists())
            finally:
                os.chdir(previous_directory)

    def test_manifest_segment_names_never_become_local_paths_or_concat_directives(self):
        manifest = """#EXTM3U
#EXTINF:1,
../escape.ts
#EXTINF:1,
https://other.example/video/part-2.ts?token=x
#EXTINF:1,
quote'and-newline.ts
# malicious concat directive is ignored
file /tmp/owned.ts
"""

        segments = fast_download.get_segments(manifest, "https://cdn.example/media/")

        self.assertEqual(
            segments,
            [
                ["https://cdn.example/escape.ts", "0"],
                ["https://other.example/video/part-2.ts?token=x", "1"],
                ["https://cdn.example/media/quote'and-newline.ts", "2"],
            ],
        )

    def test_interrupted_transcode_removes_segments_and_partial_output(self):
        def write_segment(_url, path):
            Path(path).write_bytes(b"segment")

        def interrupted_combine(
            directory, segments_count, name="result", metadata=None, hwaccel=None
        ):
            Path(directory, f"{name}.mp4").write_bytes(b"partial")
            raise subprocess.CalledProcessError(1, ["ffmpeg"])

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            fast_download, "check_ffmpeg"
        ), mock.patch.object(
            fast_download, "get_download_link", return_value=("//cdn.example/", None, [])
        ), mock.patch.object(
            fast_download, "get_url_data_with_retries", return_value="manifest"
        ), mock.patch.object(
            fast_download,
            "get_segments",
            return_value=[["https://cdn.example/0.ts", "0"]],
        ), mock.patch.object(
            fast_download, "download_segment", side_effect=write_segment
        ), mock.patch.object(
            fast_download, "combine_segments", side_effect=interrupted_combine
        ):
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                with self.assertRaises(subprocess.CalledProcessError):
                    fast_download.fast_download("1", "sh", 1, "610", "720", None)
                expected_hash = fast_download.build_cache_hash(
                    "1", "sh", "610", 1, "720", {}
                )
                self.assertFalse((Path("tmp") / expected_hash).exists())
            finally:
                os.chdir(previous_directory)

    def test_fast_download_retries_manifest_failure(self):
        manifest_error = fast_download.requests.exceptions.SSLError("expired certificate")
        manifest_response = mock.Mock()
        manifest_response.text = "manifest"
        manifest_response.raise_for_status.return_value = None

        def fake_combine(directory, segments_count, name="result", metadata=None, hwaccel=None):
            Path(directory, f"{name}.mp4").write_bytes(b"video")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            fast_download, "get_download_link", return_value=("//cdn.example/", None, [])
        ), mock.patch.object(
            fast_download.requests, "get", side_effect=[manifest_error, manifest_response]
        ) as request_get, mock.patch.object(
            fast_download.time, "sleep"
        ), mock.patch.object(
            fast_download, "get_segments", return_value=[["https://cdn.example/0.ts", "0"]]
        ), mock.patch.object(
            fast_download, "download_segment"
        ), mock.patch.object(
            fast_download, "combine_segments", side_effect=fake_combine
        ), mock.patch.object(
            fast_download, "check_ffmpeg"
        ):
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                result = fast_download.fast_download("1", "sh", 1, "610", "720", None)
                self.assertEqual(result[1], "//cdn.example/")
                self.assertEqual(request_get.call_count, 2)
                request_get.assert_called_with(
                    "https://cdn.example/720.mp4:hls:manifest.m3u8", timeout=30
                )
            finally:
                os.chdir(previous_directory)

    def test_duplicate_fast_downloads_for_same_episode_are_serialized(self):
        entered_download = threading.Event()
        release_download = threading.Event()

        def blocked_download(_link, path):
            entered_download.set()
            self.assertTrue(release_download.wait(2))
            Path(path).write_bytes(b"segment")

        def fake_combine(directory, segments_count, name="result", metadata=None, hwaccel=None):
            Path(directory, f"{name}.mp4").write_bytes(b"video")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            fast_download, "get_download_link", return_value=("//cdn.example/", None, [])
        ) as get_download_link, mock.patch.object(
            fast_download, "get_url_data_with_retries", return_value="manifest"
        ), mock.patch.object(
            fast_download, "get_segments", return_value=[["https://cdn.example/0.ts", "0"]]
        ), mock.patch.object(
            fast_download, "download_segment", side_effect=blocked_download
        ), mock.patch.object(
            fast_download, "combine_segments", side_effect=fake_combine
        ), mock.patch.object(
            fast_download, "check_ffmpeg"
        ):
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                results = []
                errors = []

                def run_download():
                    try:
                        results.append(fast_download.fast_download("1", "sh", 1, "610", "720", None))
                    except Exception as exc:
                        errors.append(exc)

                first = threading.Thread(target=run_download)
                second = threading.Thread(target=run_download)
                first.start()
                self.assertTrue(entered_download.wait(1))
                second.start()
                deadline = time.monotonic() + 0.5
                while get_download_link.call_count < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                release_download.set()
                first.join(2)
                second.join(2)

                self.assertEqual(errors, [])
                self.assertEqual(get_download_link.call_count, 1)
                self.assertEqual(len(results), 2)
                self.assertEqual({result[1] for result in results}, {"//cdn.example/", None})
            finally:
                release_download.set()
                os.chdir(previous_directory)

    def test_fast_download_releases_hash_lock_registry_entries(self):
        with mock.patch.object(fast_download, "check_ffmpeg"), mock.patch.object(
            fast_download, "_fast_download_locked", return_value=("hash", None)
        ):
            for item in range(100):
                fast_download.fast_download(str(item), "sh", 1, "610", "720", None)
        self.assertEqual(fast_download._download_locks, {})

    def test_download_lock_serializes_independent_processes(self):
        worker = r'''
import importlib
import os
import sys
import time
import types
fake_getters = types.ModuleType("getters")
fake_getters.get_download_link = lambda *args, **kwargs: ""
sys.modules["getters"] = fake_getters
fast_download = importlib.import_module("fast_download")
os.chdir(os.environ["LOCK_TEST_ROOT"])
with fast_download._download_lock("shared"):
    with open("events.log", "a", encoding="ascii") as stream:
        stream.write("enter " + os.environ["WORKER_ID"] + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    time.sleep(0.25)
    with open("events.log", "a", encoding="ascii") as stream:
        stream.write("exit " + os.environ["WORKER_ID"] + "\n")
        stream.flush()
        os.fsync(stream.fileno())
'''
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["LOCK_TEST_ROOT"] = directory
            first_environment = environment | {"WORKER_ID": "first"}
            second_environment = environment | {"WORKER_ID": "second"}
            first = subprocess.Popen([sys.executable, "-c", worker], env=first_environment)
            time.sleep(0.05)
            second = subprocess.Popen([sys.executable, "-c", worker], env=second_environment)
            self.assertEqual(first.wait(timeout=5), 0)
            self.assertEqual(second.wait(timeout=5), 0)
            events = Path(directory, "events.log").read_text(encoding="ascii").splitlines()

        self.assertIn(
            events,
            [
                ["enter first", "exit first", "enter second", "exit second"],
                ["enter second", "exit second", "enter first", "exit first"],
            ],
        )

    def test_startup_cleanup_does_not_delete_an_active_download(self):
        worker = r'''
import importlib
import os
import sys
import types
fake_getters = types.ModuleType("getters")
fake_getters.get_download_link = lambda *args, **kwargs: ""
sys.modules["getters"] = fake_getters
fast_download = importlib.import_module("fast_download")
os.chdir(os.environ["LOCK_TEST_ROOT"])
with fast_download._download_lock("shared"):
    os.makedirs("tmp/shared", exist_ok=True)
    with open("tmp/shared/0.ts", "wb") as stream:
        stream.write(b"active")
    with open("ready", "w", encoding="ascii") as stream:
        stream.write("ready")
    while not os.path.exists("release"):
        pass
'''
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["LOCK_TEST_ROOT"] = directory
            process = subprocess.Popen([sys.executable, "-c", worker], env=environment)
            ready = Path(directory, "ready")
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())

            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                fast_download.clear_tmp()
                self.assertEqual(Path("tmp/shared/0.ts").read_bytes(), b"active")
            finally:
                Path(directory, "release").touch()
                os.chdir(previous_directory)
                self.assertEqual(process.wait(timeout=5), 0)

    def test_fast_download_discards_zero_byte_cached_mp4(self):
        def fake_combine(directory, segments_count, name="result", metadata=None, hwaccel=None):
            Path(directory, f"{name}.mp4").write_bytes(b"video")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            fast_download, "check_ffmpeg"
        ), mock.patch.object(
            fast_download, "get_download_link", return_value=("//cdn.example/", None, [])
        ) as get_download_link, mock.patch.object(
            fast_download, "get_url_data_with_retries", return_value="manifest"
        ), mock.patch.object(
            fast_download, "get_segments", return_value=[["https://cdn.example/0.ts", "0"]]
        ), mock.patch.object(
            fast_download, "download_segment", side_effect=lambda _url, path: Path(path).write_bytes(b"segment")
        ), mock.patch.object(
            fast_download, "combine_segments", side_effect=fake_combine
        ):
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                expected_hash = fast_download.build_cache_hash("1", "sh", "610", 1, "720", {})
                cached_directory = Path("tmp") / expected_hash
                cached_directory.mkdir(parents=True)
                (cached_directory / "broken.mp4").write_bytes(b"")

                result = fast_download.fast_download("1", "sh", 1, "610", "720", None)

                self.assertEqual(result, (expected_hash, "//cdn.example/"))
                self.assertEqual(get_download_link.call_count, 1)
                self.assertEqual((cached_directory / "result.mp4").read_bytes(), b"video")
            finally:
                os.chdir(previous_directory)

    def test_fast_download_removes_hls_segments_after_successful_assembly(self):
        def write_segment(_url, path):
            Path(path).write_bytes(b"segment")

        def fake_combine(directory, segments_count, name="result", metadata=None, hwaccel=None):
            Path(directory, "files.txt").write_text("segments", encoding="utf-8")
            Path(directory, f"{name}.mp4").write_bytes(b"video")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            fast_download, "check_ffmpeg"
        ), mock.patch.object(
            fast_download, "get_download_link", return_value=("//cdn.example/", None, [])
        ), mock.patch.object(
            fast_download, "get_url_data_with_retries", return_value="manifest"
        ), mock.patch.object(
            fast_download, "get_segments", return_value=[
                ["https://cdn.example/0.ts", "0"],
                ["https://cdn.example/1.ts", "1"],
            ]
        ), mock.patch.object(
            fast_download, "download_segment", side_effect=write_segment
        ), mock.patch.object(
            fast_download, "combine_segments", side_effect=fake_combine
        ):
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                download_hash, _ = fast_download.fast_download("1", "sh", 1, "610", "720", None)
                remaining = sorted(path.name for path in (Path("tmp") / download_hash).iterdir())
                self.assertEqual(remaining, ["result.mp4"])
            finally:
                os.chdir(previous_directory)

    def test_fast_download_rejects_legacy_custom_mp4_cache_entry(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            fast_download, "check_ffmpeg"
        ), mock.patch.object(
            fast_download, "get_download_link", side_effect=RuntimeError("fresh download")
        ):
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                expected_hash = fast_download.build_cache_hash("1", "sh", "610", 1, "720", {})
                cached_directory = Path("tmp") / expected_hash
                cached_directory.mkdir(parents=True)
                (cached_directory / "cached.mp4").write_bytes(b"video")

                with self.assertRaisesRegex(RuntimeError, "fresh download"):
                    fast_download.fast_download("1", "sh", 1, "610", "720", None)
            finally:
                os.chdir(previous_directory)


if __name__ == "__main__":
    unittest.main()
