import unittest
from unittest import mock

import requests

import shikimori_metadata


class ShikimoriMetadataTests(unittest.TestCase):
    def setUp(self):
        shikimori_metadata.fetch_shikimori_metadata.cache_clear()
        self.raw = {
            "id": "17895",
            "name": "Golden Time",
            "russian": "Золотая пора",
            "english": "Golden Time",
            "japanese": "ゴールデンタイム",
            "kind": "tv",
            "rating": "pg_13",
            "status": "released",
            "score": 7.74,
            "episodes": 24,
            "duration": 24,
            "airedOn": {"year": 2013, "date": "2013-10-04"},
            "releasedOn": {"year": 2014, "date": "2014-03-28"},
            "genres": [
                {"name": "Drama", "russian": "Драма"},
                {"name": "Romance", "russian": "Романтика"},
            ],
            "studios": [{"name": "J.C.Staff"}],
            "description": "История [character=1]героя[/character].\n\n Продолжение.",
        }

    def test_fetches_exact_anime_from_shikimori_graphql_with_timeout(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"animes": [self.raw]}}
        with mock.patch("shikimori_metadata.requests.post", return_value=response) as post:
            result = shikimori_metadata.fetch_shikimori_metadata("z17895")

        self.assertEqual(result["name"], "Golden Time")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["variables"], {"ids": "17895"})
        self.assertEqual(kwargs["timeout"], (10, 30))
        self.assertIn("User-Agent", kwargs["headers"])

    def test_retries_transient_shikimori_failure(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"animes": [self.raw]}}
        with mock.patch(
            "shikimori_metadata.requests.post",
            side_effect=[requests.exceptions.Timeout("slow"), response],
        ) as post, mock.patch("shikimori_metadata.time.sleep"):
            result = shikimori_metadata.fetch_shikimori_metadata("17895")

        self.assertEqual(result["id"], "17895")
        self.assertEqual(post.call_count, 2)

    def test_rejects_invalid_or_missing_shikimori_result(self):
        for anime_id in ("", "../17895", "kp123"):
            with self.subTest(anime_id=anime_id):
                with self.assertRaises(ValueError):
                    shikimori_metadata.normalise_shikimori_id(anime_id)

        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"animes": []}}
        with mock.patch("shikimori_metadata.requests.post", return_value=response):
            with self.assertRaises(LookupError):
                shikimori_metadata.fetch_shikimori_metadata("17895")

    def test_builds_unicode_mp4_tags_with_original_title_and_release_dates(self):
        tags = shikimori_metadata.build_mp4_metadata(
            self.raw,
            anime_id="z17895",
            episode=7,
            translation="AniLibria.TV",
        )

        self.assertEqual(tags["title"], "Golden Time")
        self.assertEqual(tags["show"], "Golden Time")
        self.assertEqual(tags["original_title"], "ゴールデンタイム")
        self.assertEqual(tags["russian_title"], "Золотая пора")
        self.assertEqual(tags["japanese_title"], "ゴールデンタイム")
        self.assertEqual(tags["date"], "2013-10-04")
        self.assertEqual(tags["release_date"], "2014-03-28")
        self.assertEqual(tags["genre"], "Drama, Romance")
        self.assertEqual(tags["studio"], "J.C.Staff")
        self.assertEqual(tags["episode_id"], "7")
        self.assertEqual(tags["episode_sort"], "7")
        self.assertEqual(tags["season_number"], "1")
        self.assertEqual(tags["artist"], "AniLibria.TV")
        self.assertEqual(tags["description"], "История героя. Продолжение.")
        self.assertIn("https://shikimori.io/animes/17895", tags["comment"])
        self.assertTrue(all(isinstance(value, str) for value in tags.values()))

    def test_movie_metadata_omits_episode_and_season_tags(self):
        tags = shikimori_metadata.build_mp4_metadata(
            self.raw,
            anime_id="17895",
            episode=0,
            translation="Dub",
        )
        self.assertNotIn("episode_id", tags)
        self.assertNotIn("episode_sort", tags)
        self.assertNotIn("season_number", tags)


if __name__ == "__main__":
    unittest.main()
