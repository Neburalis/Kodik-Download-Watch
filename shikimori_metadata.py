"""Fetch Shikimori title data and map it to MP4 container tags."""

from __future__ import annotations

from functools import lru_cache
import html
import re
import time

import requests

import config


_GRAPHQL_QUERY = """
query($ids: String!) {
  animes(ids: $ids, limit: 1) {
    id
    name
    russian
    english
    japanese
    kind
    rating
    status
    score
    episodes
    duration
    airedOn { year date }
    releasedOn { year date }
    genres { name russian }
    studios { name }
    description
  }
}
"""


def normalise_shikimori_id(anime_id: object) -> str:
    if isinstance(anime_id, bool):
        raise ValueError("invalid Shikimori anime id")
    match = re.fullmatch(r"z?(\d+)", str(anime_id))
    if match is None:
        raise ValueError("invalid Shikimori anime id")
    return match.group(1)


def _graphql_url() -> str:
    domain = config.SHIKIMORI_MIRROR or "shikimori.io"
    domain = domain.rstrip("/")
    if not domain.startswith(("http://", "https://")):
        domain = "https://" + domain
    return domain + "/api/graphql"


def _proxy_configuration() -> dict[str, str] | None:
    if not config.SHIKI_PROXY:
        return None
    return {"http": config.SHIKI_PROXY, "https": config.SHIKI_PROXY}


@lru_cache(maxsize=512)
def fetch_shikimori_metadata(anime_id: object) -> dict:
    """Fetch one exact title from Shikimori with bounded retries and timeouts."""
    numeric_id = normalise_shikimori_id(anime_id)
    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(
                _graphql_url(),
                json={"query": _GRAPHQL_QUERY, "variables": {"ids": numeric_id}},
                headers={"User-Agent": "kodik-download-watch/1.0"},
                timeout=(10, 30),
                proxies=_proxy_configuration(),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == 2:
                raise RuntimeError("failed to fetch Shikimori metadata") from exc
            time.sleep(0.5 * (2**attempt))
            continue

        if payload.get("errors"):
            raise RuntimeError(f"Shikimori GraphQL error: {payload['errors'][0].get('message', 'unknown error')}")
        titles = payload.get("data", {}).get("animes", [])
        if not titles:
            raise LookupError(f"Shikimori anime {numeric_id} was not found")
        title = titles[0]
        if str(title.get("id")) != numeric_id:
            raise LookupError(f"Shikimori returned a different anime for {numeric_id}")
        return title

    raise RuntimeError("failed to fetch Shikimori metadata") from last_error


def _clean_text(value: object, limit: int = 8000) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\[/?[^\]]+\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def _date_value(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return _clean_text(value.get("date") or value.get("year"), limit=32)


def build_mp4_metadata(
    shikimori_data: dict,
    *,
    anime_id: object,
    episode: int,
    translation: str,
) -> dict[str, str]:
    """Build deterministic string-only MP4 tags for one episode or movie."""
    numeric_id = normalise_shikimori_id(anime_id)
    original_title = _clean_text(
        shikimori_data.get("name")
        or shikimori_data.get("english")
        or shikimori_data.get("russian")
        or f"Shikimori {numeric_id}",
        limit=512,
    )
    description = _clean_text(shikimori_data.get("description"))
    native_title = _clean_text(
        shikimori_data.get("japanese") or original_title,
        limit=512,
    )
    genres = ", ".join(
        _clean_text(item.get("name"), limit=128)
        for item in shikimori_data.get("genres") or []
        if isinstance(item, dict) and item.get("name")
    )
    studios = ", ".join(
        _clean_text(item.get("name"), limit=128)
        for item in shikimori_data.get("studios") or []
        if isinstance(item, dict) and item.get("name")
    )
    canonical_url = f"https://shikimori.io/animes/{numeric_id}"

    candidates = {
        "title": original_title,
        "show": original_title,
        "album": original_title,
        "original_title": native_title,
        "russian_title": shikimori_data.get("russian"),
        "english_title": shikimori_data.get("english"),
        "japanese_title": shikimori_data.get("japanese"),
        "date": _date_value(shikimori_data.get("airedOn")),
        "release_date": _date_value(shikimori_data.get("releasedOn")),
        "genre": genres,
        "studio": studios,
        "description": description,
        "synopsis": description,
        "comment": f"Shikimori: {canonical_url}",
        "rating": shikimori_data.get("rating"),
        "status": shikimori_data.get("status"),
        "score": shikimori_data.get("score"),
        "media_type": shikimori_data.get("kind"),
        "episode_count": shikimori_data.get("episodes"),
        "duration_minutes": shikimori_data.get("duration"),
        "artist": translation,
        "shikimori_id": numeric_id,
        "shikimori_url": canonical_url,
        "encoded_by": "kodik-download-watch",
    }
    tags = {
        key: _clean_text(value)
        for key, value in candidates.items()
        if value is not None and _clean_text(value)
    }
    if episode > 0:
        tags.update(
            {
                "episode_id": str(episode),
                "episode_sort": str(episode),
                "season_number": "1",
                "track": str(episode),
            }
        )
    return tags
