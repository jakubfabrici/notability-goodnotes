// gnnote web UI - module worker that runs the Python converter under Pyodide.
//
// Protocol (see docs/design.md section 5):
//   main -> worker  {type:"init", pyodideBase, packageUrl}
//   worker -> main  {type:"progress", stage, message, loaded?, total?}*  then
//                   {type:"ready", python, version} | {type:"error", message, traceback}
//   main -> worker  {type:"convert", id, name, buffer, options}        (buffer transferred)
//   worker -> main  {type:"progress", stage:"converting", ...}*  then
//                   {type:"done", id, name, buffer, warnings, stats, sourceFormat, targetFormat}
//                   (buffer transferred)  |  {type:"error", id, message, traceback}
//
// Pyodide needs a module worker because pyodide.asm.mjs is an ES module; it is
// loaded with a dynamic import so the base URL can be chosen at runtime (CDN,
// window.PYODIDE_BASE or a vendored ./pyodide/ directory).  The package arrives
// as gnnote.zip, is unpacked with pyodide.unpackArchive into PACKAGE_DIR on the
// Emscripten FS and that directory is put on sys.path.  Input bytes are handed
// to Python as a JS Uint8Array (JsProxy.to_bytes copies them into Python
// memory), the result bytes are copied out through PyProxy.getBuffer and every
// Python-side object is released afterwards so repeated conversions do not grow
// the WASM heap.  The worker stays alive between conversions.

const PACKAGE_DIR = "/gnnote-pkg";

let pyodide = null;
let convertFn = null;
let releaseFn = null;
let versionFn = null;
let initPromise = null;

function post(msg, transfer) {
  self.postMessage(msg, transfer || []);
}

function progress(stage, message, extra) {
  post(Object.assign({ type: "progress", stage: stage, message: message }, extra || {}));
}

function errorInfo(e) {
  // Pyodide's PythonError carries the full Python traceback in .message.
  const message = (e && e.message) ? String(e.message) : String(e);
  let traceback = e && e.stack ? String(e.stack) : "";
  if (e && e.constructor && e.constructor.name === "PythonError") {
    traceback = message;
  }
  const firstLine = message.split("\n").filter((l) => l.trim()).pop() || message;
  return { message: firstLine, traceback: traceback || message };
}

// Wrap fetch inside the worker during engine load so the ~13 MB of Pyodide
// assets report byte progress without being downloaded twice.
function installFetchCounter() {
  const originalFetch = self.fetch.bind(self);
  const counted = { loaded: 0, total: 0 };
  let lastReport = 0;

  function report(force) {
    const now = Date.now();
    if (!force && now - lastReport < 120) return;
    lastReport = now;
    progress("engine", "loading", { loaded: counted.loaded, total: counted.total });
  }

  self.fetch = async function (input, init) {
    const response = await originalFetch(input, init);
    try {
      if (!response.ok || !response.body) return response;
      // Every fetch during engine load is a distinct asset (wasm, stdlib zip,
      // lock file), so its Content-Length simply adds to the expected total.
      const len = Number(response.headers.get("content-length") || 0);
      if (len > 0) counted.total += len;
      const reader = response.body.getReader();
      const stream = new ReadableStream({
        pull(controller) {
          return reader.read().then(({ done, value }) => {
            if (done) {
              controller.close();
              report(true);
              return;
            }
            counted.loaded += value.byteLength;
            report(false);
            controller.enqueue(value);
          });
        },
        cancel(reason) {
          return reader.cancel(reason);
        },
      });
      return new Response(stream, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    } catch (e) {
      return response;
    }
  };
  return function uninstall() {
    self.fetch = originalFetch;
  };
}

async function fetchWithProgress(url, stage) {
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error("HTTP " + response.status + " for " + url);
  }
  if (!response.body) return await response.arrayBuffer();
  const total = Number(response.headers.get("content-length") || 0);
  const reader = response.body.getReader();
  const chunks = [];
  let loaded = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    loaded += value.byteLength;
    progress(stage, "loading", { loaded: loaded, total: total });
  }
  const out = new Uint8Array(loaded);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  return out.buffer;
}

const BRIDGE = `
import json, sys, traceback

_gn_last = {}

def _gn_convert(data_js, filename, opts_json):
    """Run gnnote.convert on the JS buffer; keep the result bytes in _gn_last."""
    global _gn_last
    _gn_last = {}
    try:
        from gnnote.convert import convert, Options
        data = data_js.to_bytes()
        opts = json.loads(opts_json)
        r = convert(data, filename, Options(**opts))
        del data
        _gn_last["data"] = r.data
        meta = {
            "ok": True,
            "filename": r.filename,
            "warnings": [str(w) for w in r.warnings],
            "stats": {str(k): v for k, v in dict(r.stats).items()},
            "sourceFormat": r.source_format,
            "targetFormat": r.target_format,
        }
        return json.dumps(meta)
    except Exception as e:  # report instead of raising so the traceback is clean
        return json.dumps({
            "ok": False,
            "message": "%s: %s" % (type(e).__name__, e),
            "traceback": traceback.format_exc(),
        })

def _gn_release():
    global _gn_last
    _gn_last = {}

def _gn_version():
    try:
        import gnnote
        return getattr(gnnote, "__version__", "")
    except Exception:
        return ""
`;

const DEFAULT_CDN = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";

async function loadEngine(base) {
  const mod = await import(/* webpackIgnore: true */ base + "pyodide.mjs");
  return mod.loadPyodide({ indexURL: base });
}

async function init(msg) {
  const base = String(msg.pyodideBase || DEFAULT_CDN);
  const cdn = String(msg.pyodideCdn || DEFAULT_CDN);
  const packageUrl = String(msg.packageUrl || "gnnote.zip");
  progress("engine", "loading", { loaded: 0, total: 0 });

  const uninstall = installFetchCounter();
  try {
    try {
      pyodide = await loadEngine(base);
    } catch (e) {
      // A vendored copy that does not load (missing files, a host answering HTML for
      // pyodide.mjs) must not leave the page without an engine: retry once from the CDN.
      if (base === cdn) throw e;
      console.warn("gnnote: Pyodide at " + base + " failed to load (" + (e && e.message ? e.message : e) + "); retrying from " + cdn);
      progress("engine", "loading", { loaded: 0, total: 0 });
      pyodide = await loadEngine(cdn);
    }
  } finally {
    uninstall();
  }

  progress("engine", "package");
  const zip = await fetchWithProgress(packageUrl, "package");
  try {
    pyodide.FS.mkdirTree(PACKAGE_DIR);
  } catch (e) {
    /* exists */
  }
  pyodide.unpackArchive(zip, "zip", { extractDir: PACKAGE_DIR });
  pyodide.runPython("import sys\nif " + JSON.stringify(PACKAGE_DIR) + " not in sys.path:\n    sys.path.insert(0, " + JSON.stringify(PACKAGE_DIR) + ")\n");
  pyodide.runPython(BRIDGE);
  // the three bridge functions are fetched once: every globals.get() creates a PyProxy
  convertFn = pyodide.globals.get("_gn_convert");
  releaseFn = pyodide.globals.get("_gn_release");
  versionFn = pyodide.globals.get("_gn_version");
  const version = versionFn();
  const python = pyodide.runPython("import sys; sys.version.split()[0]");
  post({ type: "ready", python: String(python), version: String(version || "") });
}

async function convert(msg) {
  if (initPromise) await initPromise;
  if (!pyodide || !convertFn) throw new Error("engine not initialised");
  const name = String(msg.name || "input");
  const options = msg.options || {};
  progress("converting", "converting", { name: name });

  const input = new Uint8Array(msg.buffer);
  let outBuffer = null;
  let meta;
  try {
    const metaJson = convertFn(input, name, JSON.stringify(options));
    meta = JSON.parse(String(metaJson));
    if (meta.ok) {
      const last = pyodide.globals.get("_gn_last");
      const pyBytes = last.get("data");
      try {
        const view = pyBytes.getBuffer();
        try {
          outBuffer = view.data.slice().buffer;
        } finally {
          view.release();
        }
      } finally {
        pyBytes.destroy();
        last.destroy();
      }
    }
  } finally {
    // free Python-side bytes whatever happened
    try {
      releaseFn();
    } catch (e) {
      /* ignore */
    }
  }
  if (!meta.ok) {
    post({ type: "error", id: msg.id, message: meta.message, traceback: meta.traceback });
    return;
  }
  post(
    {
      type: "done",
      id: msg.id,
      name: meta.filename,
      buffer: outBuffer,
      warnings: meta.warnings || [],
      stats: meta.stats || {},
      sourceFormat: meta.sourceFormat,
      targetFormat: meta.targetFormat,
    },
    [outBuffer]
  );
}

self.onmessage = async (event) => {
  const msg = event.data || {};
  try {
    if (msg.type === "init") {
      if (!initPromise) {
        initPromise = init(msg);
      }
      await initPromise;
    } else if (msg.type === "convert") {
      await convert(msg);
    }
  } catch (e) {
    if (msg.type === "init") {
      initPromise = null; // allow the main thread to retry with another base URL
      pyodide = null;
      convertFn = null;
      releaseFn = null;
      versionFn = null;
    }
    const info = errorInfo(e);
    post({ type: "error", id: msg.id, stage: msg.type, message: info.message, traceback: info.traceback });
  }
};
