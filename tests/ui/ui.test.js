// UI tests: load the real page in jsdom with a fake pywebview API.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "..", "framesnap", "ui", "index.html"), "utf8");
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 15));

function load(language, timelines) {
  const calls = [];
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    beforeParse(window) {
      Object.defineProperty(window.navigator, "language", { value: language || "en", configurable: true });
      window.pywebview = {
        api: new Proxy({}, {
          get: (_, name) => (...a) => {
            calls.push([name, ...a]);
            if (name === "get_timeline") return Promise.resolve((timelines || {})[a[0]] || null);
            return Promise.resolve();
          },
        }),
      };
    },
  });
  return { window: dom.window, doc: dom.window.document, calls };
}

const item = (o) => Object.assign({
  id: 1, name: "clip.mp4", status: "ready", fps: 23.976, target: 24, action: "retime", pct: 0, message: "",
  analysis: "done", analysis_pct: 100, dups: 0, dup_pct: 0, runs: { double: 0, triple: 0, more: 0 }, tl_v: 1, results: [],
}, o);
const repairState = (o) => Object.assign({
  modes: ["off"], sensitivity: "normal", engine: "auto", gpu: { checked: true, ok: false, software: false, device: "" },
  suffix: false, auto_analyse: true,
  suffixes: { off: "_recode", interpolate: "_recode-inter", cut_keep: "_recode-keep", cut_stretch: "_recode-speed" },
  folders: { off: "cadence", interpolate: "inter", cut_keep: "keep", cut_stretch: "speed" },
}, o);
const base = (o) => Object.assign({ version: "2", mode: "auto", skip_existing: true, running: false, error: "", repair: repairState(), items: [] }, o);
const TL = (o) => Object.assign({
  n: 121, fps: 24, seconds: 5.04, bin: 1, motion: Array.from({ length: 120 }, (_, i) => 2 + (i % 4)), peak: 5,
  runs: [[3, 1], [7, 1], [20, 2], [40, 3]], counts: { double: 2, triple: 1, more: 1 },
}, o);

// ------------------------------------------------------------------ basics

test("empty state: nothing to recode", () => {
  const { window, doc } = load("en");
  window.render(base());
  assert.equal(doc.getElementById("go").disabled, true);
  assert.equal(doc.getElementById("clear").disabled, true);
  assert.equal(doc.getElementById("list").getAttribute("data-empty"), "No clip yet");
  assert.ok(doc.getElementById("tlbody").textContent.includes("Select a clip"));
});

test("shows detected rate and plan, enables Recode", () => {
  const { window, doc } = load("en");
  window.render(base({ items: [item()] }));
  const meta = doc.querySelector(".meta").textContent;
  assert.ok(meta.includes("23.976 fps") && meta.includes("snapped to 24 fps"));
  assert.equal(doc.getElementById("go").disabled, false);
  assert.equal(doc.getElementById("go").textContent, "Recode");
});

test("french interface when the system language is french", () => {
  const { window, doc } = load("fr-FR");
  window.render(base({ items: [item({ fps: 29.97, target: 30 })] }));
  assert.equal(doc.getElementById("go").textContent, "Recoder");
  assert.ok(doc.querySelector(".meta").textContent.includes("29,97 fps"));
  assert.ok(doc.querySelector(".meta").textContent.includes("ajusté à 30 fps"));
});

test("segmented rate control reflects the mode and calls the API", () => {
  const { window, doc, calls } = load("en");
  window.render(base({ mode: 25 }));
  const checked = [...doc.querySelectorAll("#seg button")].filter((b) => b.getAttribute("aria-checked") === "true");
  assert.equal(checked.length, 1);
  assert.equal(checked[0].getAttribute("data-mode"), "25");
  doc.querySelector('#seg [data-mode="60"]').click();
  doc.querySelector('#seg [data-mode="auto"]').click();
  assert.deepEqual(calls.filter((c) => c[0] === "set_mode"), [["set_mode", 60], ["set_mode", "auto"]]);
  assert.ok(doc.getElementById("hint").textContent.includes("25 fps"));
});

test("far-from-standard clip is flagged", () => {
  const { window, doc } = load("en");
  window.render(base({ items: [item({ fps: 50, target: null, action: "skip", analysis: "skip", dups: null })] }));
  assert.ok(doc.querySelector(".meta").textContent.includes("not close to 24, 25, 30 or 60"));
});

test("running state: progress, Cancel button, every control locked", () => {
  const { window, doc, calls } = load("en");
  window.render(base({ running: true, repair: repairState({ modes: ["off", "interpolate"] }), items: [item({ status: "running", pct: 42.4 })] }));
  assert.equal(doc.querySelector(".status").textContent, "42%");
  assert.equal(doc.querySelector(".bar").style.width, "42.4%");
  const go = doc.getElementById("go");
  assert.equal(go.textContent, "Cancel");
  go.click();
  assert.deepEqual(calls.at(-1), ["cancel"]);
  for (const sel of ['#seg [data-mode="30"]', '[data-repair="cut_keep"]', '[data-sens="strict"]', '[data-engine="gpu"]', "#suffix", "#skip", "#clear"]) {
    assert.equal(doc.querySelector(sel).disabled, true, sel);
  }
});

test("Recode starts the job", () => {
  const { window, doc, calls } = load("en");
  window.render(base({ items: [item()] }));
  doc.getElementById("go").click();
  assert.deepEqual(calls.at(-1), ["start"]);
});

test("hostile names are shown as text, never as HTML", () => {
  const { window, doc } = load("en");
  window.render(base({ items: [item({ name: '<img src=x onerror="window.pwned=1">.mp4' })] }));
  assert.equal(doc.querySelector(".name").textContent, '<img src=x onerror="window.pwned=1">.mp4');
  assert.equal(doc.querySelector("#list img"), null);
  assert.equal(window.pwned, undefined);
});

test("buttons and checkboxes call the API", () => {
  const { window, doc, calls } = load("en");
  window.render(base({ items: [item()] }));
  doc.getElementById("pickFiles").click();
  doc.getElementById("pickFolder").click();
  doc.getElementById("clear").click();
  doc.querySelector(".rm").click();
  for (const [id, value] of [["skip", false], ["suffix", true], ["autoan", false]]) {
    const cb = doc.getElementById(id); cb.checked = value; cb.dispatchEvent(new window.Event("change"));
  }
  const user = calls.filter((c) => !["get_timeline", "get_video_path", "set_flags"].includes(c[0]));  // page also requests timeline and video path
  assert.deepEqual(user.map((c) => c[0]), ["pick_files", "pick_folder", "clear", "remove", "set_skip_existing", "set_use_suffix", "set_auto_analyse"]);
  assert.deepEqual(user.at(-2), ["set_use_suffix", true]);
  assert.deepEqual(user.at(-1), ["set_auto_analyse", false]);
});

test("drag over highlights the drop zone and drop clears it", () => {
  const { window, doc } = load("en");
  const drop = doc.getElementById("drop");
  doc.dispatchEvent(new window.Event("dragover", { cancelable: true }));
  assert.ok(drop.classList.contains("over"));
  const ev = new window.Event("drop", { cancelable: true });
  doc.dispatchEvent(ev);
  assert.ok(!drop.classList.contains("over"));
  assert.equal(ev.defaultPrevented, true);
});

test("pywebviewready fetches the initial state", async () => {
  const { window, doc } = load("en");
  window.pywebview.api = { get_state: () => Promise.resolve(base({ mode: 30, items: [item()] })) };
  window.dispatchEvent(new window.Event("pywebviewready"));
  await tick();
  assert.equal(doc.querySelector("#list li").getAttribute("data-id"), "1");
  assert.equal(doc.querySelector('#seg [data-mode="30"]').getAttribute("aria-checked"), "true");
});

test("a state without repair information still renders", () => {
  const { window, doc } = load("en");
  const s = base(); delete s.repair;
  window.render(s);
  assert.equal(doc.getElementById("go").textContent, "Recode");
  assert.equal(doc.querySelector('[data-repair="off"]').getAttribute("aria-checked"), "true");
});

// ----------------------------------------------------- outputs (several at once)

test("outputs are checkboxes: several can be ticked at once", () => {
  const { window, doc, calls } = load("en");
  window.render(base({ repair: repairState({ modes: ["interpolate", "cut_stretch"] }) }));
  const on = [...doc.querySelectorAll("#repairModes button")].filter((b) => b.getAttribute("aria-checked") === "true").map((b) => b.getAttribute("data-repair"));
  assert.deepEqual(on, ["interpolate", "cut_stretch"]);
  assert.equal(doc.querySelector('[data-repair="off"]').getAttribute("role"), "checkbox");
  doc.querySelector('[data-repair="cut_keep"]').click();
  doc.querySelector('[data-repair="interpolate"]').click();
  assert.deepEqual(calls.map((c) => c.join(":")), ["toggle_repair:cut_keep", "toggle_repair:interpolate"]);
});

test("every output is described, with the key promise of each", () => {
  const { window, doc } = load("en");
  window.render(base());
  const text = (m) => doc.querySelector('[data-repair="' + m + '"]').textContent;
  assert.ok(text("off").includes("Frame rate only"));
  assert.ok(text("interpolate").includes("recommended") && text("interpolate").includes("lip sync"));
  assert.ok(text("cut_keep").includes("VIDEO SHORTER THAN SOUNDTRACK"));
  assert.ok(text("cut_stretch").includes("pitch") && text("cut_stretch").includes("avoid for dialogue"));
});

test("descriptions are translated", () => {
  const { window, doc } = load("fr-FR");
  window.render(base());
  const text = (m) => doc.querySelector('[data-repair="' + m + '"]').textContent;
  assert.ok(text("off").includes("Cadence seule"));
  assert.ok(text("interpolate").includes("synchro des voix"));
  assert.ok(text("cut_keep").includes("écran vert"));
  assert.ok(text("cut_stretch").includes("sans changer sa hauteur"));
});

test("sensitivity and engine call the API, engine only matters for interpolation", () => {
  const { window, doc, calls } = load("en");
  window.render(base({ repair: repairState({ modes: ["off"] }) }));
  assert.equal(doc.getElementById("engineRow").style.display, "none");
  window.render(base({ repair: repairState({ modes: ["off", "interpolate"], sensitivity: "loose", engine: "gpu" }) }));
  assert.equal(doc.getElementById("engineRow").style.display, "");
  assert.equal(doc.querySelector('[data-sens="loose"]').getAttribute("aria-checked"), "true");
  assert.equal(doc.querySelector('[data-engine="gpu"]').getAttribute("aria-checked"), "true");
  doc.querySelector('[data-sens="strict"]').click();
  doc.querySelector('[data-engine="cpu"]').click();
  assert.deepEqual(calls.map((c) => c.join(":")), ["set_sensitivity:strict", "set_engine:cpu"]);
});

test("GPU status: checking, found, software only, missing", () => {
  const { window, doc } = load("en");
  const info = () => doc.getElementById("gpuInfo");
  const st = (gpu) => base({ repair: repairState({ modes: ["interpolate"], gpu }) });
  window.render(st({ checked: false }));
  assert.ok(info().textContent.includes("Looking for a GPU"));
  window.render(st({ checked: true, ok: true, software: false, device: "Apple M2 Pro" }));
  assert.ok(info().textContent.includes("Apple M2 Pro") && info().textContent.includes("RIFE") && info().classList.contains("good"));
  window.render(st({ checked: true, ok: true, software: true, device: "llvmpipe" }));
  assert.ok(info().textContent.includes("No usable GPU") && info().classList.contains("cpu"));
  window.render(st({ checked: true, ok: false, available: false }));
  assert.ok(info().textContent.includes("CPU"));
  window.render(base({ repair: repairState({ modes: ["cut_keep"] }) }));
  assert.equal(info().textContent, "");
});

test("file name preview: same name, suffix, one folder per output", () => {
  const { window, doc } = load("en");
  const names = () => doc.getElementById("names").textContent;
  const st = (o) => base({ repair: repairState(o), items: [item({ name: "shots/take 1.mp4" })] });
  window.render(st({ modes: ["interpolate"] }));
  assert.ok(names().includes("take 1.mp4") && !names().includes("_recode"));
  window.render(st({ modes: ["interpolate"], suffix: true }));
  assert.ok(names().includes("take 1_recode-inter.mp4"));
  window.render(st({ modes: ["off", "interpolate", "cut_keep", "cut_stretch"] }));
  assert.ok(names().includes("one folder per output") && names().includes("inter/take 1.mp4") && names().includes("speed/take 1.mp4") && names().includes("cadence/take 1.mp4"));
  window.render(st({ modes: ["off", "cut_keep", "cut_stretch"], suffix: true }));
  assert.ok(names().includes("take 1_recode.mp4") && names().includes("take 1_recode-keep.mp4") && names().includes("take 1_recode-speed.mp4"));
  assert.ok(!names().includes("folder"));
});

test("file name preview is translated", () => {
  const { window, doc } = load("fr-FR");
  window.render(base({ repair: repairState({ modes: ["off", "interpolate"] }), items: [item({ name: "a.mov" })] }));
  assert.ok(doc.getElementById("names").textContent.includes("un dossier par sortie"));
});

// ------------------------------------------------------------- analysis status

test("analysis status in the list: running, off, done", () => {
  const { window, doc } = load("en");
  window.render(base({ items: [
    item({ id: 1, analysis: "running", analysis_pct: 45.4, dups: null }),
    item({ id: 2, name: "b.mp4", analysis: "off", dups: null }),
    item({ id: 3, name: "c.mp4", dups: 9, dup_pct: 7.4 }),
    item({ id: 4, name: "d.mp4", dups: 0 }),
  ] }));
  const meta = [...doc.querySelectorAll(".meta")].map((m) => m.textContent);
  assert.ok(meta[0].includes("Analysis 45%"));
  assert.ok(meta[1].includes("analysis off"));
  assert.ok(meta[2].includes("9 held frames (7.4%)"));
  assert.ok(meta[3].includes("no held frame"));
});

test("french count uses the comma decimal", () => {
  const { window, doc } = load("fr-FR");
  window.render(base({ items: [item({ dups: 9, dup_pct: 7.4 })] }));
  assert.ok(doc.querySelector(".meta").textContent.includes("9 images copiées (7,4 %)"));
});

test("the Analyse button appears only when automatic analysis is off", () => {
  const { window, doc, calls } = load("en");
  window.render(base());
  assert.equal(doc.getElementById("analyseNow").style.display, "none");
  assert.equal(doc.getElementById("autoan").checked, true);
  window.render(base({ repair: repairState({ auto_analyse: false }) }));
  assert.equal(doc.getElementById("analyseNow").style.display, "");
  doc.getElementById("analyseNow").click();
  assert.deepEqual(calls.at(-1), ["analyse_now"]);
});

// ------------------------------------------------------------------ results and rerun

test("results of each output are listed, the button offers to recode again", () => {
  const { window, doc, calls } = load("en");
  window.render(base({ repair: repairState({ modes: ["interpolate", "cut_keep"] }), items: [item({ results: [
    { variant: "interpolate", status: "done", message: "", backend: "RIFE GPU (Apple M2)", dups: 9, file: "a.mp4" },
    { variant: "cut_keep", status: "done", message: "", backend: "", dups: 9, file: "a.mp4" },
    { variant: "cut_stretch", status: "error", message: "boom", backend: "", dups: 0, file: "" },
  ] })] }));
  const res = doc.querySelector(".res").textContent;
  assert.ok(res.includes("inter: done (RIFE GPU (Apple M2))") && res.includes("keep: done") && res.includes("speed: error"));
  assert.equal(doc.getElementById("go").textContent, "Recode again");
  assert.equal(doc.getElementById("go").disabled, false);
  assert.equal(doc.getElementById("open").disabled, false);
  assert.ok(doc.getElementById("summary").textContent.includes("2 outputs written"));
  doc.getElementById("go").click();
  assert.deepEqual(calls.at(-1), ["start"]);
});

test("a fresh clip offers Recode, not Recode again", () => {
  const { window, doc } = load("en");
  window.render(base({ items: [item()] }));
  assert.equal(doc.getElementById("go").textContent, "Recode");
  assert.equal(doc.getElementById("open").disabled, true);
});

// ------------------------------------------------------------------ timeline

test("the first clip is selected and its timeline is requested once per analysis version", async () => {
  const { window, doc, calls } = load("en", { 1: TL() });
  window.render(base({ items: [item({ id: 1 }), item({ id: 2, name: "b.mp4" })] }));
  await tick();
  assert.ok(doc.querySelector('#list li[data-id="1"]').classList.contains("sel"));
  assert.deepEqual(calls.filter((c) => c[0] === "get_timeline"), [["get_timeline", 1]]);
  window.render(base({ items: [item({ id: 1 }), item({ id: 2, name: "b.mp4" })] }));
  await tick();
  assert.equal(calls.filter((c) => c[0] === "get_timeline").length, 1);              // cached
  window.render(base({ items: [item({ id: 1, tl_v: 2 }), item({ id: 2, name: "b.mp4" })] }));
  await tick();
  assert.equal(calls.filter((c) => c[0] === "get_timeline").length, 2);              // sensitivity changed
});

test("the timeline draws one spike per held run, coloured by length, with a legend", async () => {
  const { window, doc } = load("en", { 1: TL() });
  window.render(base({ items: [item({ id: 1, dups: 4 })] }));
  await tick();
  const rects = [...doc.querySelectorAll("#tlsvg rect")];
  assert.equal(rects.length, 4);
  assert.deepEqual(rects.map((r) => r.getAttribute("class")), ["sp1", "sp1", "sp2", "sp3"]);
  const h = rects.map((r) => parseFloat(r.getAttribute("height")));
  assert.ok(h[0] < h[2] && h[2] < h[3]);                                              // longer run, taller spike
  assert.ok(doc.getElementById("tllegend").textContent.includes("double 2"));
  assert.ok(doc.getElementById("tllegend").textContent.includes("triple 1"));
  assert.ok(doc.getElementById("tllegend").textContent.includes("4 and more 1"));
  assert.ok(doc.getElementById("tlbody").textContent.includes("121 frames"));
  assert.ok(doc.querySelector("#tlsvg path.motion"));
});

test("a clip without held frames says so", async () => {
  const { window, doc } = load("en", { 1: TL({ runs: [], counts: { double: 0, triple: 0, more: 0 } }) });
  window.render(base({ items: [item({ id: 1 })] }));
  await tick();
  assert.equal(doc.querySelectorAll("#tlsvg rect").length, 0);
  assert.ok(doc.getElementById("tlbody").textContent.includes("No held frame found"));
});

test("clicking a clip selects it and shows its own timeline", async () => {
  const { window, doc, calls } = load("en", { 1: TL(), 2: TL({ runs: [[10, 1]], counts: { double: 1, triple: 0, more: 0 } }) });
  window.render(base({ items: [item({ id: 1 }), item({ id: 2, name: "b.mp4" })] }));
  await tick();
  doc.querySelector('#list li[data-id="2"]').click();
  await tick();
  assert.ok(doc.querySelector('#list li[data-id="2"]').classList.contains("sel"));
  assert.equal(doc.getElementById("tltitle").textContent, "b.mp4");
  assert.equal(doc.querySelectorAll("#tlsvg rect").length, 1);
  assert.ok(calls.some((c) => c[0] === "get_timeline" && c[1] === 2));
});

test("timeline notes while analysing, when off, skipped or failed", () => {
  const { window, doc } = load("en");
  const note = () => doc.getElementById("tlbody").textContent;
  window.render(base({ items: [item({ analysis: "running", analysis_pct: 30, dups: null })] }));
  assert.ok(note().includes("Analysis in progress: 30%"));
  window.render(base({ items: [item({ analysis: "off", dups: null })] }));
  assert.ok(note().includes("Analysis is off"));
  window.render(base({ items: [item({ analysis: "skip", dups: null })] }));
  assert.ok(note().includes("not close to 24, 25, 30 or 60"));
  window.render(base({ items: [item({ analysis: "error", dups: null })] }));
  assert.ok(note().includes("analysis failed"));
});

test("hovering the timeline names the frame and the kind of run", async () => {
  const { window, doc } = load("en", { 1: TL() });
  window.render(base({ items: [item({ id: 1 })] }));
  await tick();
  const d = window.__framesnap.describeAt;
  const tl = TL();
  assert.ok(d(tl, 3 / 121).text.includes("shown twice") && d(tl, 3 / 121).run[0] === 3);
  assert.ok(d(tl, 21 / 121).text.includes("shown 3 times"));
  assert.ok(d(tl, 41 / 121).text.includes("shown 4 times"));
  assert.equal(d(tl, 90 / 121).run, null);
  assert.ok(d(tl, 60 / 121).text.includes("frame 60"));
  const svg = doc.getElementById("tlsvg");
  svg.getBoundingClientRect = () => ({ left: 0, width: 1210, top: 0, height: 124 });
  svg.dispatchEvent(new window.MouseEvent("mousemove", { clientX: 30 }));   // frame 3
  assert.ok(doc.getElementById("tltip").textContent.includes("shown twice"));
  assert.equal(doc.getElementById("tltip").style.display, "block");
  svg.dispatchEvent(new window.MouseEvent("mouseleave"));
  assert.equal(doc.getElementById("tltip").style.display, "none");
});

test("the timeline is translated", async () => {
  const { window, doc } = load("fr-FR", { 1: TL() });
  window.render(base({ items: [item({ id: 1 })] }));
  await tick();
  assert.ok(doc.getElementById("tllegend").textContent.includes("4 et plus 1"));
  assert.ok(doc.getElementById("tlbody").textContent.includes("121 images"));
  assert.ok(window.__framesnap.describeAt(TL(), 3 / 121).text.includes("montrée 2 fois"));
});

test("a timeline request that returns nothing is asked once, not forever", async () => {
  const { window, doc, calls } = load("en", {});                  // the backend answers null
  window.render(base({ items: [item({ id: 1 })] }));
  await tick(60);
  assert.equal(calls.filter((c) => c[0] === "get_timeline").length, 1);
  assert.ok(doc.getElementById("tlbody").textContent.includes("analysis failed"));
});

test("a failing timeline request is not repeated either", async () => {
  const { window, doc } = load("en");
  let n = 0;
  window.pywebview.api = { get_timeline: () => { n++; return Promise.reject(new Error("boom")); } };
  window.render(base({ items: [item({ id: 1 })] }));
  await tick(60);
  assert.equal(n, 1);
  assert.ok(doc.getElementById("tlbody").textContent.includes("analysis failed"));
});

test("one held frame is written in the singular", () => {
  const en = load("en"); en.window.render(base({ items: [item({ dups: 1, dup_pct: 0.3 })] }));
  assert.ok(en.doc.querySelector(".meta").textContent.includes("1 held frame (0.3%)") && !en.doc.querySelector(".meta").textContent.includes("frames ("));
  const fr = load("fr-FR"); fr.window.render(base({ items: [item({ dups: 1, dup_pct: 0.3 })] }));
  assert.ok(fr.doc.querySelector(".meta").textContent.includes("1 image copiée (0,3 %)"));
});

test("the timeline keeps its proportions: no stretching of the text", async () => {
  const { window, doc } = load("en", { 1: TL() });
  window.render(base({ items: [item({ id: 1 })] }));
  await tick();
  assert.equal(doc.getElementById("tlsvg").getAttribute("preserveAspectRatio"), "xMinYMin meet");
  const vb = doc.getElementById("tlsvg").getAttribute("viewBox").split(" ").map(Number);
  assert.ok(vb[2] >= 300 && vb[3] === 124);
});

test("the tooltip can be shown from code, like a mouse move", async () => {
  const { window, doc } = load("en", { 1: TL() });
  window.render(base({ items: [item({ id: 1 })] }));
  await tick();
  window.__framesnap.hover(3 / 121);
  assert.equal(doc.getElementById("tltip").style.display, "block");
  assert.ok(doc.getElementById("tltip").textContent.includes("shown twice"));
});
