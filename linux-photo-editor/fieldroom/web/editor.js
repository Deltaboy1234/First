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
const smartCrop = {
  ticket: 0,
  controller: null,
  loading: false,
  previewActive: false,
  subjects: [],
  selectedSubject: null,
  selectedSubjects: null,
  choiceSignature: "",
  inputs: new Map()
};

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
async function api(route, data = {}, binary = false, signal) {
  if (browserTrial) return browserTrial.request(route, data);
  const response = await fetch("/api/" + route, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Fieldroom-Key": apiKey
    },
    body: JSON.stringify(data),
    signal
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
  onCropEdit: () => invalidateSmartCrop("Manual crop adjustment. Apply crop to keep it, or Cancel."),
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
  const auto = document.createElement("button");
  auto.id = "autoTone";
  auto.className = "primary";
  auto.textContent = "Auto Tone";
  auto.title = "Analyze this photo locally and suggest conservative adjustments";
  auto.onclick = async () => {
    const p = active();
    if (!p || busy) return;
    auto.disabled = true;
    try {
      const result = await api("auto_tone", {id: p.id});
      // Retain existing crop and detail controls; Auto Tone changes basic sliders only.
      for (const key of ["exposure","contrast","highlights","shadows","whites","blacks"])
        p.recipe[key] = result.recipe[key];
      original = false;
      updateAdjustments();
      mark(p);
      queuePreview();
      queueThumb(p);
      status("Auto Tone applied locally to " + p.name);
    } catch (error) { fail(error); }
    finally { auto.disabled = false; }
  };
  $("adjustments").append(auto);
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
  invalidateSmartCrop("", true);
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
  invalidateSmartCrop("", true);
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
  $("autoTone").disabled = !p || busy;
  syncSmartCropControls();
}

function startCrop() {
  const p = active();
  if (!p || busy) return;
  if (viewport.draft) {
    finishCrop(false);
    return;
  }
  if (!viewport.box) return;
  invalidateSmartCrop("Choose a framing preset, then preview the crop. Your original stays unchanged.");
  original = false;
  viewport.beginCrop(p.recipe.crop);
  viewport.clear("Opening crop…");
  toggleTools();
  queuePreview();
}

function finishCrop(accept) {
  const p = active();
  if (!p || !viewport.draft) return;
  if (accept && (smartCrop.loading || !viewport.box)) {
    status("Wait for the preview, or cancel detection before applying your crop.");
    return;
  }
  invalidateSmartCrop("");
  const rect = viewport.endCrop();
  if (accept) {
    p.recipe.crop = rect;
    mark(p);
    queueThumb(p);
    status(`Crop accepted for ${p.name}`);
  } else {
    status("Crop preview cancelled. Your previous crop is unchanged.");
  }
  toggleTools();
  queuePreview();
}

function setBusy(value) {
  if (value) invalidateSmartCrop("");
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
  if (viewport.draft) {
    status("Apply or cancel the crop preview before saving.");
    return;
  }
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
$("aspect").onchange = e => {
  if (!viewport.draft || busy) return;
  if (smartCrop.loading || smartCrop.previewActive) {
    // Let the detector expand the framing around the subject instead of trimming it.
    return suggestSmartCrop();
  } else {
    invalidateSmartCrop("Aspect ratio changed. Review the crop before applying it.");
    viewport.setAspect(e.target.value);
  }
};
$("resetCrop").onclick = () => {
  if (!viewport.draft || busy) return;
  invalidateSmartCrop("Crop reset to the full photo. Apply crop to keep it, or Cancel.");
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
function syncSmartCropControls() {
  const available = !!active() && !!viewport.draft && !busy;
  const run = $("suggestCrop");
  if (!run) return;
  const noSelection = smartCrop.inputs.size > 0 && smartCrop.selectedSubjects?.length === 0;
  run.disabled = !available || !viewport.box || smartCrop.loading || noSelection || !!browserTrial;
  run.textContent = smartCrop.loading ? "Detecting subject…" : "Preview AI Crop";
  $("smartCropPreset").disabled = !available || !!browserTrial;
  $("smartCropAll").disabled = !available || !!browserTrial;
  for (const input of smartCrop.inputs.values()) input.disabled = !available || !!browserTrial;
  $("cancelDetection").hidden = !smartCrop.loading;
  $("smartCropControls").setAttribute("aria-busy", String(smartCrop.loading));
  $("applyCrop").disabled = !available || !viewport.box || smartCrop.loading;
  $("cancelCrop").disabled = busy;
}

function invalidateSmartCrop(message, clearSubjects = false) {
  smartCrop.ticket++;
  smartCrop.controller?.abort();
  if (smartCrop.loading && $("aspect")) $("aspect").value = viewport.aspect;
  smartCrop.controller = null;
  smartCrop.loading = false;
  smartCrop.previewActive = false;
  if (clearSubjects) {
    smartCrop.subjects = [];
    smartCrop.selectedSubject = null;
    smartCrop.selectedSubjects = null;
    if ($("smartCropSubjects")) updateSmartCropSubjects();
  }
  if ($("smartCropNotice") && message !== undefined) {
    $("smartCropNotice").textContent = browserTrial ?
      "AI Smart Crop is available in the Linux desktop app with its offline models installed. Manual crop works here." : message;
  }
  syncSmartCropControls();
}

function updateSmartCropSubjects() {
  const people = smartCrop.subjects.filter(subject => subject.kind !== "group");
  const list = $("smartCropSubjects");
  const signature = JSON.stringify(people.map(subject => [String(subject.id), subject.label]));
  // Reuse the inputs when only the checked state changes, preserving keyboard
  // focus and rapid selections while earlier preview requests are in flight.
  if (signature !== smartCrop.choiceSignature) {
    smartCrop.choiceSignature = signature;
    smartCrop.inputs.clear();
    list.replaceChildren();
    for (const [index, subject] of people.entries()) {
      const label = document.createElement("label");
      label.className = "smart-crop-person";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.id = `smartCropPerson-${index}`;
      input.value = String(subject.id);
      input.onchange = () => {
        const chosen = new Set(smartCrop.selectedSubjects || []);
        input.checked ? chosen.add(input.value) : chosen.delete(input.value);
        return changeSmartCropSelection(people.filter(person => chosen.has(String(person.id))).map(person => String(person.id)));
      };
      const name = document.createElement("span");
      name.textContent = subject.label || "Detected person";
      label.append(input, name);
      list.append(label);
      smartCrop.inputs.set(String(subject.id), input);
    }
  }
  const selected = new Set(smartCrop.selectedSubjects || []);
  for (const [id, input] of smartCrop.inputs) input.checked = selected.has(id);
  const count = people.filter(person => selected.has(String(person.id))).length;
  $("smartCropAll").checked = people.length > 0 && count === people.length;
  $("smartCropAll").indeterminate = count > 0 && count < people.length;
  $("smartCropSelection").textContent = count === 0 ? "Choose at least one person." :
    count === people.length && count > 1 ? `All ${count} detected people selected.` :
    `${count} of ${people.length} selected.`;
  $("smartCropSubjectField").hidden = people.length < 2;
}

function changeSmartCropSelection(ids) {
  const requestedAspect = $("aspect").value;
  invalidateSmartCrop();
  if (ids.length) $("aspect").value = requestedAspect;
  smartCrop.selectedSubjects = ids;
  smartCrop.selectedSubject = ids.length === 0 ? null : ids.length === 1 ? ids[0] :
    ids.length === smartCrop.inputs.size ? "all" : "group";
  updateSmartCropSubjects();
  syncSmartCropControls();
  if (!ids.length) {
    $("smartCropNotice").textContent = "Choose at least one person to preview an AI crop. Your current crop is unchanged.";
    status("No people selected. Your current crop is unchanged.");
    return;
  }
  return suggestSmartCrop();
}

function validCrop(crop) {
  return Array.isArray(crop) && crop.length === 4 &&
    crop.every(n => typeof n === "number" && Number.isFinite(n) && n >= 0 && n <= 1) &&
    crop[0] < crop[2] && crop[1] < crop[3];
}

async function suggestSmartCrop() {
  const p = active();
  if (!p || busy || !viewport.box || !viewport.draft || browserTrial) return;
  if (smartCrop.inputs.size > 0 && smartCrop.selectedSubjects?.length === 0) return;
  smartCrop.controller?.abort();
  const controller = new AbortController();
  const ticket = ++smartCrop.ticket;
  const id = p.id;
  smartCrop.controller = controller;
  smartCrop.loading = true;
  const notice = $("smartCropNotice");
  notice.textContent = "Detecting the subject on this computer…";
  syncSmartCropControls();
  const current = () => ticket === smartCrop.ticket && active()?.id === id && !!viewport.draft;
  try {
    const result = await api("smart_crop", {
      id,
      preset: $("smartCropPreset").value,
      ...(smartCrop.selectedSubjects?.length ? {subject_ids: [...smartCrop.selectedSubjects]} : {}),
      aspect: $("aspect").value
    }, false, controller.signal);
    if (!current()) return;
    smartCrop.subjects = Array.isArray(result.subjects) ? result.subjects : [];
    smartCrop.selectedSubject = result.selected_subject ?? null;
    smartCrop.selectedSubjects = Array.isArray(result.selected_subjects) ? result.selected_subjects.map(String) :
      result.selected_subject === "all" ? smartCrop.subjects.filter(subject => subject.kind !== "group").map(subject => String(subject.id)) :
      result.selected_subject ? [String(result.selected_subject)] : [];
    updateSmartCropSubjects();
    if (!validCrop(result.crop)) {
      if (result.crop !== null && result.crop !== undefined) throw Error("The crop suggestion was invalid. Your crop is unchanged.");
      smartCrop.previewActive = false;
      $("aspect").value = viewport.aspect;
      notice.textContent = result.warning || "Subject not detected. Your crop is unchanged; adjust it manually or try another framing preset.";
      status(notice.textContent);
      return;
    }
    viewport.draft = [...result.crop];
    const aspect = [...$("aspect").options].some(option => option.value === result.aspect) ? result.aspect : $("aspect").value;
    viewport.aspect = aspect;
    $("aspect").value = aspect;
    viewport.drawCrop();
    smartCrop.previewActive = true;
    const groupSize = smartCrop.selectedSubjects.length > 1 ? smartCrop.selectedSubjects.length : 0;
    const groupNotice = groupSize ? (groupSize === smartCrop.inputs.size ?
      `Group preview includes all ${groupSize} detected people.` : `Group preview includes ${groupSize} selected people.`) : null;
    notice.textContent = [groupNotice, result.warning, "Review the crop, then choose Apply crop or Cancel. You can also move or resize it."].filter(Boolean).join(" ");
    status(result.warning || (groupSize ? "Group crop preview ready. No edit applied yet." : "AI crop preview ready. No edit applied yet."));
  } catch (error) {
    if (!current() || error.name === "AbortError") return;
    $("aspect").value = viewport.aspect;
    notice.textContent = error.message || String(error);
    fail(error);
  } finally {
    if (ticket === smartCrop.ticket) {
      smartCrop.controller = null;
      smartCrop.loading = false;
      syncSmartCropControls();
    }
  }
}

function addSmartCropControls() {
  const panel = $("cropPanel");
  const heading = document.createElement("h3");
  heading.textContent = "AI Smart Crop · Offline";
  const select = document.createElement("select");
  select.id = "smartCropPreset";
  select.setAttribute("aria-label", "AI crop framing");
  for (const [value, label] of [
    ["very-tight", "Very Tight · Face"], ["tight", "Tight · Face and shoulders"],
    ["normal", "Normal · Head and shoulders"], ["loose", "Loose · Environmental"],
    ["full-body", "Full Body · Head to toe"]
  ]) select.add(new Option(label, value));
  select.value = "tight";
  const subjectField = document.createElement("fieldset");
  subjectField.id = "smartCropSubjectField";
  subjectField.hidden = true;
  const subjectLabel = document.createElement("legend");
  subjectLabel.textContent = "Include in crop";
  const allLabel = document.createElement("label");
  allLabel.className = "smart-crop-person smart-crop-all";
  const allInput = document.createElement("input");
  allInput.type = "checkbox";
  allInput.id = "smartCropAll";
  allInput.onchange = () => changeSmartCropSelection(allInput.checked ? [...smartCrop.inputs.keys()] : []);
  const allText = document.createElement("span");
  allText.textContent = "All people";
  allLabel.append(allInput, allText);
  const subjectList = document.createElement("div");
  subjectList.id = "smartCropSubjects";
  const selection = document.createElement("p");
  selection.id = "smartCropSelection";
  selection.setAttribute("aria-live", "polite");
  subjectField.append(subjectLabel, allLabel, subjectList, selection);
  const run = document.createElement("button");
  run.id = "suggestCrop";
  run.textContent = "Preview AI Crop";
  const cancel = document.createElement("button");
  cancel.id = "cancelDetection";
  cancel.textContent = "Cancel detection";
  cancel.hidden = true;
  cancel.onclick = () => {
    invalidateSmartCrop("Detection cancelled. Your crop is unchanged.");
    status("Detection cancelled. Your crop is unchanged.");
  };
  const notice = document.createElement("p");
  notice.id = "smartCropNotice";
  notice.setAttribute("role", "status");
  run.onclick = suggestSmartCrop;
  select.onchange = suggestSmartCrop;
  const container = document.createElement("div");
  container.id = "smartCropControls";
  container.className = "smart-crop-controls";
  container.append(heading, select, subjectField, run, cancel, notice);
  panel.insertBefore(container, panel.querySelector("h3"));
  invalidateSmartCrop("");
}
addSmartCropControls();

function togglePhotoOnly() {
  document.body.classList.toggle("photo-only");
  viewport.schedule();
  window.dispatchEvent(new Event("resize"));
}
const photoOnlyButton = document.createElement("button");
photoOnlyButton.id = "photoOnly";
photoOnlyButton.textContent = "Photo Only (F)";
photoOnlyButton.title = "Toggle distraction-free photo viewing";
photoOnlyButton.onclick = togglePhotoOnly;
document.querySelector(".photo-actions").prepend(photoOnlyButton);
document.addEventListener("keydown", e => {
  if (busy || document.querySelector("dialog[open]") || !defaults) return;
  const modifier = e.ctrlKey || e.metaKey,
    k = e.key.toLowerCase(),
    typing = e.target.matches("textarea,input:not([type=range]):not([type=checkbox]),[contenteditable=true]");
  if (typing) return;
  // Let keyboard activation reach the focused control. In particular, Enter on
  // Preview AI Crop must run detection, not accept the existing draft first.
  if (!modifier && e.target.matches("button,select,input[type=checkbox]") && ["enter", " "].includes(k)) return;
  if (e.target.tagName === "SELECT" && k === "escape") return;
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
  if (k === "f" && !modifier && !e.altKey && !e.repeat) {
    e.preventDefault(); togglePhotoOnly(); return;
  }
  if (k === "escape" && document.body.classList.contains("photo-only")) {
    e.preventDefault(); togglePhotoOnly(); return;
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
