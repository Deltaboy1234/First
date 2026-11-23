// Exercise accepted edit history and durable recovery through the actual UI.
// Auto-adjustment/AI replies are controlled here; model inference has separate tests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const python = process.env.FIELDROOM_PYTHON || 'python3';
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'fieldroom-editing-'));
const roll = path.join(temp, 'Test roll');
const other = path.join(roll, 'second folder');
const tinyRoll = path.join(temp, 'Tiny AI roll');
const state = path.join(temp, 'App state');
const pending = new Set();
let server, browser;
const hash = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');

async function withTimeout(promise, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error(message)), 10000);
    })]);
  } finally {clearTimeout(timer);}
}

async function startServer(folder) {
  server = spawn(python, ['-u', 'app.py', '--no-browser', ...(folder ? [folder] : [])], {
    cwd: root, env: {...process.env, FIELDROOM_STATE_DIR: state,
      FIELDROOM_AI_CACHE_DIR: path.join(temp, 'AI cache')}, stdio: ['ignore', 'pipe', 'pipe']
  });
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  return new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error('Fieldroom did not start.')), 15000);
    server.stdout.on('data', chunk => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#[A-Za-z0-9_-]+[\r\n]/);
      if (match) {clearTimeout(timer); resolve(match[0].trim());}
    });
    server.once('error', error => {clearTimeout(timer); reject(error);});
    server.once('exit', code => {clearTimeout(timer); reject(Error(`Fieldroom exited during startup (${code}).`));});
  });
}

async function stopServer() {
  const child = server;
  server = null;
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise(resolve => {
    const timer = setTimeout(() => child.kill('SIGKILL'), 4000);
    child.once('exit', () => {clearTimeout(timer); resolve();});
    child.kill('SIGTERM');
  });
}

async function newPage(url, errors) {
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  const requests = {tone: [], color: [], exports: 0};
  const key = new URL(url).hash.slice(1);
  let currentFolder, capabilities;
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  page.on('request', request => {
    if (request.url().endsWith('/api/folder')) currentFolder = request.postDataJSON().path;
    if (request.url().endsWith('/api/export')) requests.exports++;
  });
  await page.route('**/api/boot', async route => {
    const response = await route.fetch();
    const data = await response.json();
    currentFolder = data.folder.path;
    capabilities = data.ai;
    data.ai = {available: true, denoise: true, sharpen: true, backend: 'CPU', message: 'Offline AI detail available.'};
    await route.fulfill({status: response.status(), contentType: 'application/json', body: JSON.stringify(data)});
  });
  for (const [kind, recipe] of [
    ['tone', {exposure: .8, contrast: 12, highlights: -20, shadows: 18, whites: 5, blacks: -3}],
    ['color', {warmth: 10, tint: -4, vibrance: 18, saturation: 3}]
  ]) await page.route(`**/api/auto_${kind}`, async route => {
    requests[kind].push(route.request().postDataJSON());
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      recipe, method: 'selected_subjects', selected_subjects: ['person-0'], warning: null,
      message: `Auto ${kind} balanced the selected subject.`
    })});
  });
  await page.goto(url);
  await page.waitForFunction(() => document.querySelector('#infoSize').textContent &&
    !document.querySelector('#cropTool').disabled);
  const snapshot = async () => {
    assert.equal(await page.evaluate(() => globalThis.fieldroomPrepareClose()), true,
      'Accepted edits must be acknowledged before the desktop window can close.');
    const identifier = await page.locator('#filmstrip .thumb[aria-selected="true"]').getAttribute('data-id');
    assert(currentFolder, 'Expected the current folder from the actual boot/folder request.');
    const response = await page.request.post(new URL('/api/folder', url).href, {
      data: {path: currentFolder}, headers: {'X-Fieldroom-Key': key, Origin: new URL(url).origin}
    });
    assert(response.ok(), `Could not read durable accepted settings: ${await response.text()}`);
    const folder = await response.json();
    const row = folder.photos.find(item => item.id === identifier);
    assert(row, 'The current photo must exist in the durable folder response.');
    return structuredClone(row.recipe);
  };
  return {context, page, requests, snapshot, capabilities};
}

async function openFolder(page, folder, expected) {
  await page.locator('#openFolder').click();
  await page.locator('#folderPath').fill(folder);
  await page.locator('#chooseFolder').click();
  await page.waitForFunction(name => document.querySelector('#photoName').textContent === name &&
    !document.querySelector('#cropTool').disabled, expected);
}

async function openContainingPanel(locator) {
  const details = locator.locator('xpath=ancestor::details').first();
  if (await details.count() && !await details.evaluate(node => node.open))
    await details.locator('summary').first().click();
  await locator.scrollIntoViewIfNeeded();
}

async function setRangeByKeyboard(page, selector, value) {
  const input = page.locator(selector);
  await openContainingPanel(input);
  const {min, step} = await input.evaluate(node => ({min: Number(node.min), step: Number(node.step || 1)}));
  await input.focus();
  await page.keyboard.press('Home');
  const steps = Math.round((value - min) / step);
  for (let index = 0; index < steps; index++) await page.keyboard.press('ArrowUp');
  assert.equal(Number(await input.inputValue()), value);
}

async function aiCancellation(fixture) {
  const {page, snapshot} = fixture;
  const before = await snapshot();
  const undoLabel = await page.locator('#undoEdit').getAttribute('title');
  let number = 0, job, cancelled = false, lateReply;
  let progressSeen;
  const firstProgress = new Promise(resolve => {progressSeen = resolve;});
  const unexpectedPreviews = [];
  await page.route('**/api/preview', async route => {
    const settings = route.request().postDataJSON().recipe;
    if (settings.ai_denoise || settings.ai_sharpen) {
      unexpectedPreviews.push(settings);
      await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({error: 'Cancelled AI must not be applied.'})});
    } else await route.continue();
  });
  await page.route('**/api/ai_prepare', async route => {
    number++;
    job = `controlled-ai-${number}`;
    cancelled = false;
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({job, state: 'queued'})});
  });
  await page.route('**/api/ai_cancel', async route => {
    cancelled = true;
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      job: route.request().postDataJSON().job, state: 'cancelled', progress: .2,
      cancel_requested: true, message: 'AI enhancement cancelled. Your edits are unchanged.'
    })});
  });
  await page.route('**/api/ai_progress', route => {
    const identifier = route.request().postDataJSON().job;
    if (identifier === 'controlled-ai-1' && !lateReply) return new Promise(resolve => {
      const abort = async () => {try {await route.abort();} catch {} finally {pending.delete(abort); resolve();}};
      pending.add(abort);
      lateReply = async () => {
        try {
          await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
            job: identifier, state: 'ready', progress: 1, message: 'Late result from cancelled work.'
          })});
        } catch (error) {if (!/closed|handled|cancel|abort/i.test(error.message)) throw error;}
        finally {pending.delete(abort); resolve();}
      };
      progressSeen();
    });
    const body = identifier === 'controlled-ai-1' ? {
      job: identifier, state: cancelled ? 'cancelled' : 'running', progress: .2,
      cancel_requested: cancelled, message: cancelled ? 'Cancelled.' : 'Processing detail tiles.'
    } : {job: identifier, state: 'error', progress: 0, error: 'The local AI model is unavailable.', message: 'The local AI model is unavailable.'};
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
  });
  await setRangeByKeyboard(page, '#aiDenoiseSlider', 25);
  await setRangeByKeyboard(page, '#aiSharpenSlider', 0);
  await page.locator('#applyAiDetail').click();
  await withTimeout(firstProgress, 'No AI progress request.');
  assert.equal(await page.evaluate(() => globalThis.fieldroomPrepareClose()), false,
    'The native window must remain open while AI work is in progress.');
  await page.locator('#cancelAiDetail').click();
  await lateReply();
  await page.waitForFunction(() => !document.querySelector('#applyAiDetail').disabled && document.querySelector('#cancelAiDetail').hidden);
  assert.deepEqual(await snapshot(), before, 'A cancelled or late AI result changed accepted edits.');
  assert.equal(await page.locator('#undoEdit').getAttribute('title'), undoLabel);

  await page.locator('#applyAiDetail').click();
  await page.waitForFunction(() => /model is unavailable/.test(document.querySelector('#status').textContent) &&
    !document.querySelector('#applyAiDetail').disabled);
  assert.deepEqual(await snapshot(), before, 'A missing AI model changed accepted edits.');
  assert.deepEqual(unexpectedPreviews, [], 'Cancelled/failed AI must never reach image rendering.');
  assert.equal(await page.locator('#undoEdit').getAttribute('title'), undoLabel);
  console.log('Passed AI cancellation, late-result rejection, model-error preservation and close blocking.');
}

async function realTinyAi(fixture) {
  const {page, snapshot, capabilities} = fixture;
  assert(capabilities?.denoise && capabilities?.sharpen,
    'The Linux installer must provide both real offline AI models for this integration case.');
  await openFolder(page, tinyRoll, 'Noisy.png');
  const source = path.join(tinyRoll, 'Noisy.png'), originalHash = hash(source);
  const before = await snapshot();
  const originalPreview = await page.locator('#overviewPhoto').getAttribute('src');
  await setRangeByKeyboard(page, '#aiDenoiseSlider', 35);
  await setRangeByKeyboard(page, '#aiSharpenSlider', 20);
  const aiPreview = () => page.waitForResponse(response => {
    if (!response.url().endsWith('/api/preview')) return false;
    const settings = response.request().postDataJSON().recipe;
    return settings.ai_denoise === 35 && settings.ai_sharpen === 20;
  }, {timeout: 60000});
  const [preview] = await Promise.all([aiPreview(), page.locator('#applyAiDetail').click()]);
  assert(preview.ok(), `Real AI preparation did not produce a viewable result: ${await preview.text()}`);
  const data = await preview.json(), enhancedPreview = 'data:image/jpeg;base64,' + data.image;
  assert.deepEqual(data.pixels, [96, 64]);
  await page.waitForFunction(url => document.querySelector('#overviewPhoto').src === url, enhancedPreview);
  await page.waitForFunction(() => document.querySelector('#cancelAiDetail').hidden &&
    /included in this edit/.test(document.querySelector('#aiNotice').textContent));
  const accepted = await snapshot();
  assert.equal(accepted.ai_denoise, 35);
  assert.equal(accepted.ai_sharpen, 20);
  await page.locator('#undoEdit').click();
  assert.deepEqual(await snapshot(), before, 'Undo did not remove genuine AI detail as one accepted edit.');
  await page.waitForFunction(url => document.querySelector('#overviewPhoto').src === url, originalPreview);
  await page.locator('#redoEdit').click();
  await page.waitForFunction(url => document.querySelector('#overviewPhoto').src === url, enhancedPreview);
  assert.deepEqual(await snapshot(), accepted);
  await page.locator('#undoEdit').click();
  assert.deepEqual(await snapshot(), before);
  assert.equal(hash(source), originalHash);
  assert.equal(fs.existsSync(path.join(tinyRoll, 'Edited')), false);
  console.log('Passed genuine offline AI denoise+sharpen Apply, cached preview and Undo/Redo on a tiny photo without changing its dimensions or original.');
}

async function run() {
  const made = spawnSync(python, ['-c',
    "import runpy,sys; from pathlib import Path; import numpy as np; from PIL import Image; " +
    "runpy.run_path('tests/test_editor.py')['make_roll'](Path(sys.argv[1])); " +
    "tiny=Path(sys.argv[2]); tiny.mkdir(); rng=np.random.default_rng(19); " +
    "rgb=np.clip(120+rng.normal(0,12,(64,96,3)),0,255).astype(np.uint8); Image.fromarray(rgb).save(tiny/'Noisy.png')", roll, tinyRoll
  ], {cwd: root, encoding: 'utf8'});
  if (made.status !== 0) throw Error(made.stderr || 'Could not create the disposable photographs.');
  fs.copyFileSync(path.join(other, 'Other.jpg'), path.join(other, 'Z-last.jpg'));
  const sources = [path.join(roll, '01-resolution.png'), path.join(other, 'Z-last.jpg')];
  const originalHashes = sources.map(hash);
  let url = await startServer(roll);
  browser = await chromium.launch({headless: true, executablePath: process.env.FIELDROOM_BROWSER || undefined});
  const errors = [];
  let fixture = await newPage(url, errors);
  let {page, snapshot} = fixture;
  assert.equal(await page.locator('#panel-basic').evaluate(node => node.nextElementSibling.id), 'panel-color');

  const exposure = page.locator('#slider-exposure');
  const range = await exposure.boundingBox();
  await page.mouse.move(range.x + range.width * .5, range.y + range.height / 2);
  await page.mouse.down();
  await page.mouse.move(range.x + range.width * .72, range.y + range.height / 2, {steps: 10});
  await page.mouse.up();
  const dragged = Number(await exposure.inputValue());
  assert(dragged > .5);
  await page.locator('#undoEdit').click();
  assert.equal(Number(await exposure.inputValue()), 0);
  assert.equal(await page.locator('#undoEdit').isDisabled(), true, 'One slider drag must create one Undo step.');
  await page.locator('#redoEdit').click();
  assert.equal(Number(await exposure.inputValue()), dragged);

  await page.locator('#cropTool').click();
  await page.waitForSelector('#cropRect', {state: 'visible'});
  const plane = await page.locator('#photoPlane').boundingBox();
  await page.mouse.move(plane.x + plane.width * .13, plane.y + plane.height * .14);
  await page.mouse.down();
  await page.mouse.move(plane.x + plane.width * .83, plane.y + plane.height * .81, {steps: 5});
  await page.mouse.up();
  assert.equal(await page.evaluate(() => globalThis.fieldroomPrepareClose()), false, 'Unaccepted crops must keep the window open.');
  await page.locator('#applyCrop').click();
  await page.waitForSelector('#cropPanel', {state: 'hidden'});
  const beforeAuto = await snapshot();
  assert(beforeAuto.crop[0] > .05 && beforeAuto.crop[2] < .95);
  await page.locator('#autoTone').click();
  await page.waitForFunction(() => document.querySelector('#slider-exposure').value === '0.8');
  const afterTone = await snapshot();
  assert.deepEqual(afterTone.crop, beforeAuto.crop);
  assert.deepEqual(fixture.requests.tone[0].crop, beforeAuto.crop);
  await page.locator('#autoColor').click();
  await page.waitForFunction(() => document.querySelector('#slider-tint').value === '-4');
  const afterColor = await snapshot();
  assert.deepEqual(afterColor.crop, beforeAuto.crop);
  assert.equal(afterColor.exposure, afterTone.exposure);
  assert.deepEqual(fixture.requests.color[0].crop, beforeAuto.crop);
  await page.locator('#undoEdit').click();
  assert.deepEqual(await snapshot(), afterTone);
  await page.locator('#undoEdit').click();
  assert.deepEqual(await snapshot(), beforeAuto);
  await page.locator('#redoEdit').click();
  await page.locator('#redoEdit').click();
  assert.deepEqual(await snapshot(), afterColor);
  console.log('Passed single-gesture Undo/Redo and reversible Auto Tone/Color with accepted crop preservation.');

  await aiCancellation(fixture);
  await openFolder(page, other, 'Other.jpg');
  await page.locator('#filmstrip .thumb').last().click();
  await page.waitForFunction(() => document.querySelector('#photoName').textContent === 'Z-last.jpg');
  await setRangeByKeyboard(page, '#slider-exposure', .3);
  await snapshot();

  // Hold the actual save request: native close must wait for durable acknowledgement.
  let saveSeen, releaseSave, rejectRecovery = true;
  const heldSave = new Promise(resolve => {saveSeen = resolve;});
  await page.route('**/api/recovery', route => {
    if (rejectRecovery) return route.fulfill({status: 507, contentType: 'application/json',
      body: JSON.stringify({error: 'The recovery disk is temporarily full.'})});
    return new Promise(resolve => {
      const abort = async () => {try {await route.abort();} catch {} finally {pending.delete(abort); resolve();}};
      pending.add(abort);
      releaseSave = async () => {try {await route.continue();} finally {pending.delete(abort); resolve();}};
      saveSeen();
    });
  });
  await exposure.focus();
  await page.keyboard.press('ArrowUp');
  const rejectedClose = await page.evaluate(async () => {
    try {return {closed: await globalThis.fieldroomPrepareClose()};}
    catch (error) {return {error: error.message};}
  });
  assert(rejectedClose.error, 'A failed recovery write must reject the native close request.');
  assert.match(await page.locator('#recoveryStatus').innerText(), /not saved/);
  rejectRecovery = false;
  let closeResolved = false;
  const closePromise = page.evaluate(() => globalThis.fieldroomPrepareClose()).then(value => {closeResolved = true; return value;});
  await withTimeout(heldSave, 'No close-time recovery request.');
  assert.equal(closeResolved, false, 'The desktop close promise resolved before the save was acknowledged.');
  await releaseSave();
  assert.equal(await closePromise, true);
  const selectedId = await page.locator('#filmstrip .thumb[aria-selected="true"]').getAttribute('data-id');
  const lastRecipe = await snapshot();
  assert.equal(fixture.requests.exports, 0);
  assert.equal(fs.existsSync(path.join(roll, 'Edited')), false);
  assert.equal(fs.existsSync(path.join(other, 'Edited')), false);
  await fixture.context.close();
  await stopServer();

  // No folder argument and no browser session storage: only the disk state can restore this.
  url = await startServer();
  fixture = await newPage(url, errors);
  ({page, snapshot} = fixture);
  await page.waitForFunction(() => document.querySelector('#photoName').textContent === 'Z-last.jpg');
  assert.equal(await page.locator('#filmstrip .thumb[aria-selected="true"]').getAttribute('data-id'), selectedId);
  assert.deepEqual(await snapshot(), lastRecipe);
  assert.equal(await page.locator('#filmstrip .thumb.dirty').count(), 1);
  await openFolder(page, roll, '01-resolution.png');
  assert.deepEqual(await snapshot(), afterColor, 'An earlier folder lost its accepted edits across the process restart.');
  assert.deepEqual(sources.map(hash), originalHashes);
  assert.equal(fixture.requests.exports, 0);
  console.log('Passed failed-save close blocking, retry acknowledgement and fresh-process/fresh-context recovery of last folder, selected photo and accepted edits without exporting or changing originals.');
  await realTinyAi(fixture);
  assert.deepEqual(errors, []);
  assert.equal(fixture.requests.exports, 0);
  await fixture.context.close();
}

run().catch(error => {console.error(error); process.exitCode = 1;}).finally(async () => {
  for (const release of [...pending]) await release();
  await browser?.close();
  await stopServer();
  fs.rmSync(temp, {recursive: true, force: true});
});
