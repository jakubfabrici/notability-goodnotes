#!/usr/bin/env node
// gnnote end-to-end test: serve dist/, open the UI in Chromium, upload a file,
// click Convert, wait for the download and save it.
//
// Usage:
//   NODE_PATH=$(npm root -g) node tests/e2e/run.js --dist dist --input FILE --expect-ext .note
//          [--pyodide-dir DIR]        serve DIR under /pyodide/ and point the page at it
//          [--pyodide-base URL]       set window.PYODIDE_BASE (absolute, or /path/ on the test server)
//          [--out PATH]               where to save the download (default: tests/e2e/out/<name>)
//          [--port N]                 static server port (default: random free port)
//          [--browser PATH]           Chromium executable (default: $PW_CHROMIUM, /opt/pw-browsers/chromium
//                                     when it exists, else Playwright's own Chromium)
//          [--timeout MS]             overall conversion timeout (default: 300000)
//          [--to FORMAT] [--paper plain|pdf] [--no-pressure] [--simplify N]   (Notability options)
//          [--pdf-ink flatten|annotations]                                  (PDF option)
//          [--lang sk|en|uk]          switch the page to this language first
//          [--verbose]                echo browser console messages
//
// Prints one JSON line {ok, outputPath, name, size, stats, warnings, engine, ms}
// and exits 0 on success, 1 on failure (JSON then carries "error").
//
// Requires playwright (global install resolved through `npm root -g`, or NODE_PATH, or
// a local node_modules).

"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");

function parseArgs(argv) {
  const args = {
    dist: "dist",
    input: null,
    expectExt: null,
    pyodideDir: null,
    pyodideBase: null,
    out: null,
    port: 0,
    browser: process.env.PW_CHROMIUM || (fs.existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : null),
    timeout: 300000,
    paper: null,
    to: null,
    pressure: true,
    simplify: null,
    pdfInk: null,
    lang: null,
    verbose: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    switch (a) {
      case "--dist": args.dist = next(); break;
      case "--input": args.input = next(); break;
      case "--expect-ext": args.expectExt = next(); break;
      case "--pyodide-dir": args.pyodideDir = next(); break;
      case "--pyodide-base": args.pyodideBase = next(); break;
      case "--out": args.out = next(); break;
      case "--port": args.port = parseInt(next(), 10); break;
      case "--browser": args.browser = next(); break;
      case "--timeout": args.timeout = parseInt(next(), 10); break;
      case "--paper": args.paper = next(); break;
      case "--to": args.to = next(); break;
      case "--no-pressure": args.pressure = false; break;
      case "--simplify": args.simplify = next(); break;
      case "--pdf-ink": args.pdfInk = next(); break;
      case "--lang": args.lang = next(); break;
      case "--verbose": args.verbose = true; break;
      case "-h": case "--help":
        console.log(fs.readFileSync(__filename, "utf8").split("\n").filter((l) => l.startsWith("//")).join("\n"));
        process.exit(0);
      default:
        throw new Error("unknown argument: " + a);
    }
  }
  if (!args.input) throw new Error("--input FILE is required");
  return args;
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".wasm": "application/wasm",
  ".zip": "application/zip",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};

// require("playwright") from NODE_PATH, the current directory or the global
// npm root, so the script also works without NODE_PATH being set.
function loadPlaywright() {
  const candidates = ["playwright"];
  try {
    candidates.push(path.join(require("child_process").execSync("npm root -g", { encoding: "utf8" }).trim(), "playwright"));
  } catch (e) {
    /* npm missing */
  }
  candidates.push(path.join(process.cwd(), "node_modules", "playwright"));
  let lastError = null;
  for (const c of candidates) {
    try {
      return require(c);
    } catch (e) {
      lastError = e;
    }
  }
  throw new Error("playwright not found (npm i -g playwright, or NODE_PATH=$(npm root -g)): " + lastError.message);
}

function startServer(distDir, pyodideDir, port) {
  const roots = [{ prefix: "/", dir: path.resolve(distDir) }];
  if (pyodideDir) roots.unshift({ prefix: "/pyodide/", dir: path.resolve(pyodideDir) });
  const server = http.createServer((req, res) => {
    let urlPath;
    try {
      urlPath = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
    } catch (e) {
      res.statusCode = 400;
      return res.end("bad request");
    }
    if (urlPath.startsWith("/api/")) {
      res.statusCode = 404; // no server engine: force the Pyodide path
      return res.end("no api");
    }
    if (urlPath === "/") urlPath = "/index.html";
    const root = roots.find((r) => urlPath.startsWith(r.prefix));
    const rel = urlPath.slice(root.prefix.length);
    const file = path.join(root.dir, rel);
    if (!file.startsWith(root.dir) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.statusCode = 404;
      return res.end("not found");
    }
    const stat = fs.statSync(file);
    res.setHeader("Content-Type", MIME[path.extname(file).toLowerCase()] || "application/octet-stream");
    res.setHeader("Content-Length", String(stat.size));
    res.setHeader("Cache-Control", "no-cache");
    if (req.method === "HEAD") return res.end();
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { chromium } = loadPlaywright();
  const t0 = Date.now();

  const distDir = path.resolve(args.dist);
  if (!fs.existsSync(path.join(distDir, "index.html"))) {
    throw new Error("no index.html in --dist " + distDir + " (run scripts/build_web.py first)");
  }
  const input = path.resolve(args.input);
  if (!fs.existsSync(input)) throw new Error("input file not found: " + input);

  const server = await startServer(distDir, args.pyodideDir, args.port);
  const origin = "http://127.0.0.1:" + server.address().port;
  let pyodideBase = null;
  if (args.pyodideBase) pyodideBase = new URL(args.pyodideBase, origin + "/").href;
  else if (args.pyodideDir) pyodideBase = origin + "/pyodide/";

  const browser = await chromium.launch({
    executablePath: args.browser || undefined, // undefined: Playwright's own Chromium
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const logs = [];
  try {
    const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 390, height: 844 } });
    if (pyodideBase) {
      await context.addInitScript("window.PYODIDE_BASE = " + JSON.stringify(pyodideBase) + ";");
    }
    const page = await context.newPage();
    page.on("console", (m) => {
      const line = "[console." + m.type() + "] " + m.text();
      logs.push(line);
      if (args.verbose) console.error(line);
    });
    page.on("pageerror", (e) => {
      logs.push("[pageerror] " + e.message);
      if (args.verbose) console.error("[pageerror] " + e.message);
    });

    await page.goto(origin + "/", { waitUntil: "load" });
    if (args.lang) await page.selectOption("#lang", args.lang);
    await page.setInputFiles("#file", input);
    if (args.to) await page.selectOption("#target", args.to);
    if (args.paper) await page.check('input[name="paper"][value="' + args.paper + '"]');
    if (!args.pressure) await page.uncheck("#pressure");
    if (args.simplify !== null) await page.fill("#simplify", String(args.simplify));
    if (args.pdfInk) await page.check('input[name="pdf_ink"][value="' + args.pdfInk + '"]');
    await page.waitForSelector("#convert:not([disabled])", { timeout: 30000 });

    const downloadPromise = page.waitForEvent("download", { timeout: args.timeout });
    downloadPromise.catch(() => {}); // only awaited after a successful conversion
    const resultPromise = page.waitForFunction(() => window.__gnnoteLastResult !== undefined, null, { timeout: args.timeout });
    await page.click("#convert");
    await resultPromise;
    const result = await page.evaluate(() => window.__gnnoteLastResult);
    if (result.error) {
      const tb = await page.evaluate(() => (document.getElementById("error-traceback") || {}).textContent || "");
      throw new Error("conversion failed: " + result.error + (tb ? "\n" + tb : ""));
    }
    await page.waitForSelector("#result:not([hidden])", { timeout: 10000 });
    // Download through the a[download] button, like a user would.
    await page.click("#download");
    const download = await downloadPromise;
    const suggested = download.suggestedFilename();
    const outPath = args.out
      ? path.resolve(args.out)
      : path.join(__dirname, "out", suggested);
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    await download.saveAs(outPath);
    const size = fs.statSync(outPath).size;
    if (args.expectExt && !suggested.toLowerCase().endsWith(args.expectExt.toLowerCase())) {
      throw new Error("expected extension " + args.expectExt + " but got " + suggested);
    }
    if (size === 0) throw new Error("downloaded file is empty");
    const shareVisible = await page.evaluate(() => !document.getElementById("share").hidden);
    console.log(JSON.stringify({
      ok: true,
      outputPath: outPath,
      name: suggested,
      size: size,
      stats: result.stats,
      warnings: result.warnings,
      sourceFormat: result.sourceFormat,
      targetFormat: result.targetFormat,
      engine: result.engine,
      shareButton: shareVisible,
      ms: Date.now() - t0,
    }));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: e.message, console: logs.slice(-30) }));
    process.exitCode = 1;
  } finally {
    await browser.close();
    server.close();
  }
}

main().catch((e) => {
  console.log(JSON.stringify({ ok: false, error: e.message }));
  process.exit(1);
});
