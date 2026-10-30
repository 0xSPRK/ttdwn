/* ttdwn Web UI - Frontend Application */

// ============================================================================
// Internationalization (all strings in one place for easy translation)
// ============================================================================
const I18N = {
  en: {
    paste: "Paste",
    clear: "Clear",
    download: "Download",
    refresh: "Refresh",
    downloading: "Downloading...",
    preparing: "Preparing...",
    complete: "Complete",
    error: "Error",
    noDownloads: "No downloads yet",
    startHint: "Paste a TikTok URL above to start downloading",
    urlHint: "Supports tiktok.com, vm.tiktok.com, vt.tiktok.com, m.tiktok.com links",
    qualityBest: "Best quality (default)",
    qualityWorst: "Smallest file",
    allowWatermark: "Allow watermark",
    downloadSuccess: "Download complete: {name}",
    downloadFailed: "Download failed: {error}",
    fileDeleted: "File deleted",
    deleteFailed: "Failed to delete file",
    copySuccess: "Copied to clipboard",
    copyFailed: "Failed to copy",
    invalidUrl: "Please enter a valid TikTok URL",
    networkError: "Network error. Please check your connection.",
    authRequired: "Authentication required",
  },
};

// Current locale (can be extended)
let currentLocale = "en";
function t(key, params = {}) {
  let str = I18N[currentLocale][key] || key;
  Object.entries(params).forEach(([k, v]) => {
    str = str.replace(`{${k}}`, v);
  });
  return str;
}

// ============================================================================
// Utility Functions
// ============================================================================
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function formatDate(timestamp) {
  const date = new Date(timestamp);
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function showSnackbar(message, type = "info") {
  const snackbar = $("#snackbar");
  const text = $("#snackbar-text");
  snackbar.className = "snackbar";
  if (type === "error") snackbar.classList.add("snackbar--error");
  if (type === "success") snackbar.classList.add("snackbar--success");
  text.textContent = message;
  snackbar.hidden = false;
  setTimeout(() => hideSnackbar(), 5000);
}

function hideSnackbar() {
  const snackbar = $("#snackbar");
  snackbar.hidden = true;
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("theme", theme);
}

function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) {
    setTheme(saved);
  } else if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
    setTheme("dark");
  } else {
    setTheme("light");
  }
}

// ============================================================================
// API Client
// ============================================================================
const API_BASE = "/api";

async function apiRequest(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
    ...options,
  });

  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }

  if (res.status === 204) return null;
  return res.json();
}

async function createDownload(url, options = {}) {
  return apiRequest("/download", {
    method: "POST",
    body: JSON.stringify({ url, ...options }),
  });
}

async function getJobs() {
  return apiRequest("/jobs");
}

async function getJob(id) {
  return apiRequest(`/jobs/${id}`);
}

async function getFiles() {
  return apiRequest("/files");
}

async function downloadFile(name) {
  const res = await fetch(`${API_BASE}/files/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error("Failed to download file");
  return res.blob();
}

async function deleteFile(name) {
  await apiRequest(`/files/${encodeURIComponent(name)}`, { method: "DELETE" });
}

// ============================================================================
// State Management
// ============================================================================
let pollingInterval = null;
const activeJobs = new Set();

function startPolling() {
  if (pollingInterval) return;
  pollingInterval = setInterval(pollJobs, 1500);
}

function stopPolling() {
  if (pollingInterval) {
    clearInterval(pollingInterval);
    pollingInterval = null;
  }
}

async function pollJobs() {
  if (activeJobs.size === 0) {
    stopPolling();
    return;
  }

  try {
    const jobs = await getJobs();
    for (const job of jobs) {
      if (activeJobs.has(job.job_id)) {
        updateJobUI(job);
        if (job.status === "done" || job.status === "error") {
          activeJobs.delete(job.job_id);
          if (job.status === "done") {
            showSnackbar(t("downloadSuccess", { name: job.result?.saved?.split("/").pop() || "file" }), "success");
            loadFiles();
          } else {
            showSnackbar(t("downloadFailed", { error: job.error || "Unknown error" }), "error");
          }
        }
      }
    }
  } catch (err) {
    console.error("Polling error:", err);
  }
}

// ============================================================================
// UI Rendering
// ============================================================================
function renderFileCard(file) {
  const card = document.createElement("div");
  card.className = "file-card";
  card.dataset.name = file.name;

  const isVideo = /\.(mp4|mkv|webm|mov)$/i.test(file.name);
  const isAudio = /\.(mp3|m4a|opus|wav)$/i.test(file.name);
  const isImage = /\.(jpg|jpeg|png|webp|gif)$/i.test(file.name);
  const isDir = file.is_dir;

  let iconSvg = "";
  if (isDir) {
    iconSvg = `<svg viewBox="0 0 24 24" width="24" height="24"><path d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z"/></svg>`;
  } else if (isVideo) {
    iconSvg = `<svg viewBox="0 0 24 24" width="24" height="24"><path d="M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm10 10.5V7c0-.55-.45-1-1-1H5c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h8c.55 0 1-.45 1-1v-2.5l5 2.5v-5l-5-2.5z"/></svg>`;
  } else if (isAudio) {
    iconSvg = `<svg viewBox="0 0 24 24" width="24" height="24"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7c2.21 0 4-1.79 4-4S14.21 3 12 3zm0 1.5c1.38 0 2.5 1.12 2.5 2.5S13.38 8 12 8 9.5 6.88 9.5 5.5 10.62 3 12 3z"/></svg>`;
  } else if (isImage) {
    iconSvg = `<svg viewBox="0 0 24 24" width="24" height="24"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>`;
  } else {
    iconSvg = `<svg viewBox="0 0 24 24" width="24" height="24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm2 16H6V4h7v5h5v11z"/></svg>`;
  }

  const metaParts = [];
  if (!isDir) metaParts.push(formatBytes(file.size));
  metaParts.push(formatDate(file.modified));

  card.innerHTML = `
    <div class="file-card__icon" aria-hidden="true">${iconSvg}</div>
    <div class="file-card__info">
      <div class="file-card__name" title="${file.name}">${file.name}</div>
      <div class="file-card__meta">${metaParts.join(" · ")}</div>
    </div>
    <div class="file-card__actions">
      ${!isDir ? `
        <button class="file-card__action file-card__action--download" aria-label="Download ${file.name}" title="Download">
          <svg viewBox="0 0 24 24" width="18" height="18"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
        </button>
        <button class="file-card__action file-card__action--danger" aria-label="Delete ${file.name}" title="Delete">
          <svg viewBox="0 0 24 24" width="18" height="18"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
        </button>
      ` : ""}
    </div>
  `;

  // Download action
  const downloadBtn = card.querySelector(".file-card__action--download");
  if (downloadBtn) {
    downloadBtn.addEventListener("click", async () => {
      try {
        const blob = await downloadFile(file.name);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = file.name;
        a.click();
        URL.revokeObjectURL(url);
        showSnackbar(t("copySuccess"));
      } catch (err) {
        showSnackbar(t("downloadFailed", { error: err.message }), "error");
      }
    });
  }

  // Delete action
  const deleteBtn = card.querySelector(".file-card__action--danger");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      if (!confirm(`Delete "${file.name}"?`)) return;
      try {
        await deleteFile(file.name);
        card.remove();
        showSnackbar(t("fileDeleted"), "success");
        checkEmptyState();
      } catch (err) {
        showSnackbar(t("deleteFailed"), "error");
      }
    });
  }

  return card;
}

function checkEmptyState() {
  const list = $("#files-list");
  const emptyState = list.querySelector(".empty-state");
  const cards = list.querySelectorAll(".file-card");
  if (cards.length === 0) {
    emptyState.hidden = false;
  } else {
    emptyState.hidden = true;
  }
}

async function loadFiles() {
  const list = $("#files-list");
  try {
    const files = await getFiles();
    list.innerHTML = "";
    const emptyState = document.createElement("div");
    emptyState.className = "empty-state";
    emptyState.hidden = true;
    emptyState.innerHTML = `
      <svg viewBox="0 0 24 24" width="64" height="64"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
      <p>${t("noDownloads")}</p>
      <p class="hint">${t("startHint")}</p>
    `;
    list.appendChild(emptyState);

    for (const file of files) {
      list.appendChild(renderFileCard(file));
    }
    checkEmptyState();
  } catch (err) {
    console.error("Failed to load files:", err);
  }
}

function updateJobUI(job) {
  const progressContainer = $("#progress-container");
  const progressBar = $(".linear-progress__bar");
  const progressText = $("#progress-text");
  const downloadBtn = $("#download-btn");

  if (job.status === "queued" || job.status === "running") {
    progressContainer.hidden = false;
    downloadBtn.disabled = true;
    downloadBtn.querySelector("span").textContent = t("downloading");
    progressBar.style.width = `${job.progress}%`;
    progressText.textContent = `${job.message} (${job.progress}%)`;
  } else {
    progressContainer.hidden = true;
    downloadBtn.disabled = false;
    downloadBtn.querySelector("span").textContent = t("download");
  }
}

// ============================================================================
// Event Handlers
// ============================================================================
async function handleDownload() {
  const urlInput = $("#url-input");
  const url = urlInput.value.trim();

  if (!url) {
    showSnackbar(t("invalidUrl"), "error");
    urlInput.focus();
    return;
  }

  // Basic URL validation
  try {
    const parsed = new URL(url);
    const allowedHosts = [
      "tiktok.com", "www.tiktok.com", "m.tiktok.com", "t.tiktok.com",
      "vt.tiktok.com", "vm.tiktok.com", "v.tiktok.com",
      "tiktokv.com", "www.tiktokv.com", "m.tiktokv.com",
      "tiktok.com.tr",
    ];
    const host = parsed.hostname.replace(/^www\./, "");
    if (!allowedHosts.includes(host)) {
      showSnackbar(t("invalidUrl"), "error");
      return;
    }
  } catch {
    showSnackbar(t("invalidUrl"), "error");
    return;
  }

  const options = {
    quality: $("#quality-select").value,
    watermark: $("#watermark-check").checked,
  };

  try {
    const job = await createDownload(url, options);
    activeJobs.add(job.job_id);
    startPolling();
    updateJobUI(job);
    urlInput.value = "";
    $("#clear-btn").hidden = true;
  } catch (err) {
    showSnackbar(t("downloadFailed", { error: err.message }), "error");
  }
}

async function handlePaste() {
  try {
    const text = await navigator.clipboard.readText();
    const urlInput = $("#url-input");
    urlInput.value = text;
    urlInput.focus();
    $("#clear-btn").hidden = false;
    validateUrl();
  } catch (err) {
    showSnackbar(t("copyFailed"), "error");
  }
}

function handleClear() {
  const urlInput = $("#url-input");
  urlInput.value = "";
  urlInput.focus();
  $("#clear-btn").hidden = true;
  validateUrl();
}

function validateUrl() {
  const urlInput = $("#url-input");
  const downloadBtn = $("#download-btn");
  const url = urlInput.value.trim();

  if (url) {
    try {
      const parsed = new URL(url);
      const allowedHosts = [
        "tiktok.com", "www.tiktok.com", "m.tiktok.com", "t.tiktok.com",
        "vt.tiktok.com", "vm.tiktok.com", "v.tiktok.com",
        "tiktokv.com", "www.tiktokv.com", "m.tiktokv.com",
        "tiktok.com.tr",
      ];
      const host = parsed.hostname.replace(/^www\./, "");
      downloadBtn.disabled = !allowedHosts.includes(host);
    } catch {
      downloadBtn.disabled = true;
    }
    $("#clear-btn").hidden = false;
  } else {
    downloadBtn.disabled = true;
    $("#clear-btn").hidden = true;
  }
}

function handleThemeToggle() {
  const current = document.documentElement.getAttribute("data-theme");
  setTheme(current === "dark" ? "light" : "dark");
}

async function handleRefreshFiles() {
  await loadFiles();
}

// ============================================================================
// Initialization
// ============================================================================
function init() {
  initTheme();

  // Theme toggle
  $("#theme-toggle").addEventListener("click", handleThemeToggle);

  // Download form
  $("#download-btn").addEventListener("click", handleDownload);
  $("#paste-btn").addEventListener("click", handlePaste);
  $("#clear-btn").addEventListener("click", handleClear);
  $("#url-input").addEventListener("input", validateUrl);
  $("#url-input").addEventListener("paste", () => {
    setTimeout(() => {
      validateUrl();
      $("#clear-btn").hidden = false;
    }, 0);
  });

  // Files
  $("#refresh-files").addEventListener("click", handleRefreshFiles);

  // Snackbar dismiss
  $("#snackbar-dismiss").addEventListener("click", hideSnackbar);

  // Keyboard support for Enter in URL input
  $("#url-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !$("#download-btn").disabled) {
      handleDownload();
    }
  });

  // Initial load
  loadFiles();

  // Handle system theme change
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (!localStorage.getItem("theme")) {
      setTheme(e.matches ? "dark" : "light");
    }
  });
}

// Start when DOM is ready
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}