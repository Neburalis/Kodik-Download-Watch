import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_main(real_flask=False):
    flask = types.ModuleType("flask")

    class Flask:
        def __init__(self, _name):
            self.config = {}

        def route(self, *_args, **_kwargs):
            return lambda function: function

    flask.Flask = Flask
    flask.render_template = mock.Mock()
    flask.request = types.SimpleNamespace(get_json=lambda silent=True: None, referrer=None)
    flask.redirect = mock.Mock()
    flask.abort = mock.Mock()
    flask.session = {}
    flask.send_file = mock.Mock()
    flask.g = types.SimpleNamespace(is_mobile=False)
    flask.jsonify = lambda mapping=None, **values: mapping if mapping is not None else values

    flask_socketio = types.ModuleType("flask_socketio")

    class SocketIO:
        def __init__(self, _app):
            pass

        def on(self, *_args, **_kwargs):
            return lambda function: function

        def send(self, *_args, **_kwargs):
            pass

    flask_socketio.SocketIO = SocketIO
    flask_socketio.send = mock.Mock()
    flask_socketio.emit = mock.Mock()
    flask_socketio.join_room = mock.Mock()
    flask_socketio.leave_room = mock.Mock()

    flask_mobility = types.ModuleType("flask_mobility")
    flask_mobility.Mobility = lambda _app: None

    getters = types.ModuleType("getters")
    getters.__all__ = ["test_shiki", "USE_KODIK_SEARCH", "get_serial_info"]
    getters.test_shiki = lambda: None
    getters.USE_KODIK_SEARCH = False
    getters.get_download_link = lambda *_args, **_kwargs: ""
    getters.get_serial_info = lambda *_args, **_kwargs: {}

    watch_together = types.ModuleType("watch_together")
    watch_together.Manager = lambda _remove_time: mock.Mock()

    dependencies = {
        "flask_socketio": flask_socketio,
        "flask_mobility": flask_mobility,
        "getters": getters,
        "watch_together": watch_together,
    }
    if not real_flask:
        dependencies["flask"] = flask
    module_name = "main_flask_response_test" if real_flask else "main_runtime_test"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, dependencies):
        spec.loader.exec_module(module)
    return module


class MainRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main()
        cls.flask_main = load_main(real_flask=True)

    def test_fast_download_uses_human_readable_http_download_name(self):
        send_file = mock.Mock(return_value="response")
        fast_download = types.ModuleType("fast_download")
        source = mock.Mock()
        fast_download.fast_download_open = mock.Mock(return_value=("hash~", None, source))
        with mock.patch.object(self.main, "send_file", send_file), mock.patch.dict(
            sys.modules, {"fast_download": fast_download}
        ):
            response = self.main.fast_download_work("kp", "123", 1, "0", "720", 12)

        self.assertEqual(response, "response")
        send_file.assert_called_once_with(
            source,
            as_attachment=True,
            download_name="Серия-01-Перевод-Неизвестно-720p.mp4",
        )

    def test_fast_download_response_encodes_unicode_content_disposition(self):
        fast_download = types.ModuleType("fast_download")
        source_path = ROOT / "tests" / ".content-disposition-video.mp4"
        source_path.write_bytes(b"video")
        source = source_path.open("rb")
        fast_download.fast_download_open = mock.Mock(
            return_value=("hash~", None, source)
        )
        try:
            with mock.patch.dict(sys.modules, {"fast_download": fast_download}):
                response = self.flask_main.app.test_client().get(
                    "/fast_download_act/kp-123-1-0-720-12/"
                )

            disposition = response.headers["Content-Disposition"]
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment;", disposition)
            self.assertIn("filename*=UTF-8''", disposition)
            self.assertIn("%D0%A1%D0%B5%D1%80%D0%B8%D1%8F", disposition)
        finally:
            response.close()
            source.close()
            source_path.unlink(missing_ok=True)

    def test_fast_download_closes_source_when_response_setup_fails(self):
        fast_download = types.ModuleType("fast_download")
        source = mock.Mock()
        fast_download.fast_download_open = mock.Mock(return_value=("hash~", None, source))
        with mock.patch.object(
            self.main, "send_file", side_effect=RuntimeError("response failure")
        ), mock.patch.dict(sys.modules, {"fast_download": fast_download}):
            with self.assertRaisesRegex(RuntimeError, "response failure"):
                self.main.fast_download_work("kp", "123", 1, "0", "720", 12)

        source.close.assert_called_once_with()

    def test_fast_download_caches_parser_url_and_skip_segments(self):
        fast_download = types.ModuleType("fast_download")
        link_data = ("//cdn.example/", ["360", "720"], [[0, 90], [1300, 1400]])
        fast_download.fast_download_open = mock.Mock(
            return_value=("hash~", link_data, mock.Mock())
        )
        cache = mock.Mock()
        with mock.patch.object(self.main, "ch_save", True), mock.patch.object(
            self.main, "ch", cache, create=True
        ), mock.patch.object(
            self.main.config, "USE_SAVED_DATA", False
        ), mock.patch.object(
            self.main, "_get_download_media_metadata", return_value={}
        ), mock.patch.dict(sys.modules, {"fast_download": fast_download}):
            self.main.fast_download_work("sh", "17895", 1, "610", "720", 12)

        cache.add_seria.assert_called_once_with(
            "sh17895", "610", 1, "//cdn.example/", [[0, 90], [1300, 1400]]
        )

    def test_batch_accepts_translation_returned_by_fresh_kodik_data(self):
        payload = {
            "serv": "kp",
            "anime_id": "123",
            "translation_id": "fresh-42",
            "quality": "720",
            "first_episode": 1,
            "last_episode": 1,
        }
        serial_data = {
            "series_count": 1,
            "translations": [
                {"id": "fresh-42", "name": "Current Kodik Dub", "series_range": [1, 1]}
            ],
        }
        manager = mock.Mock()
        manager.start_job.return_value = "job-1"
        manager.get_status.return_value = {"job_id": "job-1", "status": "queued"}
        with mock.patch.object(
            self.main.request, "get_json", return_value=payload
        ), mock.patch.object(
            self.main, "_get_batch_serial_data", return_value=serial_data
        ), mock.patch.object(self.main, "batch_download_manager", manager), mock.patch.object(
            self.main, "ch_use", False
        ):
            response, status = self.main.start_batch_download()

        self.assertEqual(status, 202, response)
        self.assertEqual(response["job_id"], "job-1")
        self.assertEqual(manager.start_job.call_args.kwargs["translation_name"], "Current Kodik Dub")

    def test_batch_serial_data_ignores_stale_application_cache(self):
        stale = {
            "series_count": 1,
            "translations": [{"id": "old", "name": "Old Dub", "series_range": [1, 1]}],
        }
        current = {
            "series_count": 1,
            "translations": [{"id": "new", "name": "Current Dub", "series_range": [1, 1]}],
        }
        cache = mock.Mock()
        cache.is_id.return_value = True
        cache.get_data_by_id.return_value = {"serial_data": stale}

        with mock.patch.object(self.main, "ch_use", True), mock.patch.object(
            self.main, "ch", cache, create=True
        ), mock.patch.object(
            self.main, "get_serial_info", return_value=current
        ) as get_serial_info:
            result = self.main._get_batch_serial_data("sh", "17895")

        self.assertEqual(result, current)
        get_serial_info.assert_called_once_with("17895", "shikimori", self.main.token)
        cache.get_data_by_id.assert_not_called()

    def test_fast_shikimori_download_continues_without_metadata(self):
        send_file = mock.Mock(return_value="response")
        fast_download = types.ModuleType("fast_download")
        fast_download.fast_download_open = mock.Mock(
            return_value=("hash~", None, mock.Mock())
        )
        with mock.patch.object(self.main, "send_file", send_file), mock.patch.object(
            self.main,
            "fetch_shikimori_metadata",
            side_effect=RuntimeError("Shikimori unavailable"),
        ), mock.patch.dict(sys.modules, {"fast_download": fast_download}):
            response = self.main.fast_download_work("sh", "17895", 1, "0", "720", 12)

        self.assertEqual(response, "response")
        self.assertEqual(fast_download.fast_download_open.call_args.kwargs["metadata"], {})

    def test_batch_shikimori_download_fails_closed_without_metadata(self):
        payload = {
            "serv": "sh",
            "anime_id": "17895",
            "translation_id": "610",
            "quality": "720",
            "first_episode": 1,
            "last_episode": 1,
        }
        serial_data = {
            "series_count": 1,
            "translations": [
                {"id": "610", "name": "AniLibria.TV", "series_range": [1, 1]}
            ],
        }
        manager = mock.Mock()
        with mock.patch.object(
            self.main.request, "get_json", return_value=payload
        ), mock.patch.object(
            self.main, "_get_batch_serial_data", return_value=serial_data
        ), mock.patch.object(
            self.main,
            "_get_download_media_metadata",
            side_effect=RuntimeError("Shikimori unavailable"),
        ), mock.patch.object(self.main, "batch_download_manager", manager):
            response, status = self.main.start_batch_download()

        self.assertEqual(status, 502)
        self.assertEqual(response["error"], "Не удалось получить метаданные Shikimori")
        manager.start_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
