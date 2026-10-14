/*
 * Draw the real FrameSnap page with real data and save PNG screenshots.
 *
 *   python docs/make_state.py /tmp/shotdata "clip.mp4=shot_01.mp4" ...     (real analysis)
 *   npm install puppeteer-core @sparticuz/chromium                          (once)
 *   node docs/shoot.js /tmp/shotdata docs/screenshots
 *
 * Set CHROME_PATH to use an installed Chrome instead of the bundled one.
 */
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");

const dataDir = process.argv[2] || "/tmp/shotdata";
const outDir = process.argv[3] || path.join(__dirname, "screenshots");
const htmlUrl = "file://" + path.resolve(__dirname, "..", "framesnap", "ui", "index.html");
const ready = JSON.parse(fs.readFileSync(path.join(dataDir, "state_ready.json"), "utf8"));
const done = JSON.parse(fs.readFileSync(path.join(dataDir, "state_done.json"), "utf8"));
const timelines = JSON.parse(fs.readFileSync(path.join(dataDir, "timelines.json"), "utf8"));

async function executable() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const chromium = require("@sparticuz/chromium");
  return (chromium.default || chromium).executablePath();
}

async function open(browser, lang, scheme, state, selected) {
  const page = await browser.newPage();
  await page.setViewport({ width: 560, height: 1000, deviceScaleFactor: 2 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
  await page.evaluateOnNewDocument((language, tl) => {
    Object.defineProperty(navigator, "language", { get: () => language });
    window.pywebview = { api: new Proxy({}, { get: (_, name) => (...a) =>
      name === "get_timeline" ? Promise.resolve(tl[a[0]] || null) : Promise.resolve() }) };
  }, lang, timelines);
  await page.goto(htmlUrl);
  await page.evaluate((s) => window.render(s), state);
  if (selected) await page.evaluate((id) => window.__framesnap.select(id), selected);
  await new Promise((r) => setTimeout(r, 400));
  return page;
}

async function hoverSpike(page, frame) {
  // let the redraw that follows any viewport change settle, then bring the timeline into view
  // and show the tooltip of a frame (same code as a mouse move)
  await new Promise((r) => setTimeout(r, 700));
  await page.evaluate(() => document.getElementById("tlsvg").scrollIntoView({ block: "center" }));
  const tl = timelines[Object.keys(timelines)[0]];
  await page.evaluate((f) => window.__framesnap.hover(f), frame / tl.n);
  await new Promise((r) => setTimeout(r, 150));
}

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await puppeteer.launch({ args: ["--no-sandbox", "--font-render-hinting=none"], executablePath: await executable(), headless: "shell" });
  for (const lang of ["en", "fr"]) {
    let page = await open(browser, lang, "dark", ready, 1);
    await page.screenshot({ path: path.join(outDir, "main-" + lang + ".png"), fullPage: true });
    await (await page.$("section.repair")).screenshot({ path: path.join(outDir, "outputs-" + lang + ".png") });
    await hoverSpike(page, 7);
    // a plain clip of the page keeps the mouse where it is, so the tooltip stays visible
    const tb = await page.evaluate(() => {
      const r = document.getElementById("tlbox").getBoundingClientRect();
      return { x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height };
    });
    await page.screenshot({ path: path.join(outDir, "timeline-" + lang + ".png"), clip: tb, captureBeyondViewport: false });
    await page.close();

    page = await open(browser, lang, "dark", done, 1);
    await page.screenshot({ path: path.join(outDir, "results-" + lang + ".png"), fullPage: true });
    await page.close();
  }
  for (const lang of ["en", "fr"]) {
    const light = await open(browser, lang, "light", ready, 1);
    await light.screenshot({ path: path.join(outDir, "main-" + lang + "-light.png"), fullPage: true });
    await light.close();
  }
  await browser.close();
  console.log(fs.readdirSync(outDir).sort().map((f) => f + " " + Math.round(fs.statSync(path.join(outDir, f)).size / 1024) + " KB").join("\n"));
})().catch((e) => { console.error(e); process.exit(1); });
