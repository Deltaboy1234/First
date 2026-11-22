const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require("playwright");

async function main() {
  const file = path.resolve(process.argv[2] || "Fieldroom-Browser-Trial.html");
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "fieldroom-web-"));
  const python = process.env.FIELDROOM_PYTHON || "python3";
  const sample = path.join(temp, "Test.png");
  let browser;
  try {
    execFileSync(python, ["-c",
      "from PIL import Image; import sys; Image.linear_gradient('L').resize((900, 600)).save(sys.argv[1])", sample]);
    browser = await chromium.launch({executablePath: process.env.FIELDROOM_BROWSER, headless: true});
    const page = await browser.newPage({viewport: {width: 1366, height: 768}, acceptDownloads: true});
    const errors = [], requests = [];
    page.on("pageerror", e => errors.push(e.message));
    page.on("request", r => { if (/^https?:/.test(r.url())) requests.push(r.url()); });
    await page.goto(pathToFileURL(file).href);
    const ready = () => page.waitForFunction(() => !document.querySelector("#photoPlane").hidden &&
      document.querySelector("#overviewPhoto").naturalWidth > 0);
    await ready();
    assert.equal(await page.locator("#filmstrip button").count(), 3);
    await page.locator("#zoomSelect").selectOption("200");
    await page.waitForFunction(() => [...document.querySelectorAll("#tileLayer img")].some(i => i.naturalWidth));
    await page.locator("#viewport").focus();
    await page.keyboard.press("ArrowRight");
    await page.waitForFunction(() => document.querySelector("#photoName").textContent.includes("Collins"));
    await page.keyboard.press("Delete");
    await page.waitForFunction(() => document.querySelectorAll("#filmstrip button").length === 2);
    await page.locator("#undoTrash").click();
    await page.waitForFunction(() => document.querySelectorAll("#filmstrip button").length === 3);
    await page.locator("#openFolder").click();
    await page.locator("#trialPhotos").setInputFiles(sample);
    await page.waitForFunction(() => document.querySelector("#photoName").textContent === "Test.png");
    await ready();
    await page.locator("[data-zoom=fit]").click();
    await page.locator("#slider-exposure").fill("0.5");
    await page.locator("#cropTool").click();
    await page.waitForFunction(() => !document.querySelector("#cropLayer").hidden && !document.querySelector("#photoPlane").hidden);
    const box = await page.locator("#overviewPhoto").boundingBox();
    await page.mouse.move(box.x + .15 * box.width, box.y + .18 * box.height);
    await page.mouse.down();
    await page.mouse.move(box.x + .80 * box.width, box.y + .72 * box.height, {steps: 6});
    await page.mouse.up();
    const crop = (await page.locator("#cropSize").textContent()).match(/\d+/g).map(Number);
    assert.ok(crop[0] < 900 && crop[1] < 600);
    await page.keyboard.press("Enter");
    await ready();
    await page.locator("#saveAll").click();
    await page.waitForFunction(() => document.querySelector("#resultsDialog").open);
    const download = page.waitForEvent("download");
    await page.locator("#viewEdited").click();
    const zip = path.join(temp, "edits.zip");
    await (await download).saveAs(zip);
    execFileSync(python, ["-c", [
      "import io, sys, zipfile", "from PIL import Image",
      "with zipfile.ZipFile(sys.argv[1]) as z:",
      " assert z.testzip() is None and z.namelist() == ['Edited/Test.jpg']",
      " with Image.open(io.BytesIO(z.read(z.namelist()[0]))) as img:",
      "  assert img.format == 'JPEG' and img.size == tuple(map(int, sys.argv[2:]))",
    ].join("\n"), zip, ...crop.map(String)]);
    assert.deepEqual(errors, []);
    assert.deepEqual(requests, []);
    console.log("Portable trial passed offline import, zoom, crop, delete/undo and full-resolution JPEG download.");
  } finally {
    if (browser) await browser.close();
    fs.rmSync(temp, {recursive: true, force: true});
  }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
