// gnnote web UI - translation table.
//
// Convention: any element with a `data-i18n="key"` attribute gets its text
// content replaced by STRINGS[lang][key]; `data-i18n-title`, `data-i18n-aria`
// and `data-i18n-placeholder` do the same for the title, aria-label and
// placeholder attributes.  Strings used from JavaScript go through t(key, vars);
// `{name}` placeholders are substituted from `vars`.

export const DEFAULT_LANG = "sk";
export const LANGS = ["sk", "en"];

export const STRINGS = {
  sk: {
    "app.title": "gnnote – prevod poznámok medzi aplikáciami",
    "app.heading": "gnnote",
    "app.tagline": "Prevod poznámok medzi aplikáciami na ručné písanie. Rukopis zostáva upraviteľný. Súbor neopúšťa vaše zariadenie, pokiaľ nie je zapnutý serverový režim.",
    "app.supported": "Podporované aplikácie: {list}",
    "lang.toggle": "English",
    "lang.toggle.aria": "Prepnúť jazyk",

    "drop.label": "Vyberte alebo sem pretiahnite súbor s poznámkami",
    "drop.hint": "Ťuknutím otvoríte výber súborov",
    "drop.choose": "Vybrať súbor",
    "file.selected": "{name} ({size})",
    "file.badext": "Tento typ súboru nepoznám. Podporované sú súbory {list}.",
    "file.direction": "{from} → {to}",

    "options.heading": "Možnosti",
    "options.target": "Previesť do",
    "options.paper.legend": "Papier pri prevode do Notability",
    "options.paper.plain": "Obyčajný",
    "options.paper.plain.help": "Vstavané papiere GoodNotes sa nahradia papierom Notability; strany sa prispôsobia šírke. Odporúčané, overené na iPade.",
    "options.paper.pdf": "PDF",
    "options.paper.pdf.help": "Každá strana sa vloží ako PDF pozadie s pôvodným papierom GoodNotes (zachová linky a mriežky; menej overené).",
    "options.paper.na": "Používa sa len pri prevode do Notability.",
    "options.pressure": "Zachovať tlak pera (premenlivá hrúbka)",
    "options.pressure.help": "Vypnuté: každý ťah má konštantnú hrúbku.",
    "options.simplify": "Zjednodušenie ťahov (pt)",
    "options.simplify.help": "0 = vypnuté. Väčšie hodnoty odstránia drobné body a zmenšia súbor.",

    "convert": "Previesť",
    "convert.again": "Previesť znova",
    "reset": "Iný súbor",

    "progress.engine.loading": "Načítavam Python do prehliadača (prvýkrát ~13 MB)…",
    "progress.engine.bytes": "Stiahnuté {loaded} z {total}",
    "progress.engine.bytes.unknown": "Stiahnuté {loaded}",
    "progress.engine.package": "Inštalujem prevodník…",
    "progress.reading": "Čítam súbor…",
    "progress.converting": "Prevádzam {name}…",
    "progress.uploading": "Odosielam na server…",

    "result.heading": "Hotovo",
    "result.file": "Výstupný súbor",
    "result.stats": "Štatistika",
    "result.warnings": "Upozornenia",
    "result.warnings.none": "Žiadne upozornenia.",
    "result.download": "Stiahnuť",
    "result.share": "Zdieľať / Uložiť do Súborov",
    "result.share.failed": "Zdieľanie sa nepodarilo: {error}",
    "result.open.hint": "Na iPade otvorte stiahnutý súbor v aplikácii Súbory a zdieľajte ho do Notability alebo GoodNotes.",
    "result.warnings.lang": "Upozornenia prevodníka sú v angličtine.",
    "result.hosted.hint": "Táto stránka beží v chránenom prostredí, preto sa súbor uloží ako {saved}. V aplikácii Súbory ho premenujte na {name} a otvorte ho v cieľovej aplikácii (GoodNotes otvorí aj súbor .goodnotes.zip priamo).",
    "result.hosted.failed": "Uloženie sa nepodarilo: {error}",

    "stats.pages": "Strany",
    "stats.strokes": "Ťahy",
    "stats.images": "Obrázky",
    "stats.texts": "Textové polia",
    "stats.pdfs": "PDF dokumenty",
    "stats.size": "Veľkosť",

    "error.heading": "Prevod zlyhal",
    "error.traceback": "Technické podrobnosti",
    "error.engine": "Prevodník sa nepodarilo načítať: {error}",
    "error.engine.help": "Skontrolujte pripojenie na internet (Python sa sťahuje z CDN) alebo spustite lokálny server: python -m gnnote.server",
    "error.server": "Server odpovedal chybou {status}.",
    "error.toolarge": "Súbor ({size}) je väčší ako limit servera {limit}.",
    "error.noworker": "Tento prehliadač nepodporuje modulové Web Workery. Použite Safari 15+, Chrome alebo Firefox.",

    "engine.server": "Prevodník: server",
    "engine.browser": "Prevodník: v prehliadači (Pyodide)",
    "engine.unknown": "Prevodník: zisťujem…",
    "engine.failed": "Prevodník: nedostupný",

    "footer.privacy": "V režime prehliadača sa nič neodosiela na server.",
    "footer.source": "Zdrojový kód",
    "footer.version": "verzia {version}",
  },
  en: {
    "app.title": "gnnote – convert notes between apps",
    "app.heading": "gnnote",
    "app.tagline": "Convert notes between handwriting apps with the handwriting kept editable. Your file never leaves the device unless the server engine is enabled.",
    "app.supported": "Supported apps: {list}",
    "lang.toggle": "Slovensky",
    "lang.toggle.aria": "Switch language",

    "drop.label": "Choose or drop a notes file here",
    "drop.hint": "Tap to open the file picker",
    "drop.choose": "Choose file",
    "file.selected": "{name} ({size})",
    "file.badext": "Unsupported file type. Supported files: {list}.",
    "file.direction": "{from} → {to}",

    "options.heading": "Options",
    "options.target": "Convert to",
    "options.paper.legend": "Paper when converting to Notability",
    "options.paper.plain": "Plain",
    "options.paper.plain.help": "Built-in GoodNotes papers become Notability paper; pages scale to fit. Recommended, proven on iPad.",
    "options.paper.pdf": "PDF",
    "options.paper.pdf.help": "Every page becomes a PDF-backed page using the original GoodNotes paper (keeps lines and grids; less tested).",
    "options.paper.na": "Only used when converting to Notability.",
    "options.pressure": "Keep pen pressure (variable width)",
    "options.pressure.help": "Off: every stroke gets a constant width.",
    "options.simplify": "Stroke simplification (pt)",
    "options.simplify.help": "0 = off. Larger values drop tiny points and shrink the file.",

    "convert": "Convert",
    "convert.again": "Convert again",
    "reset": "Another file",

    "progress.engine.loading": "Loading Python into the browser (first time ~13 MB)…",
    "progress.engine.bytes": "Downloaded {loaded} of {total}",
    "progress.engine.bytes.unknown": "Downloaded {loaded}",
    "progress.engine.package": "Installing the converter…",
    "progress.reading": "Reading the file…",
    "progress.converting": "Converting {name}…",
    "progress.uploading": "Uploading to the server…",

    "result.heading": "Done",
    "result.file": "Output file",
    "result.stats": "Statistics",
    "result.warnings": "Warnings",
    "result.warnings.none": "No warnings.",
    "result.download": "Download",
    "result.share": "Share / Save to Files",
    "result.share.failed": "Sharing failed: {error}",
    "result.open.hint": "On iPad, open the downloaded file in Files and share it to Notability or GoodNotes.",
    "result.warnings.lang": "",
    "result.hosted.hint": "This page runs in a sandboxed host, so the file is saved as {saved}. Rename it to {name} in the Files app and open it in the target app (GoodNotes opens .goodnotes.zip directly).",
    "result.hosted.failed": "Saving failed: {error}",

    "stats.pages": "Pages",
    "stats.strokes": "Strokes",
    "stats.images": "Images",
    "stats.texts": "Text boxes",
    "stats.pdfs": "PDF documents",
    "stats.size": "Size",

    "error.heading": "Conversion failed",
    "error.traceback": "Technical details",
    "error.engine": "The engine could not be loaded: {error}",
    "error.engine.help": "Check your internet connection (Python is downloaded from a CDN) or run the local server: python -m gnnote.server",
    "error.server": "The server answered with error {status}.",
    "error.toolarge": "The file ({size}) is larger than the server limit of {limit}.",
    "error.noworker": "This browser does not support module Web Workers. Use Safari 15+, Chrome or Firefox.",

    "engine.server": "Engine: server",
    "engine.browser": "Engine: in-browser (Pyodide)",
    "engine.unknown": "Engine: detecting…",
    "engine.failed": "Engine: unavailable",

    "footer.privacy": "In browser mode nothing is sent to a server.",
    "footer.source": "Source code",
    "footer.version": "version {version}",
  },
};

let current = DEFAULT_LANG;

export function currentLang() {
  return current;
}

/** Look up a string in the current language, falling back to English, then the key. */
export function t(key, vars) {
  const table = STRINGS[current] || STRINGS[DEFAULT_LANG];
  let s = table[key];
  if (s === undefined) s = STRINGS.en[key];
  if (s === undefined) s = key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      s = s.split("{" + k + "}").join(String(v));
    }
  }
  return s;
}

/** Set the language and re-translate every data-i18n* element under root. */
export function applyLanguage(lang, root) {
  current = LANGS.includes(lang) ? lang : DEFAULT_LANG;
  const doc = root || document;
  doc.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  doc.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
  });
  doc.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria")));
  });
  doc.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
  });
  if (typeof document !== "undefined") {
    document.documentElement.lang = current;
    document.title = t("app.title");
  }
  return current;
}

/** Initial language: stored preference, else the Slovak default. */
export function initialLang() {
  try {
    const stored = localStorage.getItem("gnnote.lang");
    if (stored && LANGS.includes(stored)) return stored;
  } catch (e) {
    /* storage may be unavailable (private mode) */
  }
  return DEFAULT_LANG;
}

export function rememberLang(lang) {
  try {
    localStorage.setItem("gnnote.lang", lang);
  } catch (e) {
    /* ignore */
  }
}
