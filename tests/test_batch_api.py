import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BatchDownloadIntegrationTests(unittest.TestCase):
    def test_config_declares_anime_directory(self):
        tree = ast.parse((ROOT / "config.py").read_text(encoding="utf-8"))
        values = {
            target.id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id in {"ANIME_DIRECTORY", "MAX_BATCH_EPISODES"}
        }
        self.assertEqual(values.get("ANIME_DIRECTORY"), "/srv/smalldata/media/shows")
        self.assertGreaterEqual(values.get("MAX_BATCH_EPISODES", 0), 1000)

    def test_main_exposes_batch_start_and_status_routes(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        routes = set()
        function_names = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            function_names.add(node.name)
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                if isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "route":
                    try:
                        routes.add(ast.literal_eval(decorator.args[0]))
                    except (ValueError, TypeError):
                        pass
        self.assertIn("/batch_download/start/", routes)
        self.assertIn("/batch_download/status/<string:job_id>/", routes)
        self.assertIn("start_batch_download", function_names)
        self.assertIn("batch_download_status", function_names)
        self.assertIn("BatchDownloadManager", source)
        self.assertIn("request.get_json", source)
        self.assertIn("type(payload['first_episode']) is not int", source)
        self.assertIn("translation = validate_batch_selection", source)
        self.assertIn("_get_batch_serial_data", source)
        self.assertIn("validate_batch_selection", source)
        self.assertIn("fetch_shikimori_metadata", source)
        self.assertIn("build_mp4_metadata", source)
        self.assertIn("media_metadata=media_metadata", source)
        self.assertIn("BatchQueueFullError", source)
        self.assertIn("first_episode", source)
        self.assertIn("last_episode", source)
        self.assertIn("jsonify", source)

    def test_download_page_receives_batch_context(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("serv=serv", source)
        self.assertIn("anime_id=id", source)
        self.assertIn("translation_id=translation_id", source)


if __name__ == "__main__":
    unittest.main()
