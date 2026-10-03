// gnnote web UI - main thread.
//
// Engine selection on load: GET api/health -> {"ok": true} means a gnnote
// server is behind this page and POST api/convert is used; otherwise the
// Python converter runs in the browser through web/worker.js (Pyodide).
// Pyodide assets come from window.PYODIDE_BASE if set, else from a sibling
// pyodide/ directory when pyodide/pyodide.mjs answers a HEAD request
// (vendored build), else from the jsDelivr CDN.

import { t, applyLanguage, initialLang, rememberLang, currentLang, LANGS, LANG_NAMES } from "./i18n.js";
import { FORMATS } from "./formats.js";

const PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";
const STAT_ORDER = ["pages", "strokes", "images", "texts", "pdfs"];
// target format -> element holding the options that only apply to it
const TARGET_OPTIONS = { notability: "opts-notability", pdf: "opts-pdf" };
const MIME_TYPES = {
  notability: "application/x-notability-note",
  goodnotes: "application/x-goodnotes",
  pdf: "application/pdf",
};

const $ = (id) => document.getElementById(id);
const el = {
  lang: $("lang"),
  dropzone: $("dropzone"),
  file: $("file"),
  choose: $("choose"),
  fileInfo: $("file-info"),
  fileName: $("file-name"),
  fileDirection: $("file-direction"),
  target: $("target"),
  supported: $("supported"),
  fileError: $("file-error"),
  optsNone: $("opts-none"),
  pressure: $("pressure"),
  simplify: $("simplify"),
  convert: $("convert"),
  reset: $("reset"),
  progress: $("progress"),
  progressMessage: $("progress-message"),
  progressBytes: $("progress-bytes"),
  progressBar: $("progress-bar"),
  progressFill: $("progress-fill"),
  result: $("result"),
  resultName: $("result-name"),
  download: $("download"),
  hostedHint: $("hosted-hint"),
  share: $("share"),
  stats: $("stats").querySelector("tbody"),
  warnings: $("warnings"),
  warningsNone: $("warnings-none"),
  warningsLang: $("warnings-lang"),
  error: $("error"),
  errorMessage: $("error-message"),
  errorHelp: $("error-help"),
  errorDetails: $("error-details"),
  errorTraceback: $("error-traceback"),
  engineStatus: $("engine-status"),
  version: $("version"),
};

const state = {
  file: null,           // File chosen by the user
  engine: "unknown",    // "server" | "browser" | "failed"
  engineError: null,
  worker: null,
  workerReady: null,    // Promise resolved when the worker posted "ready"
  workerProgress: null, // last engine progress {loaded,total}
  busy: false,
  maxUpload: 0,         // server engine: largest accepted upload in bytes (from api/health)
  versionInfo: null,    // version.json of the build, when it exists
  last: null,           // last result {file, name, stats, warnings, ...}
  lastUrl: null,        // object URL of the last result
  seq: 0,
};

// ---------- helpers ----------

function formatBytes(n) {
  if (!isFinite(n) || n < 0) return "?";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " kB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

function show(node, visible) {
  node.hidden = !visible;
}

function setProgress(message, bytes) {
  show(el.progress, true);
  el.progressMessage.textContent = message;
  if (bytes && bytes.loaded > 0) {
    const text = bytes.total > 0
      ? t("progress.engine.bytes", { loaded: formatBytes(bytes.loaded), total: formatBytes(bytes.total) })
      : t("progress.engine.bytes.unknown", { loaded: formatBytes(bytes.loaded) });
    el.progressBytes.textContent = text;
    show(el.progressBytes, true);
    if (bytes.total > 0) {
      show(el.progressBar, true);
      el.progressFill.style.width = Math.min(100, (100 * bytes.loaded) / bytes.total).toFixed(1) + "%";
    } else {
      show(el.progressBar, false);
    }
  } else {
    show(el.progressBytes, false);
    show(el.progressBar, false);
  }
}

function hideProgress() {
  show(el.progress, false);
}

function showError(message, traceback, help) {
  show(el.error, true);
  el.errorMessage.textContent = message;
  el.errorHelp.textContent = help || "";
  show(el.errorHelp, !!help);
  if (traceback) {
    el.errorTraceback.textContent = traceback;
    show(el.errorDetails, true);
  } else {
    show(el.errorDetails, false);
  }
  el.error.scrollIntoView({ block: "nearest" });
}

function hideError() {
  show(el.error, false);
}

function setEngineStatus() {
  const key = { server: "engine.server", browser: "engine.browser", failed: "engine.failed" }[state.engine] || "engine.unknown";
  el.engineStatus.setAttribute("data-i18n", key);
  el.engineStatus.textContent = t(key);
}

function formatById(id) {
  return FORMATS.find((f) => f.id === id) || null;
}

function readableExtensions() {
  return FORMATS.filter((f) => f.readable).flatMap((f) => f.inputExtensions);
}

function sourceFormatOf(name) {
  const lower = String(name || "").toLowerCase();
  const fmt = FORMATS.find((f) => f.readable && f.inputExtensions.some((ext) => lower.endsWith(ext)));
  return fmt ? fmt.id : null;
}

/** Fill the "Convert to" list for a source format, keeping the previous choice when possible. */
function populateTargets(source) {
  const previous = el.target.value;
  el.target.textContent = "";
  for (const f of FORMATS) {
    if (!f.writable || f.id === source) continue;
    const opt = document.createElement("option");
    opt.value = f.id;
    opt.textContent = f.name;
    el.target.appendChild(opt);
  }
  const ids = Array.from(el.target.options, (o) => o.value);
  const src = formatById(source);
  const wanted = [previous, src && src.defaultTarget, "notability"].find((id) => id && ids.includes(id));
  if (wanted) el.target.value = wanted;
}

function updateDirection() {
  const src = formatById(state.file ? sourceFormatOf(state.file.name) : null);
  const dst = formatById(el.target.value);
  el.fileDirection.textContent = src && dst ? t("file.direction", { from: src.name, to: dst.name }) : "";
  const group = dst ? TARGET_OPTIONS[dst.id] : null;
  for (const id of Object.values(TARGET_OPTIONS)) show($(id), id === group);
  show(el.optsNone, !!dst && !group);
}

function readOptions() {
  const paper = (document.querySelector('input[name="paper"]:checked') || {}).value || "plain";
  let simplify = parseFloat(el.simplify.value);
  if (!isFinite(simplify) || simplify < 0) simplify = 0;
  const options = { paper: paper, pressure: !!el.pressure.checked, simplify: simplify };
  if (el.target.value) options.target = el.target.value;
  if (options.target === "pdf") {
    options.pdf_ink = (document.querySelector('input[name="pdf_ink"]:checked') || {}).value || "flatten";
  }
  return options;
}

function updateConvertButton() {
  el.convert.disabled = !state.file || state.busy || state.engine === "failed";
  // While a conversion runs the selected file must not change: its result would be shown
  // under another file's name (or with no file selected at all).
  el.reset.disabled = state.busy;
  el.choose.disabled = state.busy;
  el.file.disabled = state.busy;
  el.dropzone.classList.toggle("busy", state.busy);
  el.dropzone.setAttribute("aria-disabled", state.busy ? "true" : "false");
}

// ---------- language ----------

function setLanguage(lang) {
  applyLanguage(lang);
  rememberLang(lang);
  setEngineStatus();
  renderSupported();
  if (state.file) describeFile(state.file);
  if (state.last) renderResult(state.last);
}

for (const code of LANGS) {
  const opt = document.createElement("option");
  opt.value = code;
  opt.lang = code;
  opt.textContent = LANG_NAMES[code] || code;
  el.lang.appendChild(opt);
}

el.lang.addEventListener("change", () => {
  setLanguage(el.lang.value);
});

// ---------- file selection ----------

function describeFile(file) {
  el.fileName.textContent = t("file.selected", { name: file.name, size: formatBytes(file.size) });
  populateTargets(sourceFormatOf(file.name));
  updateDirection();
  show(el.fileInfo, true);
}

el.target.addEventListener("change", updateDirection);

function renderSupported() {
  const names = FORMATS.filter((f) => f.readable).map((f) => f.name);
  el.supported.textContent = t("app.supported", { list: names.join(", ") });
}

function acceptFile(file) {
  if (state.busy) return;
  hideError();
  show(el.result, false);
  state.last = null;
  if (!file) return;
  if (!sourceFormatOf(file.name)) {
    state.file = null;
    show(el.fileInfo, false);
    el.fileError.textContent = t("file.badext", { list: readableExtensions().join(", ") });
    show(el.fileError, true);
    updateConvertButton();
    return;
  }
  show(el.fileError, false);
  state.file = file;
  describeFile(file);
  el.convert.textContent = t("convert");
  show(el.reset, true);
  updateConvertButton();
}

el.file.addEventListener("change", () => acceptFile(el.file.files && el.file.files[0]));

function openPicker(ev) {
  if (ev) ev.preventDefault();
  if (state.busy) return;
  el.file.click();
}
el.choose.addEventListener("click", (ev) => {
  ev.stopPropagation();
  openPicker(ev);
});
el.dropzone.addEventListener("click", (ev) => {
  if (ev.target === el.file) return;
  openPicker(ev);
});
el.dropzone.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" || ev.key === " ") openPicker(ev);
});

["dragenter", "dragover"].forEach((type) => {
  el.dropzone.addEventListener(type, (ev) => {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
    el.dropzone.classList.add("dragover");
  });
});
["dragleave", "dragend"].forEach((type) => {
  el.dropzone.addEventListener(type, () => el.dropzone.classList.remove("dragover"));
});
el.dropzone.addEventListener("drop", (ev) => {
  ev.preventDefault();
  el.dropzone.classList.remove("dragover");
  const files = ev.dataTransfer && ev.dataTransfer.files;
  if (files && files.length) acceptFile(files[0]);
});
// Dropping anywhere else on the page must not navigate away from the app.
window.addEventListener("dragover", (ev) => ev.preventDefault());
window.addEventListener("drop", (ev) => ev.preventDefault());

el.reset.addEventListener("click", () => {
  if (state.busy) return;
  state.file = null;
  state.last = null;
  el.file.value = "";
  show(el.fileInfo, false);
  show(el.fileError, false);
  show(el.result, false);
  show(el.reset, false);
  hideError();
  el.convert.textContent = t("convert");
  populateTargets(null);
  updateDirection();
  updateConvertButton();
});

// ---------- engine detection ----------

function pyodideCdn() {
  const info = state.versionInfo;
  if (info && info.pyodide && typeof info.pyodide.cdn === "string" && info.pyodide.cdn) {
    return info.pyodide.cdn;
  }
  return PYODIDE_CDN;
}

async function detectPyodideBase() {
  if (typeof window.PYODIDE_BASE === "string" && window.PYODIDE_BASE) {
    return new URL(window.PYODIDE_BASE, location.href).href;
  }
  const local = new URL("pyodide/", location.href).href;
  const info = state.versionInfo || (await loadVersion());
  if (info && info.pyodide && info.pyodide.vendored === true) {
    return local; // scripts/build_web.py --vendor-pyodide wrote the copy next to the page
  }
  try {
    const probe = new URL("pyodide/pyodide.mjs", location.href).href;
    const r = await fetch(probe, { method: "HEAD", cache: "no-cache" });
    // A static host with a SPA fallback answers 200 + index.html for every unknown path;
    // only a non-HTML answer counts as a vendored copy.
    const ctype = (r.headers.get("content-type") || "").toLowerCase();
    if (r.ok && !ctype.startsWith("text/html")) return local;
  } catch (e) {
    /* no vendored copy */
  }
  return pyodideCdn();
}

function startWorker() {
  if (state.worker) return state.workerReady;
  if (typeof Worker === "undefined") {
    return Promise.reject(new Error(t("error.noworker")));
  }
  const worker = new Worker(new URL("./worker.js", import.meta.url), { type: "module" });
  state.worker = worker;
  state.workerReady = new Promise((resolve, reject) => {
    const pending = new Map(); // id -> {resolve, reject}
    worker.__pending = pending;
    worker.onmessage = (event) => {
      const msg = event.data || {};
      if (msg.type === "progress") {
        onWorkerProgress(msg);
      } else if (msg.type === "ready") {
        state.workerProgress = null;
        resolve(msg);
      } else if (msg.type === "done") {
        const p = pending.get(msg.id);
        if (p) {
          pending.delete(msg.id);
          p.resolve(msg);
        }
      } else if (msg.type === "error") {
        if (msg.stage === "init") {
          reject(new Error(msg.message + (msg.traceback ? "\n" + msg.traceback : "")));
          return;
        }
        const p = pending.get(msg.id);
        if (p) {
          pending.delete(msg.id);
          p.reject(Object.assign(new Error(msg.message), { traceback: msg.traceback }));
        }
      }
    };
    worker.onerror = (ev) => {
      const err = new Error(ev.message || "worker error");
      reject(err);
      for (const p of pending.values()) p.reject(err);
      pending.clear();
    };
  });
  detectPyodideBase().then((base) => {
    const packageUrl = new URL("gnnote.zip", location.href).href;
    // pyodideCdn is the fallback the worker retries with when the vendored copy fails to load
    worker.postMessage({ type: "init", pyodideBase: base, pyodideCdn: pyodideCdn(), packageUrl: packageUrl });
  });
  return state.workerReady;
}

function onWorkerProgress(msg) {
  if (!state.busy && state.workerReady) {
    // engine warming up in the background: only show once the user converts
    state.workerProgress = msg;
    return;
  }
  renderEngineProgress(msg);
}

function renderEngineProgress(msg) {
  if (msg.stage === "engine") {
    setProgress(t("progress.engine.loading"), { loaded: msg.loaded || 0, total: msg.total || 0 });
  } else if (msg.stage === "package") {
    setProgress(t("progress.engine.package"), { loaded: msg.loaded || 0, total: msg.total || 0 });
  } else if (msg.stage === "converting") {
    setProgress(t("progress.converting", { name: msg.name || "" }));
  }
}

async function detectEngine() {
  setEngineStatus();
  try {
    const r = await fetch(new URL("api/health", location.href).href, { cache: "no-store" });
    if (r.ok) {
      const j = await r.json().catch(() => null);
      if (j && j.ok === true) {
        state.engine = "server";
        state.maxUpload = typeof j.maxUpload === "number" && j.maxUpload > 0 ? j.maxUpload : 0;
        setEngineStatus();
        updateConvertButton();
        return;
      }
    }
  } catch (e) {
    /* no server */
  }
  state.engine = "browser";
  setEngineStatus();
  updateConvertButton();
  // Warm the engine up in the background; errors surface when converting.
  startWorker().catch((e) => {
    state.engineError = e;
  });
}

// ---------- conversion ----------

async function convertViaServer(file, options) {
  if (state.maxUpload && file.size > state.maxUpload) {
    // The server answers 413 and closes the socket before the body is read, which the
    // browser reports as a network error: refuse here with a readable message instead.
    throw new Error(t("error.toolarge", { size: formatBytes(file.size), limit: formatBytes(state.maxUpload) }));
  }
  setProgress(t("progress.uploading"));
  const q = new URLSearchParams({
    paper: options.paper,
    pressure: options.pressure ? "true" : "false",
    simplify: String(options.simplify),
  });
  if (options.target) q.set("to", options.target);
  if (options.pdf_ink) q.set("pdf_ink", options.pdf_ink);
  const form = new FormData();
  form.append("file", file, file.name);
  const r = await fetch(new URL("api/convert?" + q.toString(), location.href).href, {
    method: "POST",
    body: form,
  });
  if (!r.ok) {
    let text = "";
    try {
      text = await r.text();
    } catch (e) {
      /* ignore */
    }
    throw Object.assign(new Error(t("error.server", { status: r.status })), { traceback: text });
  }
  setProgress(t("progress.converting", { name: file.name }));
  const blob = await r.blob();
  let warnings = [];
  try {
    warnings = JSON.parse(r.headers.get("X-GnNote-Warnings") || "[]");
  } catch (e) {
    warnings = [];
  }
  let stats = {};
  try {
    stats = JSON.parse(r.headers.get("X-GnNote-Stats") || "{}");
  } catch (e) {
    stats = {};
  }
  let name = null;
  const cd = r.headers.get("Content-Disposition") || "";
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  const plain = /filename="?([^";]+)"?/i.exec(cd);
  if (star) name = decodeURIComponent(star[1]);
  else if (plain) name = plain[1];
  const hinted = r.headers.get("X-GnNote-Filename");
  if (!name && hinted) {
    try {
      name = decodeURIComponent(hinted);
    } catch (e) {
      name = hinted;
    }
  }
  const src = sourceFormatOf(file.name);
  const srcFmt = formatById(src);
  const target = options.target || (srcFmt && srcFmt.defaultTarget) || "notability";
  if (!name) name = swapExtension(file.name, target);
  return {
    name: name,
    blob: blob,
    warnings: Array.isArray(warnings) ? warnings : [],
    stats: stats && typeof stats === "object" ? stats : {},
    sourceFormat: src,
    targetFormat: target,
  };
}

function swapExtension(name, target) {
  const dst = formatById(target);
  const src = formatById(sourceFormatOf(name));
  let stem = name;
  if (src) {
    const ext = src.inputExtensions.find((e) => name.toLowerCase().endsWith(e));
    if (ext) stem = name.slice(0, name.length - ext.length);
  }
  return stem + (dst ? dst.extension : "");
}

async function convertViaWorker(file, options) {
  if (state.workerProgress) renderEngineProgress(state.workerProgress);
  else setProgress(t("progress.engine.loading"));
  try {
    await startWorker();
  } catch (e) {
    // drop the broken worker so the next click starts a fresh one
    try {
      state.worker.terminate();
    } catch (e2) {
      /* ignore */
    }
    state.worker = null;
    state.workerReady = null;
    state.workerProgress = null;
    throw Object.assign(new Error(t("error.engine", { error: e.message.split("\n")[0] })), {
      traceback: e.message,
      help: t("error.engine.help"),
    });
  }
  setProgress(t("progress.reading"));
  const buffer = await file.arrayBuffer();
  setProgress(t("progress.converting", { name: file.name }));
  const id = ++state.seq;
  const worker = state.worker;
  const done = new Promise((resolve, reject) => {
    worker.__pending.set(id, { resolve: resolve, reject: reject });
  });
  worker.postMessage({ type: "convert", id: id, name: file.name, buffer: buffer, options: options }, [buffer]);
  const msg = await done;
  const mime = MIME_TYPES[msg.targetFormat] || "application/octet-stream";
  return {
    name: msg.name,
    blob: new Blob([msg.buffer], { type: mime }),
    warnings: msg.warnings || [],
    stats: msg.stats || {},
    sourceFormat: msg.sourceFormat,
    targetFormat: msg.targetFormat,
  };
}

function renderResult(res) {
  show(el.result, true);
  el.resultName.textContent = res.name;
  el.download.href = state.lastUrl;
  el.download.download = res.name;

  // stats table
  el.stats.textContent = "";
  const keys = STAT_ORDER.filter((k) => k in res.stats).concat(
    Object.keys(res.stats).filter((k) => !STAT_ORDER.includes(k)).sort()
  );
  for (const k of keys) {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    const td2 = document.createElement("td");
    td1.textContent = t("stats." + k) === "stats." + k ? k : t("stats." + k);
    td2.textContent = String(res.stats[k]);
    tr.append(td1, td2);
    el.stats.appendChild(tr);
  }
  const trSize = document.createElement("tr");
  const tdA = document.createElement("td");
  const tdB = document.createElement("td");
  tdA.textContent = t("stats.size");
  tdB.textContent = formatBytes(res.blob.size);
  trSize.append(tdA, tdB);
  el.stats.appendChild(trSize);

  // warnings
  el.warnings.textContent = "";
  for (const w of res.warnings) {
    const li = document.createElement("li");
    li.textContent = String(w);
    el.warnings.appendChild(li);
  }
  show(el.warningsNone, res.warnings.length === 0);
  show(el.warningsLang, res.warnings.length > 0 && currentLang() !== "en");

  // share button only when the platform can share files
  let canShare = false;
  try {
    canShare = !!(navigator.canShare && navigator.share && navigator.canShare({ files: [res.file] }));
  } catch (e) {
    canShare = false;
  }
  show(el.share, canShare);
  renderHostedHint(res);
  el.convert.textContent = t("convert.again");
}

el.share.addEventListener("click", async () => {
  if (!state.last) return;
  try {
    await navigator.share({ files: [state.last.file], title: state.last.name });
  } catch (e) {
    if (e && e.name === "AbortError") return; // user dismissed the sheet
    showError(t("result.share.failed", { error: e && e.message ? e.message : String(e) }));
  }
});

// Hosted viewers (the page published as a claude.ai artifact) block ordinary downloads and
// offer a "downloads" capability instead; its file names must carry an allow-listed extension,
// so the output is saved with a ".zip" suffix and the hint tells the user to rename it.
// Outside such a host window.claude is absent and the plain download link is used.
const HOSTED_OK_EXTENSIONS = ["zip", "pdf", "png", "jpg", "jpeg", "json", "txt", "md", "html", "svg", "csv"];

function hostedFileName(name) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  return HOSTED_OK_EXTENSIONS.includes(ext) ? name : name + ".zip";
}

const hostedDownloads = (window.claude && typeof window.claude.use === "function")
  ? Promise.resolve().then(() => window.claude.use("downloads")).catch(() => null)
  : Promise.resolve(null);

hostedDownloads.then((dl) => {
  state.downloads = dl || null;
  if (state.downloads && state.last) renderHostedHint(state.last);
});

function renderHostedHint(res) {
  const hosted = !!state.downloads;
  const saved = hostedFileName(res.name);
  // a PDF keeps its name, so there is nothing to rename
  show(el.hostedHint, hosted && saved !== res.name);
  if (hosted) {
    el.hostedHint.textContent = t("result.hosted.hint", { name: res.name, saved: saved });
    show(el.share, false);
  }
}

el.download.addEventListener("click", async (event) => {
  if (!state.downloads || !state.last) return; // plain link: the browser downloads the Blob URL
  event.preventDefault();
  try {
    await state.downloads.save({ filename: hostedFileName(state.last.name), data: state.last.blob });
  } catch (e) {
    const code = e && e.code ? String(e.code) : "";
    if (code === "declined" || code === "rate_limited") return;
    showError(t("result.hosted.failed", { error: e && e.message ? e.message : String(e) }));
  }
});

async function runConversion() {
  if (!state.file || state.busy) return;
  state.busy = true;
  hideError();
  show(el.result, false);
  updateConvertButton();
  const file = state.file;
  const options = readOptions();
  try {
    const res = state.engine === "server"
      ? await convertViaServer(file, options)
      : await convertViaWorker(file, options);
    if (state.file !== file) return; // the file was reset or replaced meanwhile
    res.file = new File([res.blob], res.name, { type: res.blob.type });
    if (state.lastUrl) URL.revokeObjectURL(state.lastUrl);
    state.lastUrl = URL.createObjectURL(res.file);
    state.last = res;
    hideProgress();
    renderResult(res);
    window.__gnnoteLastResult = {
      name: res.name,
      size: res.blob.size,
      stats: res.stats,
      warnings: res.warnings,
      sourceFormat: res.sourceFormat,
      targetFormat: res.targetFormat,
      engine: state.engine,
    };
    el.result.scrollIntoView({ block: "nearest" });
  } catch (e) {
    hideProgress();
    window.__gnnoteLastResult = { error: e && e.message ? e.message : String(e) };
    showError(e && e.message ? e.message : String(e), e && e.traceback ? e.traceback : "", e && e.help ? e.help : "");
  } finally {
    state.busy = false;
    updateConvertButton();
  }
}

el.convert.addEventListener("click", runConversion);

// ---------- boot ----------

let versionPromise = null;

function loadVersion() {
  if (!versionPromise) {
    versionPromise = (async () => {
      try {
        const r = await fetch(new URL("version.json", location.href).href, { cache: "no-cache" });
        if (r.ok) {
          const j = await r.json();
          if (j && typeof j === "object") state.versionInfo = j;
          if (j && j.version) el.version.textContent = t("footer.version", { version: j.version });
        }
      } catch (e) {
        /* development checkout without dist/ */
      }
      return state.versionInfo;
    })();
  }
  return versionPromise;
}

applyLanguage(initialLang());
el.lang.value = currentLang();
setEngineStatus();
renderSupported();
populateTargets(null);
updateDirection();
updateConvertButton();
loadVersion();
detectEngine();
