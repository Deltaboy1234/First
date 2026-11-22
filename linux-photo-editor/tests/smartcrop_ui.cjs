// Deterministic controller regression tests. Runs the actual editor and viewport
// modules with a minimal DOM and deliberately out-of-order API completions.
// This verifies state transitions, not browser layout or neural-network accuracy.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const web = path.resolve(__dirname, "../fieldroom/web");

function harness({trial = false} = {}) {
  const nodes = new Map(), handlers = new Map(), requests = [];
  class Element {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase(); this.children = []; this.options = [];
      this.style = {}; this.dataset = {}; this.attributes = {}; this.events = new Map();
      this.hidden = false; this.disabled = false; this.value = ""; this.textContent = "";
      this.clientWidth = 1000; this.clientHeight = 700;
      const classes = new Set();
      this.classList = {
        toggle(name, force) { const on = force ?? !classes.has(name); on ? classes.add(name) : classes.delete(name); return on; },
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        contains: name => classes.has(name)
      };
    }
    set id(value) { this._id = value; nodes.set(value, this); }
    get id() { return this._id; }
    setAttribute(name, value) { this.attributes[name] = value; }
    getAttribute(name) { return this.attributes[name]; }
    append(...items) { this.children.push(...items); }
    prepend(...items) { this.children.unshift(...items); }
    add(item) { this.options.push(item); }
    replaceChildren(...items) { this.children = items; this.options = []; }
    insertBefore(item) { this.children.unshift(item); }
    querySelector() { return this.children[0] || new Element(); }
    addEventListener(name, callback) { this.events.set(name, callback); }
    focus() {}
    scrollIntoView() {}
    setPointerCapture() {}
    hasPointerCapture() { return false; }
    matches(selector) {
      return selector.split(',').some(part => {
        const tag = part.trim();
        if (tag === 'input[type=checkbox]') return this.tagName === 'INPUT' && this.type === 'checkbox';
        if (tag.startsWith('input:not(')) return this.tagName === 'INPUT' && this.type !== 'range' &&
          (!tag.includes('[type=checkbox]') || this.type !== 'checkbox');
        return tag.toUpperCase() === this.tagName;
      });
    }
    closest() { return null; }
    getBoundingClientRect() { return {left: 0, top: 0, width: this.clientWidth, height: this.clientHeight}; }
    getContext() { return new Proxy({}, {get: () => () => {}}); }
    remove() {}
  }
  const shell = fs.readFileSync(path.join(web, "index.html"), "utf8");
  for (const [, id] of shell.matchAll(/id="([^"]+)"/g)) new Element().id = id;
  for (const aspect of ["Free", "Original", "1:1", "3:2", "2:3", "4:5", "5:4", "16:9"])
    nodes.get("aspect").add({value: aspect});
  nodes.get("aspect").value = "Free";
  const body = new Element(), workspace = new Element(), photoActions = new Element();
  const document = {
    body,
    getElementById: id => nodes.get(id) || null,
    createElement: tag => new Element(tag),
    createElementNS: (_, tag) => new Element(tag),
    createDocumentFragment: () => new Element(),
    querySelector: selector => selector === ".workspace" ? workspace : selector === ".photo-actions" ? photoActions : null,
    querySelectorAll: () => [],
    addEventListener: (name, callback) => handlers.set(name, callback)
  };
  const storage = new Map();
  const context = vm.createContext({
    document, console, structuredClone, AbortController, Blob, URL,
    location: {hash: "#test-key", pathname: "/"}, history: {replaceState() {}},
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    navigator: {sendBeacon() {}},
    window: {addEventListener() {}, dispatchEvent() {}, devicePixelRatio: 1},
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, requestAnimationFrame: () => 1,
    ResizeObserver: class { observe() {} }, IntersectionObserver: class { observe() {} disconnect() {} },
    Image: class extends Element {},
    Option: function(label, value) { return {text: label, value}; },
    fetch: (url, options) => {
      const data = JSON.parse(options.body);
      if (url !== "/api/smart_crop") {
        requests.push({url, data});
        return Promise.resolve({ok: true, json: async () => ({}), blob: async () => new Blob()});
      }
      return new Promise((resolve, reject) => requests.push({
        url, data, signal: options.signal,
        complete: result => resolve({ok: true, json: async () => result}),
        fail: message => reject(Error(message))
      }));
    },
    ...(trial ? {fieldroomTrial: {request() { throw Error("Trial should never run AI inference."); }}} : {})
  });
  const viewportSource = fs.readFileSync(path.join(web, "viewport.js"), "utf8").replace(/export\s*\{[\s\S]*?\};\s*$/, "");
  let editorSource = fs.readFileSync(path.join(web, "editor.js"), "utf8").replace(/^import[\s\S]*?from "\.\/viewport\.js";\s*/, "");
  editorSource = editorSource.slice(0, editorSource.lastIndexOf("boot().catch("));
  vm.runInContext(viewportSource + "\n" + editorSource + `
    defaults = {exposure:0,contrast:0,highlights:0,shadows:0,whites:0,blacks:0,warmth:0,saturation:0,luminance:0,color:0,sharpen:0,radius:1,crop:[0,0,1,1]};
    folder = {path:"/photos",name:"photos",parent:"/",folders:[],photos:[{id:"one",name:"one.jpg"},{id:"two",name:"two.jpg"}]};
    for (const item of folder.photos) records.set(item.id,{...item,folder:folder.path,recipe:copy(defaults),saved:copy(defaults)});
    selected = "one";
    createAdjustments();
    viewport.box = [0,0,1000,800]; viewport.size = [1000,800]; viewport.draft = [.1,.1,.9,.9];
    syncSmartCropControls();
    globalThis.test = {viewport,smartCrop,suggestSmartCrop,finishCrop,startCrop,saveAll,selectPhoto,applyFolder,active,
      loaded() {viewport.box=[0,0,1000,800];viewport.size=[1000,800];syncSmartCropControls();}};
  `, context, {filename: "fieldroom-controller-under-test.js"});
  return {test: context.test, nodes, requests, handlers};
}
const plain = value => JSON.parse(JSON.stringify(value));
const ready = (crop, extra = {}) => {
  const result = {status:"ready",crop,aspect:"Free",subjects:[{id:"person-0",label:"Person on left",kind:"person",box:[.1,.1,.5,.9]}],selected_subject:"person-0",warning:null,...extra};
  result.selected_subjects ??= result.selected_subject === "all" ? result.subjects.filter(s => s.kind !== "group").map(s => s.id) :
    result.selected_subject ? [result.selected_subject] : [];
  return result;
};
const groupSubjects = [
  {id:"person-0",label:"Person 1 (left)",kind:"person",box:[.1,.1,.4,.9]},
  {id:"person-1",label:"Person 2 (right)",kind:"person",box:[.6,.1,.9,.9]},
  {id:"all",label:"All people (2)",kind:"group",box:[.1,.1,.9,.9],member_ids:["person-0","person-1"]}
];
const deferred = h => h.requests.filter(r => r.url === "/api/smart_crop").at(-1);
const togglePerson = (h, id, checked) => {
  const input = h.test.smartCrop.inputs.get(id);
  assert(input, `Missing checkbox for ${id}`);
  input.checked = checked;
  return input.onchange();
};
const toggleAll = (h, checked = true) => {
  const input = h.nodes.get("smartCropAll");
  input.checked = checked;
  return input.onchange();
};

async function run() {
  let passed = 0;
  async function check(name, test) { await test(); passed++; console.log("Passed " + name); }

  await check("preview is draft-only until explicit Apply", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop();
    assert(h.nodes.get("applyCrop").disabled);
    assert.equal(deferred(h).data.preset,"tight");
    deferred(h).complete(ready([.3,.1,.7,.6])); await promise;
    assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
    assert.deepEqual(plain(h.test.viewport.draft),[.3,.1,.7,.6]);
    h.test.finishCrop(true);
    assert.deepEqual(plain(h.test.active().recipe.crop),[.3,.1,.7,.6]);
    assert.equal(h.test.viewport.draft,null);
  });
  await check("Cancel ignores a late detection without reopening crop", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop(), req = deferred(h);
    h.test.finishCrop(false); assert(req.signal.aborted);
    req.complete(ready([.2,.2,.8,.8])); await promise;
    assert.equal(h.test.viewport.draft,null);
    assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
  });
  await check("newest framing request wins out-of-order results", async () => {
    const h = harness(), first = h.test.suggestSmartCrop(), req1 = deferred(h);
    h.nodes.get("smartCropPreset").value="full-body";
    const second = h.nodes.get("smartCropPreset").onchange(), req2 = deferred(h);
    assert(req1.signal.aborted); assert.equal(req2.data.preset,"full-body");
    req2.complete(ready([.1,.05,.9,.95])); await second;
    req1.complete(ready([.4,.4,.6,.6])); await first;
    assert.deepEqual(plain(h.test.viewport.draft),[.1,.05,.9,.95]);
    assert(!h.nodes.get("suggestCrop").disabled);
  });
  await check("older completion cannot enable Apply during newer detection", async () => {
    const h = harness(), first = h.test.suggestSmartCrop(), req1 = deferred(h);
    const second = h.test.suggestSmartCrop(), req2 = deferred(h);
    req1.complete(ready([.2,.2,.8,.8])); await first;
    assert(h.nodes.get("applyCrop").disabled); assert(h.test.smartCrop.loading);
    req2.complete(ready([.15,.15,.85,.85])); await second;
    assert(!h.nodes.get("applyCrop").disabled);
  });
  await check("no subject preserves manual draft and recipe", async () => {
    const h = harness(), before = plain(h.test.viewport.draft), promise = h.test.suggestSmartCrop();
    deferred(h).complete({crop:null,status:"no_subject",subjects:[],selected_subject:null,warning:"Subject not detected."}); await promise;
    assert.deepEqual(plain(h.test.viewport.draft),before);
    assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
    assert.match(h.nodes.get("smartCropNotice").textContent,/Subject not detected/);
  });
  await check("network or model error preserves existing crop", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop();
    deferred(h).fail("Offline models are not installed."); await promise;
    assert.deepEqual(plain(h.test.viewport.draft),[.1,.1,.9,.9]);
    assert.match(h.nodes.get("smartCropNotice").textContent,/not installed/);
  });
  await check("malformed crop is rejected without modifying draft", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop();
    deferred(h).complete(ready([.9,.2,.3,.8])); await promise;
    assert.deepEqual(plain(h.test.viewport.draft),[.1,.1,.9,.9]);
    assert.match(h.nodes.get("smartCropNotice").textContent,/invalid/);
  });
  await check("individual checkbox choice and aspect are sent to backend", async () => {
    const h = harness(), first = h.test.suggestSmartCrop();
    deferred(h).complete(ready([.1,.1,.4,.8],{subjects:groupSubjects})); await first;
    assert(!h.nodes.get("smartCropSubjectField").hidden);
    togglePerson(h,"person-0",false);
    const second = togglePerson(h,"person-1",true);
    assert.deepEqual(deferred(h).data.subject_ids,["person-1"]);
    assert(!("subject_id" in deferred(h).data));
    deferred(h).complete(ready([.6,.1,.9,.8],{subjects:groupSubjects,selected_subject:"person-1"})); await second;
    assert.deepEqual(plain(h.test.viewport.draft),[.6,.1,.9,.8]);
    h.nodes.get("aspect").value="4:5";
    h.nodes.get("aspect").onchange({target:h.nodes.get("aspect")});
    const req = deferred(h);
    assert.equal(req.data.aspect,"4:5"); assert.deepEqual(req.data.subject_ids,["person-1"]);
    req.complete(ready([.5,.1,.95,.8],{subjects:groupSubjects,selected_subject:"person-1",aspect:"4:5"}));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.test.viewport.aspect,"4:5");
    assert.equal(h.nodes.get("aspect").value,"4:5");
  });
  await check("All people checkbox frames the group and persists across presets", async () => {
    const h = harness(), initial = h.test.suggestSmartCrop();
    deferred(h).complete(ready([.1,.1,.4,.7],{subjects:groupSubjects})); await initial;
    assert.equal(h.test.smartCrop.inputs.size,2,"Synthetic group must not become a person checkbox");
    assert(h.nodes.get("smartCropAll").indeterminate);
    const group = toggleAll(h);
    assert.deepEqual(deferred(h).data.subject_ids,["person-0","person-1"]);
    deferred(h).complete(ready([.05,.1,.95,.8],{subjects:groupSubjects,selected_subject:"all",method:"group"})); await group;
    assert(h.nodes.get("smartCropAll").checked);
    assert(!h.nodes.get("smartCropAll").indeterminate);
    assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
    assert.match(h.nodes.get("smartCropNotice").textContent,/all 2 detected people/);
    h.nodes.get("smartCropPreset").value="full-body";
    const fullBody = h.nodes.get("smartCropPreset").onchange();
    assert.deepEqual(deferred(h).data.subject_ids,["person-0","person-1"]);
    assert.equal(deferred(h).data.preset,"full-body");
    deferred(h).complete(ready([.05,.05,.95,.95],{subjects:groupSubjects,selected_subject:"all",method:"group"})); await fullBody;
    assert(h.nodes.get("smartCropAll").checked);
    assert.deepEqual(plain(h.test.viewport.draft),[.05,.05,.95,.95]);
    h.test.finishCrop(true);
    assert.deepEqual(plain(h.test.active().recipe.crop),[.05,.05,.95,.95]);
  });
  await check("two of three people remain selected through rapid out-of-order replies", async () => {
    const subjects = [
      ...groupSubjects.slice(0,2),
      {id:"person-2",label:"Person 3 (far right)",kind:"person",box:[.92,.1,.99,.9]},
      {...groupSubjects[2],label:"All people (3)",member_ids:["person-0","person-1","person-2"]}
    ];
    const h = harness(), initial = h.test.suggestSmartCrop();
    deferred(h).complete(ready([.1,.1,.4,.7],{subjects})); await initial;
    const all = toggleAll(h), old = deferred(h);
    const subset = togglePerson(h,"person-2",false), latest = deferred(h);
    assert(old.signal.aborted);
    assert.deepEqual(latest.data.subject_ids,["person-0","person-1"]);
    latest.complete(ready([.05,.1,.95,.8],{subjects,selected_subject:"group",selected_subjects:["person-0","person-1"],method:"group"})); await subset;
    old.complete(ready([0,0,1,1],{subjects,selected_subject:"all",method:"group"})); await all;
    assert.deepEqual(plain(h.test.smartCrop.selectedSubjects),["person-0","person-1"]);
    assert(h.test.smartCrop.inputs.get("person-0").checked);
    assert(h.test.smartCrop.inputs.get("person-1").checked);
    assert(!h.test.smartCrop.inputs.get("person-2").checked);
    assert(!h.nodes.get("smartCropAll").checked);
    assert(h.nodes.get("smartCropAll").indeterminate);
    assert.match(h.nodes.get("smartCropNotice").textContent,/2 selected people/);
    assert.deepEqual(plain(h.test.viewport.draft),[.05,.1,.95,.8]);
  });
  await check("changing checked people retains an aspect request still in flight", async () => {
    const h = harness(), initial = h.test.suggestSmartCrop();
    deferred(h).complete(ready([.1,.1,.4,.7],{subjects:groupSubjects})); await initial;
    h.nodes.get("aspect").value="4:5";
    const aspect = h.nodes.get("aspect").onchange({target:h.nodes.get("aspect")}), old = deferred(h);
    const group = togglePerson(h,"person-1",true), latest = deferred(h);
    assert(old.signal.aborted);
    assert.equal(latest.data.aspect,"4:5");
    assert.deepEqual(latest.data.subject_ids,["person-0","person-1"]);
    latest.complete(ready([.1,.1,.9,.9],{subjects:groupSubjects,selected_subject:"all",method:"group",aspect:"4:5"})); await group;
    old.complete(ready([.1,.1,.5,.7],{subjects:groupSubjects,aspect:"4:5"})); await aspect;
    assert.equal(h.nodes.get("aspect").value,"4:5");
    assert.equal(h.test.viewport.aspect,"4:5");
  });
  await check("unchecking every person cancels pending detection and keeps the crop", async () => {
    const h = harness(), initial = h.test.suggestSmartCrop();
    deferred(h).complete(ready([.1,.1,.4,.7],{subjects:groupSubjects})); await initial;
    const group = toggleAll(h), old = deferred(h);
    const count = h.requests.length;
    toggleAll(h,false);
    assert(old.signal.aborted);
    assert.equal(h.requests.length,count,"An empty selection must not request automatic subject selection");
    assert(h.nodes.get("suggestCrop").disabled);
    assert.match(h.nodes.get("smartCropNotice").textContent,/Choose at least one person/);
    old.complete(ready([.05,.05,.95,.95],{subjects:groupSubjects,selected_subject:"all",method:"group"})); await group;
    assert.deepEqual(plain(h.test.smartCrop.selectedSubjects),[]);
    assert.deepEqual(plain(h.test.viewport.draft),[.1,.1,.4,.7]);
    const next = togglePerson(h,"person-1",true);
    assert.deepEqual(deferred(h).data.subject_ids,["person-1"]);
    deferred(h).complete(ready([.6,.1,.9,.7],{subjects:groupSubjects,selected_subject:"person-1"})); await next;
    assert(!h.nodes.get("suggestCrop").disabled);
  });
  await check("switching from all people to one ignores the late group result", async () => {
    const h = harness(), initial = h.test.suggestSmartCrop();
    deferred(h).complete(ready([.1,.1,.4,.7],{subjects:groupSubjects})); await initial;
    const group = toggleAll(h), old = deferred(h);
    const person = togglePerson(h,"person-0",false), latest = deferred(h);
    assert(old.signal.aborted);
    assert.deepEqual(latest.data.subject_ids,["person-1"]);
    latest.complete(ready([.6,.1,.9,.7],{subjects:groupSubjects,selected_subject:"person-1"})); await person;
    old.complete(ready([.05,.05,.95,.95],{subjects:groupSubjects,selected_subject:"all",method:"group"})); await group;
    assert(!h.test.smartCrop.inputs.get("person-0").checked);
    assert(h.test.smartCrop.inputs.get("person-1").checked);
    assert.deepEqual(plain(h.test.viewport.draft),[.6,.1,.9,.7]);
    assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
  });
  await check("Cancel preserves the accepted crop while a group result is pending", async () => {
    const h = harness(), initial = h.test.suggestSmartCrop();
    deferred(h).complete(ready([.1,.1,.4,.7],{subjects:groupSubjects})); await initial;
    h.test.finishCrop(true);
    h.test.loaded(); h.test.startCrop(); h.test.loaded();
    const group = toggleAll(h), request = deferred(h);
    h.test.finishCrop(false);
    request.complete(ready([.05,.05,.95,.95],{subjects:groupSubjects,selected_subject:"all",method:"group"})); await group;
    assert.equal(h.test.viewport.draft,null);
    assert.deepEqual(plain(h.test.active().recipe.crop),[.1,.1,.4,.7]);
  });
  await check("backend Free fallback is shown when ratio would clip subject", async () => {
    const h = harness(); h.nodes.get("aspect").value="4:5";
    const promise = h.test.suggestSmartCrop();
    deferred(h).complete(ready([0,0,1,1],{aspect:"Free",warning:"Kept the whole visible person; requested ratio does not fit."})); await promise;
    assert.equal(h.nodes.get("aspect").value,"Free");
    assert.equal(h.test.viewport.aspect,"Free");
    assert.match(h.nodes.get("smartCropNotice").textContent,/whole visible person/);
  });
  await check("failed aspect request restores actual ratio and preserves draft", async () => {
    for (const failure of ["error", "no-subject", "cancel"]) {
      const h = harness(), first = h.test.suggestSmartCrop();
      deferred(h).complete(ready([.2,.1,.8,.85])); await first;
      h.nodes.get("aspect").value="4:5";
      const promise = h.nodes.get("aspect").onchange({target:h.nodes.get("aspect")});
      const req = deferred(h);
      if (failure === "error") req.fail("Detection failed.");
      else if (failure === "no-subject") req.complete({crop:null,status:"no_subject"});
      else {
        h.nodes.get("cancelDetection").onclick();
        req.complete(ready([.1,.1,.9,.9],{aspect:"4:5"}));
      }
      await promise;
      assert.equal(h.nodes.get("aspect").value,"Free");
      assert.equal(h.test.viewport.aspect,"Free");
      assert.deepEqual(plain(h.test.viewport.draft),[.2,.1,.8,.85]);
    }
  });
  await check("manual drag cancels inference and cannot be overwritten", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop(), req = deferred(h);
    const view = h.nodes.get("viewport");
    view.events.get("pointerdown")({button:0,pointerId:1,clientX:500,clientY:350,preventDefault(){},target:view});
    view.events.get("pointermove")({pointerId:1,clientX:550,clientY:370});
    view.events.get("pointerup")({pointerId:1});
    const adjusted = plain(h.test.viewport.draft);
    assert(req.signal.aborted); assert.notDeepEqual(adjusted,[.1,.1,.9,.9]);
    req.complete(ready([.4,.4,.6,.6])); await promise;
    assert.deepEqual(plain(h.test.viewport.draft),adjusted);
  });
  await check("Reset invalidates late results", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop(), req = deferred(h);
    h.nodes.get("resetCrop").onclick(); req.complete(ready([.4,.4,.6,.6])); await promise;
    assert.deepEqual(plain(h.test.viewport.draft),[0,0,1,1]);
  });
  await check("switching photos ignores previous photo result", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop(), req = deferred(h);
    h.test.selectPhoto("two"); req.complete(ready([.4,.4,.6,.6])); await promise;
    assert.equal(h.test.active().id,"two"); assert.equal(h.test.viewport.draft,null);
    assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
    assert(h.nodes.get("smartCropSubjectField").hidden);
  });
  await check("Cancel detection preserves draft and restores Apply", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop(), req = deferred(h);
    h.nodes.get("cancelDetection").onclick();
    assert(!h.nodes.get("applyCrop").disabled); assert(req.signal.aborted);
    req.complete(ready([.4,.4,.6,.6])); await promise;
    assert.deepEqual(plain(h.test.viewport.draft),[.1,.1,.9,.9]);
  });
  await check("switching to an empty folder cancels detection", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop(), req = deferred(h);
    h.test.applyFolder({path:"/empty",name:"empty",parent:"/",folders:[],photos:[]});
    req.complete(ready([.4,.4,.6,.6])); await promise;
    assert.equal(h.test.active(),undefined);
    assert.equal(h.test.viewport.draft,null);
    assert(h.nodes.get("suggestCrop").disabled);
  });
  await check("Enter and Save All cannot commit an unseen crop", async () => {
    const h = harness(), promise = h.test.suggestSmartCrop(), req = deferred(h);
    h.test.finishCrop(true);
    assert(h.test.viewport.draft); assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
    await h.test.saveAll(); assert(!h.requests.some(r => r.url === "/api/export"));
    assert.match(h.nodes.get("status").textContent,/Apply or cancel/);
    req.complete(ready([.4,.4,.6,.6])); await promise;
    await h.test.saveAll(); assert(!h.requests.some(r => r.url === "/api/export"));
    assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
  });
  await check("Enter and Space activate focused controls instead of accepting crop", async () => {
    const h = harness();
    for (const id of ["suggestCrop","smartCropPreset","smartCropAll"])
      for (const key of ["Enter"," "]) {
        let prevented = false;
        h.handlers.get("keydown")({key,target:h.nodes.get(id),preventDefault() {prevented=true;}});
        assert.equal(prevented,false,`${key} on ${id} must reach the control`);
        assert.deepEqual(plain(h.test.viewport.draft),[.1,.1,.9,.9]);
        assert.deepEqual(plain(h.test.active().recipe.crop),[0,0,1,1]);
      }
  });
  await check("browser trial explains unavailable models and keeps manual crop", async () => {
    const h = harness({trial:true});
    assert(h.nodes.get("suggestCrop").disabled);
    assert.match(h.nodes.get("smartCropNotice").textContent,/Linux desktop app/);
    await h.test.suggestSmartCrop(); assert.equal(h.requests.length,0);
    h.test.finishCrop(true); assert.deepEqual(plain(h.test.active().recipe.crop),[.1,.1,.9,.9]);
  });
  console.log(`${passed} Smart Crop controller regressions passed.`);
}
run().catch(error => {console.error(error);process.exitCode=1;});
