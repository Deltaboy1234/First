const byId = id => document.getElementById(id);
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
const stops = [6.25, 12.5, 25, 33.333, 50, 66.667, 100, 150, 200, 300, 400, 800, 1600];
const asPercent = value => `${Number(value.toFixed(value<10?2:1))}%`;
const tileEdge = 512;

class PhotoViewport {
  constructor({
    loadTile,
    onCrop,
    onCropEdit,
    onError,
    onComparePosition
  }) {
    this.view = byId("viewport");
    this.plane = byId("photoPlane");
    this.layer = byId("tileLayer");
    this.nav = byId("navSurface");
    this.loadTile = loadTile;
    this.onCrop = onCrop;
    this.onCropEdit = onCropEdit;
    this.onError = onError;
    this.onComparePosition = onComparePosition;
    this.comparisonEnabled = false;
    this.comparisonPosition = .5;
    this.comparisonActive = false;
    this.before = null;
    this.mode = "fit";
    this.nearMode = "fit";
    this.detailPercent = 100;
    this.customDetail = false;
    this.cx = .5;
    this.cy = .5;
    this.draft = null;
    this.aspect = "Free";
    this.cache = new Map();
    this.pending = new Set();
    this.failed = new Set();
    this.wanted = new Map();
    this.nodes = new Map();
    this.frame = 0;
    this.space = false;
    this.usedSpace = false;
    this.createComparison();
    this.zoomSelect = byId("zoomSelect");
    for (const [value, label] of [
        ["fit", "Fit"],
        ["fill", "Fill"], ...stops.map(n => [String(n), asPercent(n)])
      ]) {
      this.zoomSelect.add(new Option(label, value));
    }
    this.zoomSelect.onchange = () => {
      this.setZoom(this.zoomSelect.value);
      this.view.focus({
        preventScroll: true
      });
    };
    byId("zoomIn").onclick = () => this.step(1);
    byId("zoomOut").onclick = () => this.step(-1);
    document.querySelectorAll("[data-zoom]").forEach(b => b.onclick = () => this.setZoom(b.dataset.zoom));
    this.bindPointers();
    const observer = new ResizeObserver(() => this.schedule());
    observer.observe(this.view);
    observer.observe(this.nav);
    window.addEventListener("resize", () => this.schedule());
    window.addEventListener("blur", () => {
      this.space = false;
      this.drag = null;
      this.comparePointer = null;
      this.usedSpace = false;
      this.schedule();
    });
  }
  setSource(photo, settings, preview, before = null) {
    // A fresh overview also acknowledges managed AI preparation. Tiles that
    // failed before that preparation completed must be allowed to load again.
    this.failed.clear();
    this.photo = photo;
    this.settings = settings;
    this.version = photo.id + "|" + JSON.stringify(settings);
    this.box = preview.box;
    this.size = preview.size;
    this.previewWidth = preview.pixels[0];
    const url = "data:image/jpeg;base64," + preview.image;
    byId("overviewPhoto").src = url;
    byId("navImage").src = url;
    // The controller delivers the two previews together after its revision check.
    // Reject mismatched framing instead of showing a misleading comparison.
    const matches = (a, b) => Array.isArray(a) && Array.isArray(b) &&
      a.length === b.length && a.every((value, index) => value === b[index]);
    this.before = before?.settings && before.preview?.image &&
      matches(before.preview.box, preview.box) && matches(before.preview.size, preview.size) ? {
        settings: before.settings,
        previewWidth: before.preview.pixels[0],
        version: photo.id + "|" + JSON.stringify(before.settings)
      } : null;
    this.beforeImage.src = this.before ? "data:image/jpeg;base64," + before.preview.image : "";
    this.plane.hidden = false;
    byId("navWrap").hidden = false;
    byId("photoMessage").hidden = true;
    this.render();
  }
  clear(message = "Opening photo…") {
    this.photo = null;
    this.box = null;
    this.drag = null;
    this.before = null;
    this.comparePointer = null;
    this.comparisonActive = false;
    this.beforeOverlay.hidden = true;
    this.comparisonChrome.hidden = true;
    this.beforeImage.src = "";
    this.wanted.clear();
    this.trimNodes();
    this.plane.hidden = true;
    byId("navWrap").hidden = true;
    byId("photoMessage").textContent = message;
    byId("photoMessage").hidden = false;
    byId("zoomReadout").textContent = "—";
    this.view.style.cursor = "default";
  }
  geometry(mode = this.mode) {
    if (!this.box) return null;
    const [l, t, r, b] = this.box, w = this.view.clientWidth, h = this.view.clientHeight;
    if (!w || !h) return null;
    const width = r - l,
      height = b - t,
      dpr = window.devicePixelRatio || 1,
      pad = Math.min(24, w * .035, h * .05);
    const fit = Math.min((w - 2 * pad) / width, (h - 2 * pad) / height);
    let scale;
    if (mode === "fit") scale = fit;
    else if (mode === "fill") scale = Math.max(w / width, h / height);
    else scale = Number(mode) / (100 * dpr);
    const sw = width * scale,
      sh = height * scale;
    let x = sw <= w ? (w - sw) / 2 : clamp(w / 2 - this.cx * sw, w - sw, 0);
    let y = sh <= h ? (h - sh) / 2 : clamp(h / 2 - this.cy * sh, h - sh, 0);
    if (typeof mode === "number" && mode >= 100) {
      // Place native pixels on the physical display grid, even with fractional sidebar widths.
      const origin = this.view.getBoundingClientRect();
      x = Math.round((origin.left + x) * dpr) / dpr - origin.left;
      y = Math.round((origin.top + y) * dpr) / dpr - origin.top;
    }
    return {
      l,
      t,
      r,
      b,
      w,
      h,
      width,
      height,
      dpr,
      scale,
      sw,
      sh,
      x,
      y,
      visible: [clamp(-x / sw, 0, 1), clamp(-y / sh, 0, 1), clamp((w - x) / sw, 0, 1), clamp((h - y) / sh,
        0, 1)]
    };
  }
  createComparison() {
    this.beforeOverlay = document.createElement("div");
    this.beforeOverlay.id = "beforeOverlay";
    this.beforeOverlay.hidden = true;
    this.beforeImage = new Image();
    this.beforeImage.id = "beforeOverview";
    this.beforeImage.alt = "";
    this.beforeImage.draggable = false;
    this.beforeLayer = document.createElement("div");
    this.beforeLayer.id = "beforeTileLayer";
    this.beforeOverlay.append(this.beforeImage, this.beforeLayer);
    this.plane.append(this.beforeOverlay);

    this.comparisonChrome = document.createElement("div");
    this.comparisonChrome.id = "comparisonChrome";
    this.comparisonChrome.hidden = true;
    this.beforeLabel = document.createElement("span");
    this.beforeLabel.className = "comparison-label";
    this.beforeLabel.textContent = "Before";
    this.afterLabel = document.createElement("span");
    this.afterLabel.className = "comparison-label";
    this.afterLabel.textContent = "After";
    const divider = this.comparisonDivider = document.createElement("div");
    divider.id = "comparisonDivider";
    divider.tabIndex = 0;
    divider.setAttribute("role", "slider");
    divider.setAttribute("aria-label", "Before and after split");
    divider.setAttribute("aria-orientation", "horizontal");
    divider.setAttribute("aria-valuemin", "0");
    divider.setAttribute("aria-valuemax", "100");
    divider.setAttribute("aria-valuenow", "50");
    divider.title = "Drag to compare. Left/Right move the divider; Home/End show one side.";
    const grip = document.createElement("span");
    grip.className = "comparison-grip";
    grip.setAttribute("aria-hidden", "true");
    grip.textContent = "↔";
    divider.append(grip);
    this.comparisonChrome.append(this.beforeLabel, this.afterLabel, divider);
    this.view.append(this.comparisonChrome);

    const move = e => {
      const g = this.geometry();
      if (!g || !this.comparisonActive) return;
      const left = Math.max(0, g.x), right = Math.min(g.w, g.x + g.sw);
      if (right > left) this.setComparisonPosition((this.local(e).x - left) / (right - left));
    };
    divider.addEventListener("pointerdown", e => {
      if (e.button !== 0 || !this.comparisonActive) return;
      e.preventDefault();
      e.stopPropagation();
      this.comparePointer = e.pointerId;
      divider.setPointerCapture(e.pointerId);
      divider.focus({preventScroll: true});
      move(e);
    });
    divider.addEventListener("pointermove", e => {
      if (e.pointerId !== this.comparePointer) return;
      e.preventDefault();
      e.stopPropagation();
      move(e);
    });
    const stop = e => {
      if (e.pointerId !== this.comparePointer) return;
      e.stopPropagation();
      this.comparePointer = null;
    };
    divider.addEventListener("pointerup", stop);
    divider.addEventListener("pointercancel", stop);
    divider.addEventListener("lostpointercapture", () => { this.comparePointer = null; });
    divider.addEventListener("keydown", e => {
      const step = e.shiftKey ? .1 : .01;
      const positions = {ArrowLeft: this.comparisonPosition - step,
        ArrowRight: this.comparisonPosition + step, Home: 0, End: 1};
      if (Object.prototype.hasOwnProperty.call(positions, e.key)) {
        e.preventDefault();
        e.stopPropagation();
        this.setComparisonPosition(positions[e.key]);
      } else if ([" ", "Enter", "ArrowUp", "ArrowDown"].includes(e.key)) {
        // These keys belong to the focused comparison control, not photo/crop shortcuts.
        e.preventDefault();
        e.stopPropagation();
      }
    });
  }
  setComparison(enabled) {
    this.comparisonEnabled = !!enabled;
    if (!enabled) this.comparePointer = null;
    this.render();
  }
  setComparisonPosition(value) {
    if (!Number.isFinite(value)) return;
    this.comparisonPosition = clamp(value, 0, 1);
    this.onComparePosition?.(this.comparisonPosition);
    this.render();
  }
  renderComparison(g) {
    this.comparisonActive = !!(g && this.comparisonEnabled && this.before && !this.draft);
    this.beforeOverlay.hidden = !this.comparisonActive;
    this.comparisonChrome.hidden = !this.comparisonActive;
    if (!this.comparisonActive) return;
    const left = Math.max(0, g.x), right = Math.min(g.w, g.x + g.sw),
      top = Math.max(0, g.y), bottom = Math.min(g.h, g.y + g.sh),
      split = left + (right - left) * this.comparisonPosition,
      sourceSplit = clamp((split - g.x) / g.sw, 0, 1);
    this.comparisonSourceSplit = sourceSplit;
    this.beforeOverlay.style.clipPath = `inset(0 ${(1 - sourceSplit) * 100}% 0 0)`;
    Object.assign(this.comparisonDivider.style, {left: split + "px", top: top + "px",
      height: Math.max(0, bottom - top) + "px"});
    this.comparisonDivider.setAttribute("aria-valuenow", String(Math.round(this.comparisonPosition * 100)));
    this.comparisonDivider.setAttribute("aria-valuetext", `${Math.round(this.comparisonPosition * 100)}% before, ${Math.round((1 - this.comparisonPosition) * 100)}% after`);
    Object.assign(this.beforeLabel.style, {left: left + 10 + "px", top: top + 10 + "px"});
    Object.assign(this.afterLabel.style, {right: g.w - right + 10 + "px", top: top + 10 + "px"});
    this.beforeLabel.hidden = split - left < 80;
    this.afterLabel.hidden = right - split < 80;
  }
  anchor(point, g = this.geometry()) {
    return [clamp((point.x - g.x) / g.sw, 0, 1), clamp((point.y - g.y) / g.sh, 0, 1)];
  }
  setZoom(mode, point, remember = true) {
    if (!this.box) return;
    const old = this.geometry();
    if (!old) return;
    const xy = point || {
      x: old.w / 2,
      y: old.h / 2
    };
    const [a, b] = this.anchor(xy, old);
    if (mode === "fit" || mode === "fill") {
      if (remember) this.nearMode = mode;
    } else {
      mode = Number(mode);
      if (!stops.includes(mode)) return;
      if (remember) {
        this.detailPercent = mode;
        this.customDetail = true;
      }
    }
    this.mode = mode;
    const g = this.geometry();
    this.cx = a + (g.w / 2 - xy.x) / g.sw;
    this.cy = b + (g.h / 2 - xy.y) / g.sh;
    if (mode === "fit") {
      this.cx = .5;
      this.cy = .5;
    }
    this.render();
    this.flash();
  }
  toggle(point) {
    if (!this.box) return;
    if (typeof this.mode === "number") {
      this.setZoom(this.nearMode, point, false);
      return;
    }
    const g = this.geometry();
    let target = this.detailPercent;
    // The disposable samples can already fit at or above 100% on a large screen.
    if (!this.customDetail && target <= g.scale * g.dpr * 100 + 1) target = stops.find(n => n > g.scale * g
      .dpr * 100 + 1) || 1600;
    this.setZoom(target, point, false);
  }
  step(direction, point) {
    const g = this.geometry();
    if (!g) return;
    const value = g.scale * g.dpr * 100;
    const levels = stops.map(n => ({
      mode: n,
      value: n
    }));
    for (const mode of ["fit", "fill"]) {
      const gm = this.geometry(mode),
        v = gm.scale * gm.dpr * 100;
      if (!levels.some(x => Math.abs(x.value - v) < .35)) levels.push({
        mode,
        value: v
      });
    }
    levels.sort((a, b) => a.value - b.value);
    const next = direction > 0 ? levels.find(x => x.value > value + .15) : levels.reverse().find(x => x
      .value < value - .15);
    if (next) this.setZoom(next.mode, point);
  }
  flash() {
    const out = byId("zoomFlash"),
      g = this.geometry();
    if (!g) return;
    out.textContent = (this.mode === "fit" ? "Fit · " : this.mode === "fill" ? "Fill · " : "") + asPercent(g
      .scale * g.dpr * 100);
    out.classList.add("visible");
    clearTimeout(this.flashTimer);
    this.flashTimer = setTimeout(() => out.classList.remove("visible"), 660);
  }
  schedule() {
    if (!this.frame) this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.render();
    });
  }
  render() {
    const g = this.geometry();
    if (!g) return;
    this.cx = (g.w / 2 - g.x) / g.sw;
    this.cy = (g.h / 2 - g.y) / g.sh;
    Object.assign(this.plane.style, {
      left: g.x + "px",
      top: g.y + "px",
      width: g.sw + "px",
      height: g.sh + "px"
    });
    this.plane.classList.toggle("pixel-detail", g.scale * g.dpr >= 1.99);
    this.view.style.cursor = this.drag?.kind === "pan" ? "grabbing" : this.space ? "grab" : this.draft ?
      "crosshair" : typeof this.mode === "number" ? "grab" : "zoom-in";
    this.zoomSelect.value = String(this.mode);
    byId("zoomReadout").textContent = asPercent(g.scale * g.dpr * 100);
    document.querySelectorAll("[data-zoom]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset
      .zoom === String(this.mode))));
    const navScale = Math.min(this.nav.clientWidth / g.width, this.nav.clientHeight / g.height);
    const wrap = byId("navWrap");
    wrap.style.width = g.width * navScale + "px";
    wrap.style.height = g.height * navScale + "px";
    this.position(byId("navFrame"), g.visible);
    byId("cropLayer").hidden = !this.draft;
    if (this.draft) this.drawCrop();
    this.renderComparison(g);
    this.planTiles(g);
  }
  position(node, box) {
    const [l, t, r, b] = box;
    Object.assign(node.style, {
      left: (l * 100) + "%",
      top: (t * 100) + "%",
      width: ((r - l) * 100) + "%",
      height: ((b - t) * 100) + "%"
    });
  }
  planTiles(g) {
    const wanted = new Map(),
      density = g.scale * g.dpr,
      items = [];
    const sides = [{side: "after", version: this.version, settings: this.settings,
      previewWidth: this.previewWidth,
      left: this.comparisonActive ? this.comparisonSourceSplit : g.visible[0], right: g.visible[2]}];
    if (this.comparisonActive) sides.push({side: "before", ...this.before,
      left: g.visible[0], right: this.comparisonSourceSplit});
    for (const side of sides) {
      if (side.right <= side.left || !(density > side.previewWidth / g.width * 1.075 ||
        (typeof this.mode === "number" && this.mode >= 100))) continue;
      const step = clamp(2 ** Math.floor(Math.log2(1 / density)), 1, 16),
        span = tileEdge * step;
      const bounds = [g.l + side.left * g.width, g.t + g.visible[1] * g.height, g.l + side.right * g
        .width, g.t + g.visible[3] * g.height
      ];
      const cx = (bounds[0] + bounds[2]) / 2,
        cy = (bounds[1] + bounds[3]) / 2;
      for (let row = Math.floor(bounds[1] / span); row < Math.ceil(bounds[3] / span); row++) {
        for (let column = Math.floor(bounds[0] / span); column < Math.ceil(bounds[2] / span); column++) {
          const x0 = Math.max(column * span, g.l),
            y0 = Math.max(row * span, g.t);
          const x1 = Math.min((column + 1) * span, g.r),
            y1 = Math.min((row + 1) * span, g.b);
          const key = `${side.side === "before" ? "before:" : ""}${side.version}|${step}/${column}/${row}`;
          items.push({
            key,
            side: side.side,
            id: this.photo.id,
            recipe: side.settings,
            column,
            row,
            step,
            box: [(x0 - g.l) / g.width, (y0 - g.t) / g.height, (x1 - g.l) / g.width, (y1 - g.t) / g
              .height
            ],
            distance: Math.abs((x0 + x1) / 2 - cx) + Math.abs((y0 + y1) / 2 - cy)
          });
        }
      }
    }
    items.sort((a, b) => a.distance - b.distance).forEach(tile => wanted.set(tile.key, tile));
    this.wanted = wanted;
    this.trimNodes();
    this.pump();
  }
  trimNodes() {
    for (const [key, node] of this.nodes)
      if (!this.wanted.has(key)) {
        node.remove();
        this.nodes.delete(key);
      }
  }
  attach(tile, url) {
    if (this.nodes.has(tile.key)) return;
    const img = new Image();
    img.alt = "";
    img.draggable = false;
    img.src = url;
    this.position(img, tile.box);
    (tile.side === "before" ? this.beforeLayer : this.layer).append(img);
    this.nodes.set(tile.key, img);
  }
  pump() {
    for (const tile of this.wanted.values()) {
      if (this.cache.has(tile.key)) {
        if (!this.nodes.has(tile.key)) {
          const url = this.cache.get(tile.key);
          this.cache.delete(tile.key);
          this.cache.set(tile.key, url);
          this.attach(tile, url);
        }
        continue;
      }
      if (this.pending.size >= 3 || this.pending.has(tile.key) || this.failed.has(tile.key)) continue;
      this.fetchTile(tile);
    }
  }
  async fetchTile(tile) {
    this.pending.add(tile.key);
    try {
      const {
        id,
        recipe,
        column,
        row,
        step
      } = tile;
      const blob = await this.loadTile({
        id,
        recipe,
        column,
        row,
        step
      });
      const url = URL.createObjectURL(blob);
      this.cache.set(tile.key, url);
      if (this.wanted.has(tile.key)) this.attach(tile, url);
      for (const [key, old] of this.cache) {
        if (this.cache.size <= 96) break;
        if (!this.wanted.has(key)) {
          this.cache.delete(key);
          URL.revokeObjectURL(old);
        }
      }
    } catch (error) {
      this.failed.add(tile.key);
      if (this.wanted.has(tile.key)) this.onError(error);
    } finally {
      this.pending.delete(tile.key);
      this.pump();
    }
  }
  beginCrop(crop) {
    this.previousZoom = {
      mode: this.mode,
      cx: this.cx,
      cy: this.cy
    };
    this.mode = "fit";
    this.cx = .5;
    this.cy = .5;
    this.draft = [...crop];
    this.aspect = "Free";
    byId("aspect").value = "Free";
    byId("cropLayer").hidden = false;
    this.drawCrop();
    this.renderComparison(this.geometry());
  }
  endCrop() {
    const crop = this.draft;
    this.draft = null;
    this.drag = null;
    byId("cropLayer").hidden = true;
    if (this.previousZoom) {
      Object.assign(this, this.previousZoom);
      this.previousZoom = null;
    }
    return crop;
  }
  drawCrop() {
    if (!this.draft || !this.size) return;
    this.position(byId("cropRect"), this.draft);
    const [a, b, c, d] = this.draft;
    this.onCrop(Math.max(1, Math.round(c * this.size[0]) - Math.round(a * this.size[0])), Math.max(1, Math
      .round(d * this.size[1]) - Math.round(b * this.size[1])));
  }
  ratio() {
    if (this.aspect === "Free") return null;
    if (this.aspect === "Original") return 1;
    const [w, h] = this.aspect.split(":").map(Number);
    return w / h * this.size[1] / this.size[0];
  }
  setAspect(label) {
    this.aspect = label;
    const ratio = this.ratio();
    if (!ratio || !this.draft) return;
    const [a, b, c, d] = this.draft, cx = (a + c) / 2, cy = (b + d) / 2;
    const w = Math.min(c - a, (d - b) * ratio),
      h = w / ratio;
    this.draft = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2];
    this.drawCrop();
  }
  local(e) {
    const r = this.view.getBoundingClientRect();
    return {
      x: e.clientX - r.left,
      y: e.clientY - r.top
    };
  }
  cropAt(point, drag) {
    const p = this.anchor(point),
      [a, b, c, d] = drag.crop,
      [ox, oy] = drag.start;
    if (drag.kind === "moveCrop") {
      const dx = clamp(p[0] - ox, -a, 1 - c),
        dy = clamp(p[1] - oy, -b, 1 - d);
      this.draft = [a + dx, b + dy, c + dx, d + dy];
    } else {
      const [x, y] = drag.origin, dx = p[0] - x, dy = p[1] - y;
      const signX = dx === 0 ? (x > .5 ? -1 : 1) : Math.sign(dx),
        signY = dy === 0 ? (y > .5 ? -1 : 1) : Math.sign(dy);
      const maxW = signX > 0 ? 1 - x : x,
        maxH = signY > 0 ? 1 - y : y;
      let w = Math.min(maxW, Math.max(Math.abs(dx), 1 / this.size[0])),
        h = Math.min(maxH, Math.max(Math.abs(dy), 1 / this.size[1]));
      const ratio = this.ratio();
      if (ratio) {
        w = Math.min(Math.max(w, h * ratio), maxW, maxH * ratio);
        h = w / ratio;
      }
      if (w < 1 / this.size[0] || h < 1 / this.size[1]) return;
      this.draft = [Math.min(x, x + signX * w), Math.min(y, y + signY * h), Math.max(x, x + signX * w), Math
        .max(y, y + signY * h)
      ].map(n => clamp(n, 0, 1));
    }
    this.drawCrop();
  }
  bindPointers() {
    this.view.addEventListener("pointerdown", e => {
      if (!this.box || this.drag || e.button > 1) return;
      const g = this.geometry(),
        point = this.local(e),
        inPhoto = point.x >= g.x && point.x <= g.x + g.sw && point.y >= g.y && point.y <= g.y + g.sh;
      if (!inPhoto) return;
      e.preventDefault();
      this.view.focus({
        preventScroll: true
      });
      const start = this.anchor(point, g);
      this.drag = {
        pointer: e.pointerId,
        kind: "view",
        point,
        start,
        cx: this.cx,
        cy: this.cy,
        moved: false
      };
      if (e.button === 1 || this.space) {
        this.drag.kind = "pan";
        if (this.space) this.usedSpace = true;
      } else if (this.draft) {
        // A user's drag takes precedence over any inference still in flight.
        this.onCropEdit?.();
        const [a, b, c, d] = this.draft, [x, y] = start;
        this.drag.crop = [...this.draft];
        const corner = e.target.closest("[data-corner]")?.dataset.corner;
        if (corner) {
          this.drag.kind = "sizeCrop";
          this.drag.origin = [corner.includes("w") ? c : a, corner.includes("n") ? d : b];
        } else if (x > a && x < c && y > b && y < d && (a > 0 || b > 0 || c < 1 || d < 1)) {
          this.drag.kind = "moveCrop";
        } else {
          this.drag.kind = "sizeCrop";
          this.drag.origin = start;
        }
      }
      this.view.setPointerCapture(e.pointerId);
    });
    this.view.addEventListener("pointermove", e => {
      const d = this.drag;
      if (!d || d.pointer !== e.pointerId) return;
      const p = this.local(e),
        dx = p.x - d.point.x,
        dy = p.y - d.point.y;
      if (!d.moved && Math.hypot(dx, dy) < 3) return;
      d.moved = true;
      if (d.kind === "view") d.kind = "pan";
      if (d.kind === "pan") {
        const g = this.geometry();
        if (!g) return;
        this.cx = d.cx - dx / g.sw;
        this.cy = d.cy - dy / g.sh;
        this.schedule();
      } else this.cropAt(p, d);
    });
    this.view.addEventListener("pointerup", e => {
      const d = this.drag;
      if (!d || d.pointer !== e.pointerId) return;
      this.drag = null;
      if (!d.moved && d.kind === "view") this.toggle(d.point);
      else this.schedule();
    });
    this.view.addEventListener("pointercancel", () => {
      this.drag = null;
      this.schedule();
    });
    this.view.addEventListener("lostpointercapture", () => {
      this.drag = null;
    });
    let wheel = 0,
      lastWheel = 0;
    this.view.addEventListener("wheel", e => {
      if (!this.box) return;
      e.preventDefault();
      const delta = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? this.view.clientHeight :
      1);
      if (Math.sign(delta) !== Math.sign(wheel) || performance.now() - lastWheel > 250) wheel = 0;
      lastWheel = performance.now();
      wheel += delta;
      if (Math.abs(wheel) >= (e.ctrlKey ? 6 : 32)) {
        this.step(wheel < 0 ? 1 : -1, this.local(e));
        wheel = 0;
      }
    }, {
      passive: false
    });
    const navMove = e => {
      const r = byId("navWrap").getBoundingClientRect();
      if (!this.box || !r.width || !r.height) return;
      const x = clamp((e.clientX - r.left) / r.width, 0, 1),
        y = clamp((e.clientY - r.top) / r.height, 0, 1);
      if (this.mode === "fit") this.toggle();
      this.cx = x;
      this.cy = y;
      this.render();
    };
    this.nav.onpointerdown = e => {
      if (!this.box || e.button !== 0) return;
      e.preventDefault();
      this.nav.setPointerCapture(e.pointerId);
      navMove(e);
    };
    this.nav.onpointermove = e => {
      if (this.nav.hasPointerCapture(e.pointerId)) navMove(e);
    };
  }
}
export {
  PhotoViewport
};
