import {
  PhotoViewport
} from "./viewport.js";

const $ = id => document.getElementById(id);
const browserTrial = globalThis.fieldroomTrial;
const apiKey = browserTrial ? "" : location.hash.slice(1) || sessionStorage.getItem("fieldroom-key") || "";
if (apiKey) {
  sessionStorage.setItem("fieldroom-key", apiKey);
  history.replaceState(null, "", location.pathname);
}
const records = new Map(),
  deleted = [],
  overviewCache = new Map(),
  thumbCache = new Map(),
  thumbButtons = new Map();
const thumbQueue = new Map(),
  thumbInFlight = new Set();
let defaults, folder, places = [],
  selected = null,
  rawAvailable = true,
  original = false,
  busy = false;
let folderTicket = 0,
  browserTicket = 0,
  browserFolder = null,
  folderToShow = null;
let revision = 0,
  pending = null,
  rendering = false,
  renderTimer;

function status(message) {
  $("status").textContent = message;
  $("status").title = message;
}

function fail(error) {
  status(error.message || String(error));
}

function copy(value) {
  return structuredClone(value);
}

function dirty(p) {
  return JSON.stringify(p.recipe) !== JSON.stringify(p.saved);
}

function active() {
  return records.get(selected);
}

function icon(name) {
  const ns = "http://www.w3.org/2000/svg",
    svg = document.createElementNS(ns, "svg"),
    use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#" + name);
  svg.setAttribute("aria-hidden", "true");
  svg.append(use);
  return svg;
}
async function api(route, data = {}, binary = false) {
  if (browserTrial) return browserTrial.request(route, data);
  const response = await fetch("/api/" + route, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Fieldroom-Key": apiKey
    },
    body: JSON.stringify(data)
  });
  if (!response.ok) {
    const e = await response.json().catch(() => ({}));
    throw Error(e.error || `Request failed (${response.status}).`);
  }
  return binary ? response.blob() : response.json();
}
const viewport = new PhotoViewport({
  loadTile: data => api("tile", data, true),
  onCrop: (w, h) => {
    $("cropSize").textContent = `${w.toLocaleString()} × ${h.toLocaleString()} px`;
  },
  onError: fail
});
const controls = {
  Basic: [
    ["Exposure", "exposure", -4, 4, .05],
    ["Contrast", "contrast", -100, 100, 1],
    ["Highlights", "highlights", -100, 100, 1],
    ["Shadows", "shadows", -100, 100, 1],
    ["Whites", "whites", -100, 100, 1],
    ["Blacks", "blacks", -100, 100, 1],
    ["Warmth", "warmth", -100, 100, 1],
    ["Saturation", "saturation", -100, 100, 1]
  ],
  "Noise reduction": [
    ["Luminance", "luminance", 0, 100, 1],
    ["Color noise", "color", 0, 100, 1]
  ],
  Sharpening: [
    ["Amount", "sharpen", 0, 100, 1],
    ["Radius", "radius", .5, 3, .1]
  ]
};

function adjustValue(key, value) {
  if (key === "exposure") return (value > 0 ? "+" : "") + value.toFixed(2);
  if (key === "radius") return value.toFixed(1);
  return String(value > 0 && ["contrast", "highlights", "shadows", "whites", "blacks", "warmth", "saturation"]
    .includes(key) ? "+" + value : value);
}

function updateAdjustments() {
  const p = active();
  for (const specs of Object.values(controls))
    for (const [, k] of specs) {
      const input = $("slider-" + k);
      if (!input) continue;
      const value = (p?.recipe || defaults)[k];
      input.value = value;
      input.disabled = !p || busy;
      $("value-" + k).textContent = adjustValue(k, value);
      input.setAttribute("aria-valuetext", adjustValue(k, value));
    }
}

function createAdjustments() {
  for (const [title, specs] of Object.entries(controls)) {
    const details = document.createElement("details");
    details.open = title === "Basic";
    const summary = document.createElement("summary");
    summary.textContent = title;
    const body = document.createElement("div");
    body.className = "sliders";
    for (const [label, key, min, max, step] of specs) {
      const row = document.createElement("div");
      row.className = "slider-row";
      const caption = document.createElement("label");
      caption.textContent = label;
      caption.htmlFor = "slider-" + key;
      const input = document.createElement("input");
      Object.assign(input, {
        type: "range",
        id: "slider-" + key,
        min,
        max,
        step,
        value: defaults[key]
      });
      const out = document.createElement("output");
      out.id = "value-" + key;
      out.htmlFor = input.id;
      const change = v => {
        const p = active();
        if (!p || busy) return;
        p.recipe[key] = v;
        input.value = v;
        out.textContent = adjustValue(key, v);
        input.setAttribute("aria-valuetext", out.textContent);
        if (original) {
          original = false;
          toggleTools();
        }
        mark(p);
        queuePreview(58);
      };
      input.addEventListener("input", () => change(Number(input.value)));
      input.addEventListener("change", () => {
        const p = active();
        if (p) queueThumb(p);
      });
      row.ondblclick = () => {
        change(defaults[key]);
        if (active()) queueThumb(active());
      };
      row.append(caption, input, out);
      body.append(row);
    }
    details.append(summary, body);
    $("adjustments").append(details);
  }
  const button = document.createElement("button");
  button.id = "resetAll";
  button.textContent = "Reset photo edits";
  button.onclick = () => {
    const p = active();
    if (!p || busy) return;
    p.recipe = copy(defaults);
    original = false;
    updateAdjustments();
    toggleTools();
    mark(p);
    queuePreview();
    queueThumb(p);
  };
  $("adjustments").append(button);
}

function drawHist(channels) {
  const canvas = $("histogram"),
    g = canvas.getContext("2d"),
    w = canvas.width,
    h = canvas.height;
  g.clearRect(0, 0, w, h);
  for (let i = 1; i < 4; i++) {
    g.fillStyle = "#393c3d";
    g.fillRect(w * i / 4, 2, 1, h - 4);
  }
  if (!channels) return;
  const colors = ["224,120,111", "112,179,143", "124,163,212"],
    max = Math.max(1, ...channels.flat());
  for (const c of [2, 0, 1]) {
    g.beginPath();
    g.moveTo(0, h);
    channels[c].forEach((v, i) => g.lineTo(i * w / (channels[c].length - 1), h - 2 - (v / max) ** .52 * (h -
      5)));
    g.lineTo(w, h);
    g.closePath();
    g.fillStyle = "rgba(" + colors[c] + ",.28)";
    g.fill();
    g.strokeStyle = "rgba(" + colors[c] + ",.9)";
    g.lineWidth = 1.2;
    g.stroke();
  }
}

function remember() {
  if (!folder || browserTrial) return;
  try {
    sessionStorage.setItem("fieldroom-session", JSON.stringify({
      path: folder.path,
      id: selected,
      changes: [...records.values()].filter(dirty).map(p => ({
        id: p.id,
        name: p.name,
        folder: p.folder,
        recipe: p.recipe,
        saved: p.saved,
        exported: p.exported
      }))
    }));
  } catch {
    /* Editing remains available if browser session storage is full. */ }
}

function mark(p) {
  thumbButtons.get(p.id)?.classList.toggle("dirty", dirty(p));
  $("saveAll").title =
    `Save this folder and pending edits in other folders (${[...records.values()].filter(dirty).length} edited)`;
  remember();
}

function queuePreview(delay = 0) {
  const p = active();
  clearTimeout(renderTimer);
  const token = ++revision;
  if (!p) {
    pending = null;
    return;
  }
  const recipe = copy(original ? {
    ...defaults,
    crop: p.recipe.crop
  } : p.recipe);
  if (viewport.draft) recipe.crop = copy(defaults.crop);
  pending = {
    id: p.id,
    token,
    recipe,
    key: p.id + JSON.stringify(recipe),
    ready: false
  };
  renderTimer = setTimeout(() => {
    if (pending?.token === token) {
      pending.ready = true;
      drainPreview();
    }
  }, delay);
}
async function drainPreview() {
  if (rendering || !pending?.ready) return;
  const job = pending;
  pending = null;
  rendering = true;
  try {
    let data = overviewCache.get(job.key);
    if (!data) {
      data = await api("preview", {
        id: job.id,
        recipe: job.recipe
      });
      overviewCache.set(job.key, data);
      if (overviewCache.size > 5) overviewCache.delete(overviewCache.keys().next().value);
    }
    if (job.token !== revision || selected !== job.id) return;
    viewport.setSource(active(), job.recipe, data);
    $("infoSize").textContent = `${data.size[0].toLocaleString()} × ${data.size[1].toLocaleString()} px`;
    $("infoCamera").textContent = data.metadata.camera || "";
    $("infoCamera").title = data.metadata.rawError || "";
    $("infoCapture").textContent = [data.metadata.iso, data.metadata.aperture, data.metadata.shutter, data
      .metadata.lens
    ].filter(Boolean).join(" · ");
    drawHist(data.histogram);
    toggleTools();
  } catch (error) {
    if (job.token === revision) {
      viewport.clear(error.message);
      drawHist();
      fail(error);
      toggleTools();
    }
  } finally {
    rendering = false;
    if (pending?.ready) drainPreview();
  }
}

function cancelQueuedView() {
  revision++;
  pending = null;
  clearTimeout(renderTimer);
}

function folderRow(label, path, action, {
  kind = "folder",
  activeRow = false,
  child = false,
  count
} = {}) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "folder-row" + (activeRow ? " active" : "") + (child ? " child" : "");
  b.title = path;
  b.append(icon(kind));
  const span = document.createElement("span");
  span.textContent = label;
  b.append(span);
  if (count !== undefined) {
    const number = document.createElement("small");
    number.textContent = count;
    b.append(number);
  }
  b.onclick = () => action(path);
  return b;
}

function redrawFolders() {
  const root = $("folderRows");
  root.replaceChildren();
  if (folder.parent !== folder.path) root.append(folderRow("Parent folder", folder.parent, openFolder, {
    kind: "folderup"
  }));
  root.append(folderRow(folder.name, folder.path, openFolder, {
    activeRow: true,
    count: folder.photos.length
  }));
  for (const d of folder.folders) root.append(folderRow(d.name, d.path, openFolder, {
    child: true
  }));
  $("filmInfo").textContent =
    `${folder.name}  /  ${folder.photos.length} photo${folder.photos.length===1?"":"s"}`;
}
async function openFolder(path, pick) {
  if (busy) return;
  const ticket = ++folderTicket;
  try {
    const result = await api("folder", {
      path
    });
    if (ticket === folderTicket && !busy) applyFolder(result, pick);
  } catch (error) {
    if (ticket === folderTicket) fail(error);
  }
}

function applyFolder(result, pick) {
  if (viewport.draft) viewport.endCrop();
  original = false;
  folder = result;
  for (const photo of folder.photos) {
    const prev = records.get(photo.id);
    if (!prev || !dirty(prev)) records.set(photo.id, {
      ...photo,
      recipe: copy(photo.recipe),
      saved: copy(photo.recipe)
    });
  }
  redrawFolders();
  buildStrip();
  selectPhoto(folder.photos.some(p => p.id === pick) ? pick : folder.photos[0]?.id, true);
  status(folder.path);
  remember();
}
const thumbObserver = new IntersectionObserver(entries => {
  for (const item of entries) {
    const p = records.get(item.target.dataset.id);
    if (item.isIntersecting && p) queueThumb(p);
  }
}, {
  root: $("filmstrip"),
  rootMargin: "0px 180px"
});

function buildStrip() {
  thumbObserver.disconnect();
  thumbQueue.clear();
  thumbButtons.clear();
  const frag = document.createDocumentFragment();
  for (const row of folder.photos) {
    const p = records.get(row.id),
      b = document.createElement("button");
    b.className = "thumb";
    b.dataset.id = p.id;
    b.setAttribute("role", "option");
    b.title = p.name;
    b.classList.toggle("dirty", dirty(p));
    const img = new Image();
    img.alt = "";
    img.draggable = false;
    const name = document.createElement("span");
    name.textContent = p.name;
    b.append(img, name);
    b.onclick = () => selectPhoto(p.id);
    frag.append(b);
    thumbButtons.set(p.id, b);
    thumbObserver.observe(b);
  }
  $("filmstrip").replaceChildren(frag);
}

function queueThumb(p) {
  const b = thumbButtons.get(p.id);
  if (!b) return;
  const recipe = copy(p.recipe),
    key = p.id + JSON.stringify(recipe);
  b.dataset.thumbKey = key;
  if (thumbCache.has(key)) {
    b.firstChild.src = thumbCache.get(key);
    return;
  }
  if (!thumbInFlight.has(key)) thumbQueue.set(p.id, {
    id: p.id,
    key,
    recipe
  });
  pumpThumbs();
}

function pumpThumbs() {
  while (thumbInFlight.size < 2 && thumbQueue.size) {
    const [id, job] = thumbQueue.entries().next().value;
    thumbQueue.delete(id);
    thumbInFlight.add(job.key);
    api("thumbnail", {
        id,
        recipe: job.recipe
      }, true).then(blob => {
        const url = URL.createObjectURL(blob);
        thumbCache.set(job.key, url);
        const b = thumbButtons.get(id);
        if (b?.dataset.thumbKey === job.key) b.firstChild.src = url;
        if (thumbCache.size > 180) {
          const oldest = thumbCache.keys().next().value;
          URL.revokeObjectURL(thumbCache.get(oldest));
          thumbCache.delete(oldest);
        }
      }).catch(error => {
        const b = thumbButtons.get(id);
        if (b) b.title = records.get(id).name + " · " + error.message;
      })
      .finally(() => {
        thumbInFlight.delete(job.key);
        pumpThumbs();
      });
  }
}

function selectPhoto(id, force = false) {
  if (busy || (!force && id === selected)) return;
  if (viewport.draft) viewport.endCrop();
  original = false;
  cancelQueuedView();
  thumbButtons.get(selected)?.setAttribute("aria-selected", "false");
  selected = id || null;
  const p = active(),
    b = thumbButtons.get(selected);
  viewport.clear(p ? (/\.(nef|dng)$/i.test(p.name) ? "Developing " + p.name + "…" : "Opening photo…") : "This folder has no supported photos.");
  if (b) {
    b.setAttribute("aria-selected", "true");
    b.scrollIntoView({
      block: "nearest",
      inline: "nearest"
    });
    queueThumb(p);
  }
  const i = folder.photos.findIndex(q => q.id === id);
  $("photoName").textContent = p?.name || folder.name;
  $("position").textContent = p ? `${i+1} / ${folder.photos.length}` : "";
  $("infoName").textContent = p?.name || "—";
  for (const key of ["infoSize", "infoCamera", "infoCapture"]) $(key).textContent = "";
  updateAdjustments();
  toggleTools();
  drawHist();
  if (p) queuePreview();
  remember();
}

function movePhoto(step) {
  const index = folder?.photos.findIndex(p => p.id === selected) ?? -1;
  const target = folder?.photos[index + step];
  if (target) selectPhoto(target.id);
}

function toggleTools() {
  const p = active(),
    crop = !!viewport.draft,
    loaded = !!viewport.box,
    i = folder?.photos.findIndex(x => x.id === selected) ?? -1;
  $("applyCrop").hidden = !crop;
  $("cancelCrop").hidden = !crop;
  $("originalTool").hidden = crop;
  $("cropPanel").hidden = !crop;
  $("developPanel").hidden = crop;
  $("cropTool").setAttribute("aria-pressed", String(crop));
  $("originalTool").setAttribute("aria-pressed", String(original));
  for (const id of ["cropTool", "originalTool", "zoomIn", "zoomOut", "zoomSelect"]) $(id).disabled = !
    loaded || busy;
  document.querySelectorAll("[data-zoom]").forEach(b => b.disabled = !loaded || busy);
  $("trashPhoto").disabled = !p || busy;
  $("previousPhoto").disabled = i <= 0 || busy;
  $("nextPhoto").disabled = i < 0 || i >= folder.photos.length - 1 || busy;
  $("openFolder").disabled = busy;
  $("saveAll").disabled = busy || (!folder?.photos.length && ![...records.values()].some(dirty));
  $("undoTrash").hidden = !deleted.length;
  $("undoTrash").disabled = busy;
  $("resetAll").disabled = !p || busy;
}

function startCrop() {
  const p = active();
  if (!p || !viewport.box || busy) return;
  if (viewport.draft) {
    finishCrop(true);
    return;
  }
  original = false;
  viewport.beginCrop(p.recipe.crop);
  viewport.clear("Opening crop…");
  toggleTools();
  queuePreview();
}

function finishCrop(accept) {
  const p = active();
  if (!p || !viewport.draft) return;
  const rect = viewport.endCrop();
  if (accept) {
    p.recipe.crop = rect;
    mark(p);
    queueThumb(p);
    status(`Crop accepted for ${p.name}`);
  }
  toggleTools();
  queuePreview();
}

function setBusy(value) {
  busy = value;
  document.querySelector(".workspace").inert = value;
  $("filmstrip").inert = value;
  toggleTools();
}
async function trash() {
  const p = active();
  if (!p || busy) return;
  const idx = folder.photos.findIndex(x => x.id === p.id);
  setBusy(true);
  try {
    const r = await api("trash", {
      id: p.id
    });
    deleted.push({
      ticket: r.ticket,
      photo: p
    });
    if (viewport.draft) viewport.endCrop();
    records.delete(p.id);
    folder.photos.splice(idx, 1);
    setBusy(false);
    redrawFolders();
    buildStrip();
    selectPhoto(folder.photos[Math.min(idx, folder.photos.length - 1)]?.id, true);
    status(browserTrial ? `${p.name} removed from this trial. Undo delete restores it.` : `${p.name} moved to Fieldroom Trash.`);
  } catch (error) {
    fail(error);
  } finally {
    setBusy(false);
  }
}
async function undo() {
  if (!deleted.length || busy) return;
  const item = deleted[deleted.length - 1];
  setBusy(true);
  try {
    const restored = await api("undo", {
      ticket: item.ticket
    });
    deleted.pop();
    records.set(item.photo.id, item.photo);
    const listing = await api("folder", {
      path: restored.folder
    });
    setBusy(false);
    applyFolder(listing, restored.id);
    status(`Restored ${restored.name}.`);
  } catch (error) {
    fail(error);
  } finally {
    setBusy(false);
  }
}
async function saveAll() {
  if (!folder || busy) return;
  if (viewport.draft) finishCrop(true);
  const jobs = new Map(folder.photos.map(p => [p.id, records.get(p.id)]));
  for (const p of records.values())
    if (dirty(p)) jobs.set(p.id, p);
  if (!jobs.size) return;
  setBusy(true);
  let saved = 0,
    skipped = 0,
    index = 0;
  const errors = [],
    outputs = new Set();
  try {
    browserTrial?.startExport();
    for (const p of jobs.values()) {
      $("saveAll").querySelector("span").textContent = `Saving ${++index}/${jobs.size}`;
      status(`Saving ${p.name}…`);
      try {
        const r = await api("export", {
          id: p.id,
          recipe: p.recipe
        });
        if (r.skipped) skipped++;
        else saved++;
        p.saved = copy(p.recipe);
        p.exported = true;
        mark(p);
        outputs.add(r.path.slice(0, Math.max(r.path.lastIndexOf("/"), r.path.lastIndexOf("\\"))));
      } catch (error) {
        errors.push(`${p.name} — ${error.message}`);
      }
    }
    const msg =
      browserTrial ? `${saved} edited JPEG${saved === 1 ? "" : "s"} ready to download${errors.length ? " · " + errors.length + " failed" : ""}` :
      `${saved} saved · ${skipped} already current${errors.length?" · "+errors.length+" failed":""}`;
    const report = $("saveReport");
    report.replaceChildren();
    const text = document.createElement("p");
    text.textContent = msg;
    report.append(text);
    for (const path of browserTrial ? [] : outputs) {
      const d = document.createElement("p");
      d.textContent = path;
      report.append(d);
    }
    if (errors.length) {
      const ul = document.createElement("ul");
      for (const s of errors) {
        const li = document.createElement("li");
        li.textContent = s;
        ul.append(li);
      }
      report.append(ul);
    }
    folderToShow = [...outputs].find(path => path.startsWith(folder.path)) || [...outputs][0];
    if (browserTrial && saved) {
      browserTrial.finishExport();
      $("viewEdited").textContent = "Download ZIP";
    }
    $("viewEdited").hidden = browserTrial ? !saved : !folderToShow;
    const updated = await api("folder", {
      path: folder.path
    }).catch(() => null);
    if (updated) {
      folder.folders = updated.folders;
      redrawFolders();
    }
    status(msg);
    if (!$("resultsDialog").open) $("resultsDialog").showModal();
  } finally {
    setBusy(false);
    $("saveAll").querySelector("span").textContent = "Save All";
    remember();
  }
}
async function browseTo(path) {
  const ticket = ++browserTicket;
  $("folderPath").value = path;
  $("folderError").textContent = "";
  $("chooseFolder").disabled = true;
  try {
    const data = await api("directories", {
      path
    });
    if (ticket !== browserTicket) return;
    browserFolder = data;
    if ($("folderPath").value === path) $("folderPath").value = data.path;
    const list = $("dialogFolders");
    list.replaceChildren(...data.folders.map(d => folderRow(d.name, d.path, browseTo)));
    $("dialogUp").disabled = data.parent === data.path;
    $("chooseFolder").disabled = false;
  } catch (error) {
    if (ticket === browserTicket) {
      browserFolder = null;
      $("chooseFolder").disabled = !$("folderPath").value.trim();
      $("folderError").textContent = error.message;
    }
  }
}

function showFolders() {
  if (busy) return;
  if (browserTrial) return browserTrial.showFiles();
  const dialog = $("folderDialog");
  if (!dialog.open) dialog.showModal();
  browseTo(folder?.path || places[0]?.path);
  $("folderPath").focus();
}
$("openFolder").onclick = showFolders;
$("saveAll").onclick = () => saveAll().catch(fail);
$("trashPhoto").onclick = trash;
$("undoTrash").onclick = undo;
$("nextPhoto").onclick = () => movePhoto(1);
$("previousPhoto").onclick = () => movePhoto(-1);
$("cropTool").onclick = startCrop;
$("applyCrop").onclick = () => finishCrop(true);
$("cancelCrop").onclick = () => finishCrop(false);
$("originalTool").onclick = () => {
  if (!active()) return;
  original = !original;
  toggleTools();
  queuePreview();
};
$("aspect").onchange = e => viewport.setAspect(e.target.value);
$("resetCrop").onclick = () => {
  viewport.draft = [0, 0, 1, 1];
  viewport.aspect = "Free";
  $("aspect").value = "Free";
  viewport.drawCrop();
};
$("dialogUp").onclick = () => {
  if (browserFolder) browseTo(browserFolder.parent);
};
$("folderPath").oninput = () => {
  $("chooseFolder").disabled = !$("folderPath").value.trim();
};
$("pathForm").onsubmit = e => {
  e.preventDefault();
  browseTo($("folderPath").value);
};
$("chooseFolder").onclick = async () => {
  const path = $("folderPath").value.trim();
  if (!path) return;
  try {
    const data = await api("folder", {
      path
    });
    $("folderDialog").close();
    applyFolder(data);
  } catch (error) {
    $("folderError").textContent = error.message;
  }
};
$("viewEdited").onclick = () => {
  if (browserTrial) {
    browserTrial.download();
    return;
  }
  $("resultsDialog").close();
  if (folderToShow) openFolder(folderToShow);
};
document.querySelectorAll(".close-dialog").forEach(b => b.onclick = () => b.closest("dialog").close());
document.addEventListener("keydown", e => {
  if (busy || document.querySelector("dialog[open]") || !defaults) return;
  const modifier = e.ctrlKey || e.metaKey,
    k = e.key.toLowerCase(),
    typing = e.target.matches("textarea,input:not([type=range]),[contenteditable=true]");
  if (typing) return;
  if (modifier && k === "o") {
    e.preventDefault();
    showFolders();
    return;
  }
  if (modifier && k === "s") {
    e.preventDefault();
    saveAll().catch(fail);
    return;
  }
  if (modifier && k === "z" && !e.repeat) {
    e.preventDefault();
    undo();
    return;
  }
  if (!active() || e.altKey) return;
  if ((k === "=" || k === "+" || k === "-") && viewport.box) {
    e.preventDefault();
    viewport.step(k === "-" ? -1 : 1);
    return;
  }
  if (modifier && k === "0") {
    e.preventDefault();
    viewport.setZoom("fit");
    return;
  }
  if (modifier) return;
  if (e.target.tagName === "SELECT" && ["arrowleft", "arrowright", "arrowup", "arrowdown", " "].includes(
      k)) return;
  if (k === "arrowleft" || k === "arrowright") {
    e.preventDefault();
    movePhoto(k === "arrowright" ? 1 : -1);
  } else if (k === " " && viewport.box) {
    e.preventDefault();
    if (!e.repeat) {
      viewport.space = true;
      viewport.usedSpace = false;
      viewport.schedule();
    }
  } else if (k === "r" && !e.repeat) {
    e.preventDefault();
    startCrop();
  } else if (k === "enter" && viewport.draft) {
    e.preventDefault();
    finishCrop(true);
  } else if (k === "escape" && viewport.draft) {
    e.preventDefault();
    finishCrop(false);
  } else if (k === "delete" && !e.repeat) {
    e.preventDefault();
    trash();
  }
});
document.addEventListener("keyup", e => {
  if (e.key !== " " || !viewport.space) return;
  e.preventDefault();
  const didPan = viewport.usedSpace;
  viewport.space = false;
  viewport.usedSpace = false;
  if (!didPan && !busy && !document.querySelector("dialog[open]")) viewport.toggle();
  viewport.schedule();
});
window.addEventListener("beforeunload", e => {
  if (busy || viewport.draft || [...records.values()].some(dirty)) {
    e.preventDefault();
    e.returnValue = "";
  }
});
window.addEventListener("pagehide", () => {
  if (apiKey) navigator.sendBeacon("/api/closed", JSON.stringify({
    key: apiKey
  }));
});
setInterval(() => {
  if (apiKey) api("alive").catch(() => {});
}, 5000);
async function boot() {
  const data = await api("boot");
  defaults = data.defaults;
  places = data.places;
  rawAvailable = data.raw;
  createAdjustments();
  for (const place of places) {
    $("placeRows").append(folderRow(place.name, place.path, openFolder, {
      kind: "drive"
    }));
    $("dialogPlaces").append(folderRow(place.name, place.path, browseTo, {
      kind: "drive"
    }));
  }
  let saved = null;
  try {
    if (!browserTrial) saved = JSON.parse(sessionStorage.getItem("fieldroom-session") || "null");
  } catch {}
  if (saved?.changes)
    for (const p of saved.changes) records.set(p.id, p);
  let start = data.folder;
  if (saved?.path && saved.path !== start.path) {
    try {
      start = await api("folder", {
        path: saved.path
      });
    } catch {}
  }
  applyFolder(start, saved?.id);
  if (browserTrial) browserTrial.ready(openFolder, status);
  else if (!rawAvailable) status("JPEG/TIFF mode · install the RAW package to develop camera RAW files.");
}
boot().catch(error => {
  viewport.clear(error.message);
  fail(error);
  $("openFolder").disabled = true;
  $("saveAll").disabled = true;
});
