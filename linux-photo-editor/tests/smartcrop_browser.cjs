// Real-browser companion to smartcrop_ui.cjs. Controlled replies exercise races.
// Set FIELDROOM_SMARTCROP_IMAGE to OpenCV's public basketball1.png fixture for
// an additional real-inference / Cancel / Apply / export integration scenario.
// Optional FIELDROOM_SMARTCROP_SCREENSHOT saves that public-fixture preview.
// Optional FIELDROOM_GROUPCROP_SCREENSHOT saves the two-person group preview.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const python = process.env.FIELDROOM_PYTHON || 'python3';
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'fieldroom-smart-crop-'));
const roll = path.join(temp, 'Test roll');
let server, browser;
const pendingRouteReleases = new Set();
const subjects = [
  {id:'person-0',label:'Person on left',kind:'person',box:[.1,.1,.5,.9]},
  {id:'person-1',label:'Person on right',kind:'person',box:[.6,.1,.95,.9]},
  {id:'person-2',label:'Person at centre',kind:'person',box:[.45,.2,.6,.8]},
  {id:'all',label:'All people (3)',kind:'group',box:[.1,.1,.95,.9],member_ids:['person-0','person-1','person-2']}
];
const result = (crop, extra = {}) => {
  const value = {status:'ready',crop,subjects,selected_subject:'person-0',aspect:'Free',warning:null,...extra};
  value.selected_subjects ??= value.selected_subject === 'all' ? subjects.filter(s => s.kind !== 'group').map(s => s.id) : [value.selected_subject];
  return value;
};

async function run() {
  const fixture = spawnSync(python, ['-c',
    "import runpy,sys; from pathlib import Path; runpy.run_path('tests/test_editor.py')['make_roll'](Path(sys.argv[1]))", roll
  ], {cwd:root,encoding:'utf8'});
  if (fixture.status !== 0) throw Error(fixture.stderr || 'Could not prepare test photos.');
  server = spawn(python, ['-u','app.py','--no-browser',roll], {cwd:root,stdio:['ignore','pipe','pipe']});
  server.stderr.on('data', chunk => process.stderr.write(chunk));
  const url = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error('Fieldroom did not start.')), 15000);
    server.stdout.on('data', chunk => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#[A-Za-z0-9_-]+[\r\n]/);
      if (match) {clearTimeout(timer);resolve(match[0].trim());}
    });
    server.once('error', error => {clearTimeout(timer);reject(error);});
    server.once('exit', code => {clearTimeout(timer);reject(Error(`Fieldroom exited during startup (${code}).`));});
  });
  browser = await chromium.launch({headless:true,executablePath:process.env.FIELDROOM_BROWSER || undefined});
  const page = await browser.newPage({viewport:{width:1366,height:900}});
  page.setDefaultTimeout(15000);
  const requests = [], errors = [];
  let exports = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if (request.url().endsWith('/api/export')) exports++;});
  page.on('dialog', dialog => dialog.accept());
  await page.route('**/api/smart_crop', route => new Promise(resolve => {
    const finish = () => {pendingRouteReleases.delete(release);resolve();};
    const release = async () => {
      try {await route.abort();} catch {}
      finally {finish();}
    };
    pendingRouteReleases.add(release);
    requests.push({
      data:route.request().postDataJSON(),
      reply:async body => {
        try {await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});}
        catch (error) {if (!/closed|handled|cancel|abort/i.test(error.message)) throw error;}
        finally {finish();}
      }
    });
  }));
  await page.goto(url);
  await page.waitForFunction(() => /3.?200/.test(document.getElementById('infoSize').textContent));
  const waitForRequest = async index => {
    for (let n=0;n<300 && requests.length<=index;n++) await new Promise(resolve => setTimeout(resolve,25));
    assert(requests[index], 'Expected Smart Crop request');
    return requests[index];
  };
  const crop = () => page.locator('#cropRect').getAttribute('style');
  const ready = () => page.waitForFunction(() => !document.getElementById('suggestCrop').disabled);
  const enterCrop = async () => {
    await page.locator('#cropTool').click();
    await ready();
  };

  await enterCrop();
  const before = await crop();
  await page.locator('#suggestCrop').focus();
  await page.keyboard.press('Enter');
  const first = await waitForRequest(0);
  assert.equal(first.data.preset,'tight');
  assert.equal(await page.locator('#applyCrop').isDisabled(),true);
  await page.locator('#viewport').focus();
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#cropPanel').isVisible(),true);
  await page.locator('#saveAll').click();
  assert.equal(exports,0);
  await first.reply(result([.15,.1,.7,.8]));
  await ready();
  assert.notEqual(await crop(),before);
  assert.equal(await page.locator('#filmstrip .thumb.dirty').count(),0);
  assert.equal(await page.locator('#smartCropSubjectField').isVisible(),true);
  await page.locator('#smartCropSubjects input[value="person-0"]').uncheck();
  await page.locator('#smartCropSubjects input[value="person-1"]').check();
  const second = await waitForRequest(1);
  assert.deepEqual(second.data.subject_ids,['person-1']);
  assert(!('subject_id' in second.data));
  await second.reply(result([.55,.15,.95,.85],{selected_subject:'person-1'}));
  await ready();
  const chosen = await crop();

  await page.locator('#suggestCrop').click();
  const missing = await waitForRequest(2);
  await missing.reply({status:'no_subject',crop:null,subjects:[],selected_subject:null,warning:'Subject not detected. Crop unchanged.'});
  await ready();
  assert.equal(await crop(),chosen);
  assert.match(await page.locator('#smartCropNotice').innerText(),/Subject not detected/);

  await page.locator('#suggestCrop').click();
  const cancelled = await waitForRequest(3);
  await page.locator('#cancelCrop').click();
  await cancelled.reply(result([.3,.3,.4,.4]));
  await page.waitForFunction(() => document.getElementById('cropPanel').hidden);
  assert.equal(await page.locator('#filmstrip .thumb.dirty').count(),0);
  await enterCrop();
  assert.equal(await crop(),before);

  await page.locator('#suggestCrop').click();
  const old = await waitForRequest(4);
  await page.locator('#smartCropPreset').selectOption('full-body');
  const latest = await waitForRequest(5);
  await latest.reply(result([.1,.05,.9,.95]));
  await ready();
  const latestCrop = await crop();
  await old.reply(result([.4,.4,.5,.5]));
  assert.equal(await crop(),latestCrop);

  await page.locator('#suggestCrop').click();
  const manual = await waitForRequest(6);
  const rect = await page.locator('#cropRect').boundingBox();
  await page.mouse.move(rect.x+rect.width/2,rect.y+rect.height/2);
  await page.mouse.down();
  await page.mouse.move(rect.x+rect.width/2+12,rect.y+rect.height/2+6,{steps:4});
  await page.mouse.up();
  const adjusted = await crop();
  assert.notEqual(adjusted,latestCrop);
  await manual.reply(result([.4,.4,.5,.5]));
  assert.equal(await crop(),adjusted);
  await page.locator('#applyCrop').click();
  await page.waitForFunction(() => document.getElementById('cropPanel').hidden);
  assert.equal(await page.locator('#filmstrip .thumb.dirty').count(),1);

  await enterCrop();
  assert.equal(await page.locator('#smartCropSubjects input[type="checkbox"]').count(),3);
  await page.locator('#smartCropSubjects input[value="person-1"]').check();
  const subset = await waitForRequest(7);
  assert.deepEqual(subset.data.subject_ids,['person-0','person-1'],'The real checkbox UI did not request exactly the two selected people.');
  await subset.reply(result([.1,.1,.95,.85],{selected_subject:'group',selected_subjects:['person-0','person-1'],method:'group'}));
  await ready();
  assert(await page.locator('#smartCropSubjects input[value="person-0"]').isChecked());
  assert(await page.locator('#smartCropSubjects input[value="person-1"]').isChecked());
  assert.equal(await page.locator('#smartCropSubjects input[value="person-2"]').isChecked(),false);
  assert.equal(await page.locator('#smartCropAll').isChecked(),false);
  assert(await page.locator('#smartCropAll').evaluate(node => node.indeterminate));
  assert.match(await page.locator('#smartCropSelection').innerText(),/2 of 3 selected/);
  assert.match(await page.locator('#smartCropNotice').innerText(),/2 selected people/);
  await page.locator('#cancelCrop').click();
  await page.waitForFunction(() => document.getElementById('cropPanel').hidden);
  await enterCrop();
  assert.equal(await crop(),adjusted,'Cancelling a subset preview changed the accepted manual crop.');
  await page.locator('#cancelCrop').click();
  assert.deepEqual(errors,[]);
  console.log('Passed browser Smart Crop preview, exact two-of-three checkbox selection, no-subject, cancellation, preset race, manual adjustment and explicit Apply.');
  await page.close();
  if (process.env.FIELDROOM_SMARTCROP_IMAGE) await realInference(url);
  else console.log('Real-inference browser scenario skipped: FIELDROOM_SMARTCROP_IMAGE is not set.');
}

async function realInference(url) {
  const image = path.resolve(process.env.FIELDROOM_SMARTCROP_IMAGE);
  assert(fs.existsSync(image), 'FIELDROOM_SMARTCROP_IMAGE must name the downloaded public basketball1.png fixture.');
  const realRoll = path.join(temp,'Real inference');
  fs.mkdirSync(realRoll);
  const filename = 'off-center-subject' + path.extname(image).toLowerCase();
  const original = path.join(realRoll,filename);
  fs.copyFileSync(image,original);
  const hash = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const originalHash = hash(original), sourceHash = hash(image);
  const page = await browser.newPage({viewport:{width:1366,height:900}});
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  // This page has no mocked routes: requests reach the local server and models.
  await page.goto(url);
  await page.waitForFunction(() => !document.getElementById('openFolder').disabled && document.getElementById('infoSize').textContent);
  await page.locator('#openFolder').click();
  await page.locator('#folderPath').fill(realRoll);
  await page.locator('#chooseFolder').click();
  await page.waitForFunction(name => document.getElementById('photoName').textContent === name && !document.getElementById('cropTool').disabled, filename);
  const enterCrop = async () => {
    await page.locator('#cropTool').click();
    await page.waitForFunction(() => !document.getElementById('suggestCrop').disabled);
  };
  const detect = async ({activate = () => page.locator('#suggestCrop').click(), method = 'face'} = {}) => {
    const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/smart_crop') && response.request().method() === 'POST');
    await activate();
    const response = await responsePromise;
    assert(response.ok(), `Smart Crop request failed (${response.status()}).`);
    const suggestion = await response.json();
    await page.waitForFunction(() => !document.getElementById('suggestCrop').disabled);
    assert.equal(suggestion.status,'ready', suggestion.warning || 'Expected a real subject detection.');
    assert.equal(suggestion.method,method);
    assert(Array.isArray(suggestion.crop) && suggestion.crop.length === 4);
    return suggestion;
  };
  const saveAndInspect = async expectedCrop => {
    await page.locator('#saveAll').click();
    await page.waitForSelector('#resultsDialog[open]');
    assert.match(await page.locator('#saveReport').innerText(),/1 saved/);
    const manifest = JSON.parse(fs.readFileSync(path.join(realRoll,'Edited','.fieldroom.json'),'utf8'));
    const saved = manifest.photos[filename];
    assert.deepEqual(saved.recipe.crop,expectedCrop);
    const inspection = spawnSync(python,['-c',
      'import json,sys; from PIL import Image; from fieldroom.imaging import pixel_box; source=Image.open(sys.argv[1]); result=Image.open(sys.argv[2]); box=pixel_box(source.size,json.loads(sys.argv[3])); expected=[box[2]-box[0],box[3]-box[1]]; print(json.dumps({"original":list(source.size),"export":list(result.size),"expected":expected})); assert list(result.size)==expected',
      original,path.join(realRoll,'Edited',saved.output),JSON.stringify(expectedCrop)
    ],{cwd:root,encoding:'utf8'});
    assert.equal(inspection.status,0,inspection.stderr || 'Export dimensions did not match the accepted crop.');
    assert.equal(hash(original),originalHash,'Editing or export modified the original.');
    assert.equal(hash(image),sourceHash,'The external fixture was modified.');
    await page.locator('#resultsDialog .close-dialog').last().click();
    return JSON.parse(inspection.stdout.trim());
  };

  await enterCrop();
  const initialStyle = await page.locator('#cropRect').getAttribute('style');
  const suggestion = await detect();
  const [left,top,right,bottom] = suggestion.crop;
  // In the public basketball fixture the visible frontal face is near x=0.14.
  // A center crop or the turned-away person on the right must fail this check.
  assert((left+right)/2 < .3 && right < .45,'Tight crop did not follow the off-center face on the left.');
  assert(left < .13 && right > .16 && top < .20 && bottom > .27,'The detected face is not enclosed by its portrait crop.');
  const drawn = await page.locator('#cropRect').evaluate(node => [
    parseFloat(node.style.left)/100,parseFloat(node.style.top)/100,
    (parseFloat(node.style.left)+parseFloat(node.style.width))/100,
    (parseFloat(node.style.top)+parseFloat(node.style.height))/100
  ]);
  suggestion.crop.forEach((value,index) => assert(Math.abs(drawn[index]-value)<1e-6,'The UI did not draw the backend crop.'));
  assert.equal(await page.locator('#filmstrip .thumb.dirty').count(),0,'Preview changed the saved recipe before Apply.');
  assert(!fs.existsSync(path.join(realRoll,'Edited')),'Preview unexpectedly exported a photo.');
  const layout = await page.evaluate(() => {
    const frame = document.getElementById('photoPlane').getBoundingClientRect();
    const crop = document.getElementById('cropRect').getBoundingClientRect();
    return {overflow:document.documentElement.scrollWidth > document.documentElement.clientWidth,
      within:crop.left>=frame.left-1 && crop.top>=frame.top-1 && crop.right<=frame.right+1 && crop.bottom<=frame.bottom+1};
  });
  assert.equal(layout.overflow,false,'The desktop layout overflows horizontally.');
  assert(layout.within,'The crop overlay lies outside the displayed original.');
  if (process.env.FIELDROOM_SMARTCROP_SCREENSHOT) {
    const screenshot = path.resolve(process.env.FIELDROOM_SMARTCROP_SCREENSHOT);
    fs.mkdirSync(path.dirname(screenshot),{recursive:true});
    await page.screenshot({path:screenshot});
  }

  await page.locator('#cancelCrop').click();
  await page.waitForFunction(() => document.getElementById('cropPanel').hidden);
  const cancelledExport = await saveAndInspect([0,0,1,1]);
  assert.deepEqual(cancelledExport.export,[640,480],'The public basketball fixture should retain its full size after Cancel.');
  await enterCrop();
  assert.equal(await page.locator('#cropRect').getAttribute('style'),initialStyle,'Cancel changed the next crop session.');
  const accepted = await detect();
  assert.deepEqual(accepted.crop,suggestion.crop,'Cached detection changed the framing unexpectedly.');
  await page.locator('#applyCrop').click();
  await page.waitForFunction(() => document.getElementById('cropPanel').hidden);
  const croppedExport = await saveAndInspect(accepted.crop);
  assert(croppedExport.export[0]<640 && croppedExport.export[1]<480,'Apply did not produce a cropped export.');

  await enterCrop();
  const allPeople = page.locator('#smartCropAll');
  assert.equal(await page.locator('#smartCropSubjects input[type="checkbox"]').count(),2);
  const groupPortrait = await detect({activate:() => allPeople.check(),method:'group'});
  assert.equal(groupPortrait.selected_subject,'all');
  assert(await allPeople.isChecked());
  assert.match(await page.locator('#smartCropNotice').innerText(),/all 2 detected people/);
  assert.equal(await page.locator('#filmstrip .thumb.dirty').count(),0,'A group preview changed the accepted individual crop.');
  const groupBody = await detect({activate:() => page.locator('#smartCropPreset').selectOption('full-body'),method:'group'});
  assert.equal(groupBody.selected_subject,'all','Changing a preset lost the group selection.');
  const groupRatio = await detect({activate:() => page.locator('#aspect').selectOption('4:5'),method:'group'});
  assert.equal(groupRatio.aspect,'Free','An impossible portrait ratio must not cut a person out of the group.');
  assert(await allPeople.isChecked());
  const people = groupRatio.subjects.filter(subject => subject.kind !== 'group');
  assert.equal(people.length,2);
  const containsEveryone = suggestion => {
    for (const person of people) {
      assert(suggestion.crop[0] <= person.box[0] && suggestion.crop[1] <= person.box[1],`Group crop clipped the upper/left extent of ${person.id}.`);
      assert(suggestion.crop[2] >= person.box[2] && suggestion.crop[3] >= person.box[3],`Group crop clipped the lower/right extent of ${person.id}.`);
    }
  };
  containsEveryone(groupBody);
  containsEveryone(groupRatio);
  const individual = await detect({activate:() => page.locator(`#smartCropSubjects input[value="${people[1].id}"]`).uncheck(),method:'person'});
  assert.equal(individual.selected_subject,people[0].id);
  assert(individual.crop[2] < .35,'Switching back to an individual did not isolate the left person.');
  const group = await detect({activate:() => allPeople.check(),method:'group'});
  containsEveryone(group);
  assert(await allPeople.isChecked());
  const groupDrawn = await page.locator('#cropRect').evaluate(node => [
    parseFloat(node.style.left)/100,parseFloat(node.style.top)/100,
    (parseFloat(node.style.left)+parseFloat(node.style.width))/100,
    (parseFloat(node.style.top)+parseFloat(node.style.height))/100
  ]);
  group.crop.forEach((value,index) => assert(Math.abs(groupDrawn[index]-value)<1e-6,'The UI did not draw the group crop.'));
  if (process.env.FIELDROOM_GROUPCROP_SCREENSHOT) {
    const screenshot = path.resolve(process.env.FIELDROOM_GROUPCROP_SCREENSHOT);
    fs.mkdirSync(path.dirname(screenshot),{recursive:true});
    await page.screenshot({path:screenshot});
  }
  await page.locator('#cancelCrop').click();
  await page.waitForFunction(() => document.getElementById('cropPanel').hidden);
  const afterGroupCancel = JSON.parse(fs.readFileSync(path.join(realRoll,'Edited','.fieldroom.json'),'utf8'));
  assert.deepEqual(afterGroupCancel.photos[filename].recipe.crop,accepted.crop,'Cancelling the group preview changed the saved individual crop.');
  await enterCrop();
  const acceptedGroup = await detect({method:'group'});
  assert.deepEqual(acceptedGroup.crop,group.crop);
  await page.locator('#applyCrop').click();
  await page.waitForFunction(() => document.getElementById('cropPanel').hidden);
  const groupExport = await saveAndInspect(acceptedGroup.crop);
  assert(groupExport.export[0] > croppedExport.export[0],'Group export did not widen to include both people.');
  assert.deepEqual(errors,[]);
  console.log(`Passed real offline inference on off-center person, unchanged Cancel export (${cancelledExport.export.join(' × ')}), accepted crop export (${croppedExport.export.join(' × ')}), exact recipe/dimensions and original preservation.`);
  console.log(`Passed All people selection, group preset persistence, aspect fallback, return to individual, group Cancel and accepted two-person export (${groupExport.export.join(' × ')}).`);
  await page.close();
}
run().catch(error => {console.error(error);process.exitCode=1;}).finally(async () => {
  await Promise.allSettled([...pendingRouteReleases].map(release => release()));
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    await new Promise(resolve => {
      const timer = setTimeout(() => {server.kill('SIGKILL');resolve();},3000);
      server.once('exit',() => {clearTimeout(timer);resolve();});
      server.kill();
    });
  }
  fs.rmSync(temp,{recursive:true,force:true,maxRetries:4,retryDelay:200});
});
