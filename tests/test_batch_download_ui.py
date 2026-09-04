from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "download.html").read_text(encoding="utf-8")
JS_PATH = ROOT / "static" / "js" / "batch_download.js"


def test_template_renders_batch_controls_before_existing_episode_table():
    panel = TEMPLATE.index('id="batch-download-panel"')
    episode_table = TEMPLATE.index("{% for url in range(series[0], series[1]+1) %}")

    assert panel < episode_table
    assert "{% if series != 0 and series[1] >= series[0] %}" in TEMPLATE
    assert "Скачать все серии на сервер" in TEMPLATE
    for quality in ("720", "480", "360"):
        assert f'data-quality="{quality}"' in TEMPLATE
    for attribute, value in (
        ("data-serv", "{{ serv }}"),
        ("data-anime-id", "{{ anime_id }}"),
        ("data-translation-id", "{{ translation_id }}"),
        ("data-first-episode", "{{ series[0] }}"),
        ("data-last-episode", "{{ series[1] }}"),
    ):
        assert f'{attribute}="{value}"' in TEMPLATE
    assert "{{ url_for('start_batch_download') }}" in TEMPLATE
    assert "/batch_download/status/__JOB_ID__/" in TEMPLATE
    assert "url_for('batch_download_status'" in TEMPLATE
    assert 'id="batch-progress"' in TEMPLATE
    assert 'id="batch-summary"' in TEMPLATE
    assert 'id="batch-current"' in TEMPLATE
    assert 'id="batch-destination"' in TEMPLATE
    assert 'id="batch-episodes"' in TEMPLATE
    assert "fast-720-{{ url }}/" in TEMPLATE
    assert "old-720-{{ url }}/" in TEMPLATE
    assert "watch-{{ url }}/" in TEMPLATE
    assert "js/batch_download.js" in TEMPLATE


def test_script_posts_contract_polls_persists_and_renders_safely():
    script = JS_PATH.read_text(encoding="utf-8")

    assert "fetch(panel.dataset.startUrl" in script
    assert 'method: "POST"' in script
    assert '"Content-Type": "application/json"' in script
    for field in (
        "serv",
        "anime_id",
        "translation_id",
        "quality",
        "first_episode",
        "last_episode",
    ):
        assert field in script
    assert "JSON.stringify(payload)" in script
    assert "anime_id: animeId" in script
    assert "translation_id: translationId" in script
    assert "quality: quality" in script
    assert "response.ok" in script
    assert "snapshot.job_id" in script
    assert "entry.state" in script
    assert "__JOB_ID__" in script
    assert "encodeURIComponent(jobId)" in script
    assert "setTimeout" in script
    assert "1000" in script
    assert "localStorage.setItem" in script
    assert "localStorage.getItem" in script
    assert "error.status >= 400 && error.status < 500" in script
    assert "setButtonsDisabled(true)" in script
    assert "setButtonsDisabled(false)" in script
    assert "showError" in script
    for key_part in ("serv", "animeId", "translationId", "quality"):
        assert key_part in script
    assert '"completed"' in script
    assert '"completed_with_errors"' in script
    for russian_status in (
        "В очереди",
        "Скачивается",
        "Завершено",
        "Пропущено",
        "Ошибка",
    ):
        assert russian_status in script
    assert ".textContent" in script
    assert "createElement" in script
    assert ".innerHTML" not in script
