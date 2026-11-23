// Real-interface regression: the two sides must stay registered through crop,
// zoom, pan, keyboard interaction and photo changes, with native detail on both.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const python = process.env.FIELDROOM_PYTHON || 'python3';
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'fieldroom-compare-'));
const roll = path.join(temp, 'Compare photos');
let server, browser;

async function run() {
  const fixture = spawnSync(python, ['-c',
    "import runpy,sys; from pathlib import Path; runpy.run_path('tests/test_editor.py')['make_roll'](Path(sys.argv[1]))", roll],
    {cwd:root,encoding:'utf8'});
  if (fixture.status !== 0) throw Error(fixture.stderr || 'Could not make the comparison photos.');
  server = spawn(python, ['-u','app.py','--no-browser',roll], {
    cwd:root,stdio:['ignore','pipe','pipe'],
    env:{...process.env, FIELDROOM_STATE_DIR:path.join(temp,'state')}
  });
  server.stderr.on('data', value => process.stderr.write(value));
  const url = await new Promise((resolve,reject) => {
    let text = '';
    const timer = setTimeout(() => reject(Error('Fieldroom did not start.')),15000);
    server.stdout.on('data',chunk => {
      text += chunk;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#[A-Za-z0-9_-]+[\r\n]/);
      if (match) {clearTimeout(timer);resolve(match[0].trim());}
    });
    server.once('error',error => {clearTimeout(timer);reject(error);});
  });
  browser = await chromium.launch({headless:true,executablePath:process.env.FIELDROOM_BROWSER || undefined,
    args:['--disable-gpu','--disable-crash-reporter','--disable-breakpad','--no-first-run','--disable-background-networking']});
  const page = await browser.newPage({viewport:{width:1440,height:920}});
  page.setDefaultTimeout(20000);
  const errors = [], tileRequests = [];
  page.on('pageerror',error => errors.push(error.message));
  page.on('dialog',dialog => dialog.accept());
  page.on('request',request => {
    if (request.url().endsWith('/api/tile')) tileRequests.push(request.postDataJSON());
  });
  await page.goto(url);
  await page.waitForFunction(() => document.querySelector('#photoName').textContent === '01-resolution.png' &&
    !document.querySelector('#cropTool').disabled);

  // Compare a cropped, edited photograph. The original uses the same crop.
  await page.locator('#cropTool').click();
  await page.waitForSelector('#cropRect',{state:'visible'});
  const plane = await page.locator('#photoPlane').boundingBox();
  await page.mouse.move(plane.x + plane.width*.1,plane.y + plane.height*.1);
  await page.mouse.down();
  await page.mouse.move(plane.x + plane.width*.9,plane.y + plane.height*.85,{steps:5});
  await page.mouse.up();
  await page.locator('#applyCrop').click();
  await page.waitForSelector('#cropPanel',{state:'hidden'});
  await page.locator('#slider-exposure').focus();
  for (let i=0;i<20;i++) await page.keyboard.press('ArrowUp');
  assert.equal(Number(await page.locator('#slider-exposure').inputValue()),1);
  await page.locator('#beforeAfter').click();
  await comparisonReady(page);
  assert.notEqual(await page.locator('#beforeOverview').getAttribute('src'),
    await page.locator('#overviewPhoto').getAttribute('src'),'Before must show the original tones, not a second edited image.');
  await aligned(page);
  const selected = await page.locator('#photoName').innerText();
  const zoom = await page.locator('#zoomSelect').inputValue();
  const divider = page.locator('#comparisonDivider');
  const handle = await divider.boundingBox();
  const view = await page.locator('#viewport').boundingBox();
  const currentPlane = await page.locator('#photoPlane').boundingBox();
  const left = Math.max(view.x,currentPlane.x), right = Math.min(view.x+view.width,currentPlane.x+currentPlane.width);
  await page.mouse.move(handle.x+handle.width/2,handle.y+handle.height/2);
  await page.mouse.down();
  await page.mouse.move(left+(right-left)*.25,handle.y+handle.height/2,{steps:8});
  await page.mouse.up();
  assert(Math.abs(Number(await divider.getAttribute('aria-valuenow'))-25)<=1);
  assert.equal(await page.locator('#zoomSelect').inputValue(),zoom,'Dragging the divider must not toggle zoom.');
  await divider.focus();
  await page.keyboard.press('Home');
  assert.equal(await divider.getAttribute('aria-valuenow'),'0');
  await page.keyboard.press('End');
  assert.equal(await divider.getAttribute('aria-valuenow'),'100');
  await page.keyboard.press('Home');
  await page.keyboard.press('Shift+ArrowRight');
  assert.equal(await divider.getAttribute('aria-valuenow'),'10');
  await page.keyboard.press('ArrowRight');
  assert.equal(await divider.getAttribute('aria-valuenow'),'11');
  assert.equal(await page.locator('#photoName').innerText(),selected,'Divider arrows must not navigate photographs.');
  await page.keyboard.press('Shift+ArrowRight');
  await page.keyboard.press('Shift+ArrowRight');
  await page.keyboard.press('Shift+ArrowRight');

  await page.locator('#zoomSelect').selectOption('100');
  await page.waitForFunction(() => ['#tileLayer img','#beforeTileLayer img'].every(selector =>
    [...document.querySelectorAll(selector)].some(image => image.complete && image.naturalWidth>0)));
  const originalRequests = tileRequests.filter(request => request.recipe.exposure === 0),
    editedRequests = tileRequests.filter(request => request.recipe.exposure === 1);
  assert(originalRequests.length>0 && editedRequests.length>0,'Zoom must render full-resolution detail for both sides.');
  assert.deepEqual(originalRequests.at(-1).recipe.crop,editedRequests.at(-1).recipe.crop);
  assert(originalRequests.at(-1).recipe.crop[0]>.08,'The reference tiles must retain the accepted crop.');
  await aligned(page);
  const beforePan = await page.locator('#photoPlane').boundingBox();
  await page.mouse.move(view.x+view.width*.8,view.y+view.height*.6);
  await page.mouse.down();
  await page.mouse.move(view.x+view.width*.8+60,view.y+view.height*.6+30,{steps:6});
  await page.mouse.up();
  await page.waitForFunction(previous => Math.abs(document.querySelector('#photoPlane').getBoundingClientRect().x-previous)>20,beforePan.x);
  await aligned(page);
  await page.locator('#zoomSelect').selectOption('fit');
  if (process.env.FIELDROOM_COMPARISON_SCREENSHOT) {
    await page.screenshot({path:process.env.FIELDROOM_COMPARISON_SCREENSHOT});
  }

  await page.locator('#cropTool').click();
  await page.waitForSelector('#cropRect',{state:'visible'});
  assert.equal(await page.locator('#comparisonChrome').isVisible(),false,'Comparison is hidden while choosing a new crop.');
  assert.equal(await page.locator('#beforeOverlay').isVisible(),false);
  await page.locator('#cancelCrop').click();
  await comparisonReady(page);
  await page.locator('#nextPhoto').click();
  await page.waitForFunction(() => document.querySelector('#photoName').textContent === '02-portrait.jpg');
  await comparisonReady(page);
  await page.waitForFunction(() => {
    const image = document.querySelector('#beforeOverview');
    return image.naturalHeight>image.naturalWidth;
  });
  await aligned(page);
  await page.locator('#zoomSelect').selectOption('fit');
  await page.locator('#beforeAfter').click();
  assert.equal(await page.locator('#comparisonChrome').isVisible(),false);
  assert.equal(await page.locator('#beforeOverlay').isVisible(),false);
  assert.deepEqual(errors,[]);
  console.log('Passed Before/After divider drag and keyboard, same-crop registration, native tiles on both sides, zoom/pan, crop-mode hiding, navigation and toggle.');
}

async function comparisonReady(page) {
  await page.waitForFunction(() => {
    const chrome=document.querySelector('#comparisonChrome'), before=document.querySelector('#beforeOverview');
    return chrome && !chrome.hidden && before.complete && before.naturalWidth>0;
  });
}
async function aligned(page) {
  const [before,after] = await Promise.all([
    page.locator('#beforeOverview').boundingBox(),page.locator('#overviewPhoto').boundingBox()
  ]);
  assert(before && after);
  for (const key of ['x','y','width','height']) assert(Math.abs(before[key]-after[key])<.1,`Comparison ${key} must match.`);
}
run().catch(error => {console.error(error);process.exitCode=1;}).finally(async () => {
  if (browser) await browser.close();
  if (server) server.kill();
  if (path.dirname(path.resolve(temp))===path.resolve(os.tmpdir()) && path.basename(temp).startsWith('fieldroom-compare-')) {
    fs.rmSync(temp,{recursive:true,force:true,maxRetries:4,retryDelay:200});
  }
});
