const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {pathToFileURL} = require("node:url");
const {execFileSync} = require("node:child_process");
const {chromium} = require("playwright");

async function main() {
  const [html, ...fixtures] = process.argv.slice(2);
  assert.ok(html && fixtures.length, "Pass the offline trial and one or more RAW fixtures.");
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), "fieldroom-raw-"));
  const browser = await chromium.launch({executablePath:process.env.FIELDROOM_BROWSER, headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1366,height:768},acceptDownloads:true});
    const network = [], errors = [];
    page.on("request", r => {if (/^https?:/.test(r.url())) network.push(r.url());});
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(pathToFileURL(path.resolve(html)).href);
    await page.waitForFunction(() => !document.querySelector("#photoPlane").hidden);
    for (const filename of fixtures) {
      const name = path.basename(filename), stem = path.parse(name).name;
      await page.locator("#openFolder").click();
      await page.locator("#trialPhotos").setInputFiles(path.resolve(filename));
      await page.waitForFunction(name => document.querySelector("#photoName").textContent===name,name);
      try {
        await page.waitForFunction(() => document.querySelector("#infoCamera").textContent ||
          document.querySelector("#photoMessage").textContent.startsWith("Cannot develop"), null, {timeout:85000});
        assert.match(await page.locator("#infoCamera").textContent(), /^Developed from RAW/);
      } catch(e) {
        throw Error(name + ": " + await page.locator("#photoMessage").textContent() + " / " +
          await page.locator("#infoCamera").textContent() + " / " + await page.locator("#infoCamera").getAttribute("title") +
          " / " + await page.locator("#status").textContent());
      }
      const size = (await page.locator("#infoSize").textContent()).replace(/,/g,"").match(/\d+/g).map(Number);
      assert.ok(size[0]>30 && size[1]>30);
      await page.locator("#zoomSelect").selectOption("200");
      await page.waitForFunction(() => [...document.querySelectorAll("#tileLayer img")].some(i => i.naturalWidth) ||
        document.querySelector("#overviewPhoto").naturalWidth < 128);
      await page.locator("#slider-exposure").fill("0.25");
      await page.locator("#saveAll").click();
      await page.waitForFunction(() => document.querySelector("#resultsDialog").open);
      const saving=page.waitForEvent("download");
      await page.locator("#viewEdited").click();
      const zip = path.join(dest,stem+".zip");
      await (await saving).saveAs(zip);
      const python=process.env.FIELDROOM_PYTHON || "python3";
      execFileSync(python,["-c",[
        "import sys, zipfile, io","from PIL import Image, ImageStat",
        "with zipfile.ZipFile(sys.argv[1]) as z:",
        " with Image.open(io.BytesIO(z.read('Edited/'+sys.argv[2]+'.jpg'))) as im:",
        "  assert im.size == (int(sys.argv[3]),int(sys.argv[4])) and max(ImageStat.Stat(im).extrema)[1] > 2",
        "  assert max(hi-lo for lo,hi in im.getextrema()) > 5",
      ].join("\n"),zip,stem,...size.map(String)]);
      await page.locator("#resultsDialog .close-dialog.primary").click();
      console.log(name + " developed from sensor data at " + size.join(" x ") + ", zoomed and exported.");
    }
    assert.deepEqual(errors,[]); assert.deepEqual(network,[]);
  } finally {
    await browser.close();
    fs.rmSync(dest,{recursive:true,force:true});
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
