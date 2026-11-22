const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  spawn,
  spawnSync
} = require("node:child_process");
const {
  chromium
} = require("playwright");
const root = path.resolve(__dirname, "..");
const python = process.env.FIELDROOM_PYTHON || "python3";
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "fieldroom-ui-"));
const roll = path.join(temp, "Test roll");
let server, browser;
async function run() {
  const fixture = spawnSync(python, ["-c",
    "import runpy,sys; from pathlib import Path; runpy.run_path('tests/test_editor.py')['make_roll'](Path(sys.argv[1]))",
    roll
  ], {
    cwd: root,
    encoding: "utf8"
  });
  if (fixture.status !== 0) throw Error(fixture.stderr || "Could not prepare the disposable roll.");
  server = spawn(python, ["-u", "app.py", "--no-browser", roll], {
    cwd: root,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"]
  });
  server.stderr.on("data", d => process.stderr.write(d));
  const url = await new Promise((resolve, reject) => {
    let buffer = "";
    const timer = setTimeout(() => reject(Error("Fieldroom did not start.")), 15000);
    server.stdout.on("data", chunk => {
      buffer += chunk;
      const m = buffer.match(/http:\/\/127\.0\.0\.1:\d+\/#[A-Za-z0-9_-]+[\r\n]/);
      if (m) {
        clearTimeout(timer);
        resolve(m[0].trim());
      }
    });
    server.once("error", error => {
      clearTimeout(timer);
      reject(error);
    });
  });
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.FIELDROOM_BROWSER || undefined,
    args: ["--disable-gpu", "--disable-crash-reporter", "--disable-breakpad", "--no-first-run",
      "--disable-background-networking"
    ]
  });
  for (const dpr of [1, 2]) {
    const context = await browser.newContext({
      viewport: {
        width: 1366,
        height: 850
      },
      deviceScaleFactor: dpr
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    page.on("dialog", d => d.accept());
    await page.goto(url);
    const sel = page.locator("#zoomSelect"),
      plane = page.locator("#photoPlane"),
      vp = page.locator("#viewport");
    await page.waitForFunction(() => document.querySelector("#infoSize").textContent.includes("3,200") ||
      document.querySelector("#infoSize").textContent.includes("3200"));
    const fitted = await plane.boundingBox();
    assert(fitted.width < 1000);
    const photo = await vp.boundingBox();
    const anchor = {
      x: photo.x + photo.width * .57,
      y: photo.y + photo.height * .45
    };
    await page.mouse.click(anchor.x, anchor.y);
    assert.equal(await sel.inputValue(), "100");
    await page.waitForFunction(() => [...document.querySelectorAll("#tileLayer img")].some(i => i
      .complete && i.naturalWidth > 0));
    const native = await plane.boundingBox();
    assert(Math.abs(native.width * dpr - 3200) < 2);
    const sourceX = (anchor.x - native.x) / native.width;
    assert(Math.abs(sourceX - (anchor.x - fitted.x) / fitted.width) < .003);
    const nativeCount = await page.locator("#tileLayer img").count();
    assert(nativeCount > 0);
    await page.mouse.move(anchor.x, anchor.y);
    await page.mouse.wheel(0, -120);
    await page.waitForFunction(() => document.querySelector("#zoomSelect").value === "150");
    const afterWheel = await plane.boundingBox();
    assert(Math.abs((anchor.x - afterWheel.x) / afterWheel.width - sourceX) < .003);
    await page.mouse.down();
    await page.mouse.move(anchor.x + 72, anchor.y + 36, {
      steps: 6
    });
    await page.mouse.up();
    await page.waitForFunction(x => Math.abs(document.querySelector("#photoPlane").getBoundingClientRect()
      .x - x) > 40, afterWheel.x);
    const panned = await plane.boundingBox();
    assert.equal(await sel.inputValue(), "150");
    const nav = await page.locator("#navWrap").boundingBox();
    await page.mouse.click(nav.x + nav.width * .26, nav.y + nav.height * .34);
    await page.waitForFunction(x => Math.abs(document.querySelector("#photoPlane").getBoundingClientRect()
      .x - x) > 50, panned.x);
    await vp.focus();
    await page.keyboard.press("Space");
    assert.equal(await sel.inputValue(), "fit");
    await page.keyboard.press("Space");
    assert.equal(await sel.inputValue(), "150");
    await page.keyboard.press("Control+0");
    assert.equal(await sel.inputValue(), "fit");
    await page.locator('[data-zoom="fill"]').click();
    assert.equal(await sel.inputValue(), "fill");
    await page.keyboard.press("Control+=");
    assert.notEqual(await sel.inputValue(), "fill");
    await page.keyboard.press("Space");
    assert.equal(await sel.inputValue(), "fill");
    await page.locator('[data-zoom="fit"]').click();
    await page.keyboard.press("r");
    await page.waitForSelector("#cropRect", {
      state: "visible"
    });
    const orig = await plane.boundingBox();
    await page.mouse.move(orig.x + orig.width * .15, orig.y + orig.height * .16);
    await page.mouse.down();
    await page.mouse.move(orig.x + orig.width * .8, orig.y + orig.height * .74, {
      steps: 9
    });
    await page.mouse.up();
    const crop = await page.locator("#cropSize").innerText();
    assert(!crop.includes("3,200"));
    await vp.focus();
    await page.keyboard.press("Enter");
    await page.waitForSelector("#cropPanel", {
      state: "hidden"
    });
    await page.keyboard.press("ArrowRight");
    await page.waitForFunction(() => document.querySelector("#infoSize").textContent === "610 × 940 px");
    await page.keyboard.press("ArrowLeft");
    await page.waitForFunction(() => document.querySelector("#infoSize").textContent.includes("200"));
    await page.keyboard.press("r");
    await page.waitForSelector("#cropRect", {
      state: "visible"
    });
    assert.equal(await page.locator("#cropSize").innerText(), crop);
    await page.keyboard.press("Escape");
    if (dpr === 1) {
      await page.locator("#slider-exposure").focus();
      for (let i = 0; i < 10; i++) await page.keyboard.press("ArrowUp");
      await page.locator("#nextPhoto").click();
      const second = path.join(roll, "02-portrait.jpg");
      await vp.focus();
      await page.keyboard.press("Delete");
      await page.waitForSelector("#undoTrash", {
        state: "visible"
      });
      assert(!fs.existsSync(second));
      await page.locator("#undoTrash").click();
      await page.waitForFunction(() => document.querySelector("#photoName").textContent ===
        "02-portrait.jpg");
      assert(fs.existsSync(second));
      await page.locator("#saveAll").click();
      await page.waitForSelector("#resultsDialog[open]");
      assert.match(await page.locator("#saveReport").innerText(), /2 saved/);
      const edited = JSON.parse(fs.readFileSync(path.join(roll, "Edited", ".fieldroom.json"), "utf8"))
        .photos;
      assert.equal(edited["01-resolution.png"].recipe.exposure, .5);
      assert(edited["01-resolution.png"].recipe.crop[0] > .13);
      await page.locator("#resultsDialog .close-dialog").last().click();
      await page.locator("#openFolder").click();
      await page.locator("#folderPath").fill(path.join(roll, "second folder"));
      await page.locator("#chooseFolder").click();
      await page.waitForFunction(() => document.querySelector("#photoName").textContent === "Other.jpg");
    }
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`Passed ${dpr}× display zoom, pan, Navigator, crop and navigation`);
    if (dpr === 1) {
      // Use fresh originals for the next display scale.
      const output = path.resolve(roll, "Edited");
      assert.equal(path.dirname(output), path.resolve(roll));
      fs.rmSync(output, {
        recursive: true
      });
    }
  }
}
run().catch(e => {
  console.error(e);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server) server.kill();
  if (path.dirname(path.resolve(temp)) === path.resolve(os.tmpdir()) && path.basename(temp).startsWith(
      "fieldroom-ui-")) {
    try {
      fs.rmSync(temp, {
        recursive: true,
        force: true,
        maxRetries: 4,
        retryDelay: 200
      });
    } catch (e) {
      console.warn(e.message);
    }
  }
});
