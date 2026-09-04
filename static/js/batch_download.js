(() => {
  "use strict";

  const panel = document.getElementById("batch-download-panel");
  if (!panel) {
    return;
  }

  const buttons = Array.from(panel.querySelectorAll(".batch-download-button"));
  const statusPanel = document.getElementById("batch-status-panel");
  const progressBar = document.getElementById("batch-progress");
  const summary = document.getElementById("batch-summary");
  const current = document.getElementById("batch-current");
  const destination = document.getElementById("batch-destination");
  const episodesBody = document.getElementById("batch-episodes");

  const statusLabels = {
    queued: "В очереди",
    running: "Скачивается",
    downloading: "Скачивается",
    completed: "Завершено",
    skipped: "Пропущено",
    failed: "Ошибка",
  };
  const terminalStatuses = new Set(["completed", "completed_with_errors"]);

  const serv = panel.dataset.serv;
  const animeId = panel.dataset.animeId;
  const translationId = panel.dataset.translationId;

  function storageKey(quality) {
    return ["batch-download", serv, animeId, translationId, quality].join(":");
  }

  function setButtonsDisabled(disabled) {
    buttons.forEach((button) => {
      button.disabled = disabled;
    });
  }

  function showError(message) {
    statusPanel.classList.remove("d-none");
    summary.textContent = `Ошибка: ${message}`;
  }

  async function jsonResponse(response) {
    let body;
    try {
      body = await response.json();
    } catch (_error) {
      body = {};
    }
    if (!response.ok) {
      const error = new Error(body.error || body.message || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function episodeStatus(entry) {
    const state = entry.state || entry.status;
    const label = statusLabels[state] || state || "В очереди";
    const detail = entry.error || entry.message;
    return detail ? `${label}: ${detail}` : label;
  }

  function renderEpisodes(episodes) {
    episodesBody.replaceChildren();
    episodes.forEach((entry) => {
      const row = document.createElement("tr");
      const episodeCell = document.createElement("td");
      const statusCell = document.createElement("td");
      episodeCell.textContent = String(entry.episode ?? entry.number ?? "");
      statusCell.textContent = episodeStatus(entry);
      row.append(episodeCell, statusCell);
      episodesBody.appendChild(row);
    });
  }

  function renderSnapshot(snapshot) {
    statusPanel.classList.remove("d-none");

    const episodes = Array.isArray(snapshot.episodes) ? snapshot.episodes : [];
    const completedCount = Number(snapshot.completed_count ?? snapshot.completed ?? 0);
    const skippedCount = Number(snapshot.skipped_count ?? snapshot.skipped ?? 0);
    const failedCount = Number(snapshot.failed_count ?? snapshot.failed ?? 0);
    const total = Number(snapshot.total ?? episodes.length ?? 0);
    const processed = completedCount + skippedCount + failedCount;
    const suppliedProgress = Number(snapshot.progress);
    const percent = Number.isFinite(suppliedProgress)
      ? suppliedProgress
      : (total > 0 ? (processed / total) * 100 : 0);
    const boundedPercent = Math.max(0, Math.min(100, Math.round(percent)));

    progressBar.style.width = `${boundedPercent}%`;
    progressBar.setAttribute("aria-valuenow", String(boundedPercent));
    progressBar.textContent = `${boundedPercent}%`;

    const overallLabel = snapshot.status === "completed_with_errors"
      ? "Завершено с ошибками"
      : (statusLabels[snapshot.status] || snapshot.status || "В очереди");
    summary.textContent = `${overallLabel}. Готово: ${completedCount}, пропущено: ${skippedCount}, ошибок: ${failedCount}, всего: ${total}.`;
    current.textContent = snapshot.current_episode == null
      ? ""
      : `Текущая серия: ${snapshot.current_episode}`;
    destination.textContent = snapshot.destination
      ? `Папка назначения: ${snapshot.destination}`
      : "";
    renderEpisodes(episodes);
  }

  function statusUrl(jobId) {
    return panel.dataset.statusUrlTemplate.replace(
      "__JOB_ID__",
      encodeURIComponent(jobId),
    );
  }

  async function poll(jobId, quality) {
    try {
      const response = await fetch(statusUrl(jobId), {
        headers: { Accept: "application/json" },
      });
      const snapshot = await jsonResponse(response);
      renderSnapshot(snapshot);
      if (terminalStatuses.has(snapshot.status)) {
        localStorage.removeItem(storageKey(quality));
        setButtonsDisabled(false);
        return;
      }
    } catch (error) {
      showError(error.message || "Не удалось получить состояние загрузки");
      if (error.status >= 400 && error.status < 500) {
        localStorage.removeItem(storageKey(quality));
        setButtonsDisabled(false);
        return;
      }
    }
    window.setTimeout(() => poll(jobId, quality), 1000);
  }

  async function start(quality) {
    setButtonsDisabled(true);
    statusPanel.classList.remove("d-none");
    summary.textContent = "Запуск пакетной загрузки…";

    const payload = {
      serv,
      anime_id: animeId,
      translation_id: translationId,
      quality: quality,
      first_episode: Number(panel.dataset.firstEpisode),
      last_episode: Number(panel.dataset.lastEpisode),
    };

    try {
      const response = await fetch(panel.dataset.startUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify(payload),
      });
      const snapshot = await jsonResponse(response);
      if (!snapshot.job_id) {
        throw new Error("Сервер не вернул идентификатор задания");
      }
      renderSnapshot(snapshot);
      localStorage.setItem(storageKey(quality), String(snapshot.job_id));
      if (terminalStatuses.has(snapshot.status)) {
        localStorage.removeItem(storageKey(quality));
        setButtonsDisabled(false);
      } else {
        window.setTimeout(() => poll(snapshot.job_id, quality), 1000);
      }
    } catch (error) {
      showError(error.message || "Не удалось запустить загрузку");
      setButtonsDisabled(false);
    }
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => start(button.dataset.quality));
  });

  for (const button of buttons) {
    const quality = button.dataset.quality;
    const savedJobId = localStorage.getItem(storageKey(quality));
    if (savedJobId) {
      setButtonsDisabled(true);
      statusPanel.classList.remove("d-none");
      summary.textContent = "Возобновление отслеживания загрузки…";
      poll(savedJobId, quality);
      break;
    }
  }
})();
