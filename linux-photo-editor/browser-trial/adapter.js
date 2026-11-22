/* Browser-only processing for the downloadable trial. Files stay in this tab. */
(() => {
  const get = id => document.getElementById(id);
  const defaults = {exposure: 0, contrast: 0, highlights: 0, shadows: 0, whites: 0,
    blacks: 0, warmth: 0, saturation: 0, luminance: 0, color: 0, sharpen: 0,
    radius: 1, crop: [0, 0, 1, 1]};
  const samplePath = "/Sample roll";
  const photos = new Map(), folders = new Map(), decoded = new Map();
  let seq = 0, open, message, entries = [], zipUrl = null;
  const copy = obj => structuredClone(obj);
  const tail = path => path.split("/").pop() || "Your folders";
  function parent(path) {
    return path.slice(0, path.lastIndexOf("/")) || "/";
  }
  function addFolder(path) {
    if (folders.has(path)) return;
    folders.set(path, []);
    if (path !== "/") addFolder(parent(path));
  }
  function addPhoto(path, name, source, credit = "") {
    addFolder(path);
    const p = {id: "photo-" + (++seq), folder: path, name, source, credit, hidden: false,
      recipe: copy(defaults)};
    photos.set(p.id, p);
    folders.get(path).push(p.id);
  }
  function listing(path) {
    if (!folders.has(path)) throw Error("Choose that folder again with Open photos.");
    const visible = folders.get(path).map(id => photos.get(id)).filter(p => !p.hidden);
    return {path, parent: parent(path), name: tail(path),
      folders: [...folders.keys()].filter(k => k !== path && parent(k) === path)
        .map(k => ({name: tail(k), path: k})),
      photos: visible.sort((a,b) => a.name.localeCompare(b.name, undefined, {numeric:true})).map(p =>
        ({id:p.id, name:p.name, folder:p.folder, recipe:copy(p.recipe)}))};
  }
  function surface(w, h) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, w); canvas.height = Math.max(1, h);
    return canvas;
  }
  const toBlob = (canvas, type, quality) => new Promise((resolve, reject) =>
    canvas.toBlob(b => b ? resolve(b) : reject(Error("The browser could not encode this image.")), type, quality));
  function loadImage(p) {
    if (!decoded.has(p.id)) {
      const pending = (async () => {
        let source = p.source;
        if (FieldroomRaw.isRaw(p.name)) {
          try {
            const bitmap = await FieldroomRaw.decode(source);
            p.credit = "Developed from RAW sensor data";
            p.previewOnly = false;
            p.rawFailure = "";
            return bitmap;
          } catch (error) {
            const preview = await FieldroomRaw.embedded(source);
            if (!preview) throw Error("Cannot develop " + p.name + ": " + error.message);
            p.previewOnly = true;
            p.rawFailure = error.message;
            p.credit = "Embedded preview only · full RAW export unavailable";
            source = preview;
          }
        }
        const img = new Image(), isFile = source instanceof Blob;
        const url = isFile ? URL.createObjectURL(source) : source;
        try { img.src = url; await img.decode(); return img; }
        catch { throw Error("This browser could not decode " + p.name + ". Try a JPEG, PNG or WebP."); }
        finally { if (isFile) URL.revokeObjectURL(url); }
      })();
      decoded.set(p.id, pending);
      if (decoded.size > 3) decoded.delete(decoded.keys().next().value);
    }
    return decoded.get(p.id);
  }
  function bounds(img, crop) {
    const w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
    const l = Math.min(w - 1, Math.round(w * crop[0])), t = Math.min(h - 1, Math.round(h * crop[1]));
    return [l, t, Math.max(l + 1, Math.round(w * crop[2])), Math.max(t + 1, Math.round(h * crop[3]))];
  }
  function toneTable(gain) {
    return Float32Array.from({length:256}, (_, i) => {
      const v = i / 255, lin = (v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4) * gain;
      return lin <= .0031308 ? lin * 12.92 : 1.055 * lin ** (1 / 2.4) - .055;
    });
  }
  function develop(canvas, r) {
    const tonal = ["exposure","contrast","highlights","shadows","whites","blacks","warmth","saturation"].some(k => r[k]);
    const detail = r.luminance || r.color || r.sharpen;
    if (!tonal && !detail) return;
    const ctx = canvas.getContext("2d", {willReadFrequently:true});
    const gain = 2 ** r.exposure, warm = .0024 * r.warmth;
    const tables = [toneTable(gain * (1 + warm)), toneTable(gain), toneTable(gain * (1 - warm))];
    const contrast = 1 + r.contrast * .008, sat = 1 + r.saturation * .01;
    const w = canvas.width, h = canvas.height, radius = Math.max(1, Math.round(r.radius));
    let previousRows;
    for (let top = 0; top < h; top += 160) {
      const rows = Math.min(160, h - top), start = Math.max(0, top - radius);
      const extra = Math.min(h, top + rows + radius) - start;
      const band = ctx.getImageData(0, start, w, extra), data = band.data;
      if (previousRows) data.set(previousRows);
      const from = (top - start) * w * 4, end = from + rows * w * 4;
      previousRows = detail ? data.slice(end - Math.min(radius, rows) * w * 4, end) : null;
      const raw = detail ? data.slice() : data;
      for (let i = from; i < end; i += 4) {
        let red = raw[i], green = raw[i + 1], blue = raw[i + 2];
        if (detail) {
          const x = i / 4 % w, y = Math.floor(i / 4 / w);
          const west = i - Math.min(x, radius) * 4, east = i + Math.min(w-1-x, radius) * 4;
          const up = i - Math.min(y, radius) * w * 4, down = i + Math.min(extra-1-y, radius) * w * 4;
          const blur = c => (raw[west+c]+raw[east+c]+raw[up+c]+raw[down+c]+4*raw[i+c])/8;
          const br=blur(0), bg=blur(1), bb=blur(2);
          const delta = (red-br)*.2126 + (green-bg)*.7152 + (blue-bb)*.0722;
          const light = delta * (.023*r.sharpen - .01*r.luminance);
          const chroma = .01*r.color;
          red += light + chroma * (br - red + delta);
          green += light + chroma * (bg - green + delta);
          blue += light + chroma * (bb - blue + delta);
        }
        if (tonal) {
          const byte = v => Math.max(0, Math.min(255, Math.round(v)));
          red = tables[0][byte(red)]; green = tables[1][byte(green)]; blue = tables[2][byte(blue)];
          const y = Math.max(0, Math.min(1, red*.2126 + green*.7152 + blue*.0722)), s = 1-y;
          const lift = .014*(r.shadows*s*s*y + r.highlights*y*y*s) + .002*(r.whites*y**4 + r.blacks*s**4);
          red=.45+(red+lift-.45)*contrast; green=.45+(green+lift-.45)*contrast; blue=.45+(blue+lift-.45)*contrast;
          const gray=red*.2126+green*.7152+blue*.0722;
          red=255*(gray+(red-gray)*sat); green=255*(gray+(green-gray)*sat); blue=255*(gray+(blue-gray)*sat);
        }
        data[i]=red; data[i+1]=green; data[i+2]=blue;
      }
      ctx.putImageData(band, 0, start, 0, top-start, w, rows);
    }
  }
  function render(img, box, r, maxEdge = Infinity) {
    const [l,t,right,bottom]=box, w=right-l, h=bottom-t, scale=Math.min(1,maxEdge/Math.max(w,h));
    const out=surface(Math.round(w*scale),Math.round(h*scale)), ctx=out.getContext("2d",{willReadFrequently:true});
    ctx.fillStyle="white"; ctx.fillRect(0,0,out.width,out.height);
    ctx.drawImage(img,l,t,w,h,0,0,out.width,out.height);
    develop(out,r);
    return out;
  }
  function tile(img, box, r, column, row, step) {
    const span=512*step, x=Math.max(box[0],column*span), y=Math.max(box[1],row*span);
    const right=Math.min(box[2],(column+1)*span), bottom=Math.min(box[3],(row+1)*span);
    const pad=Math.ceil(r.radius)+1, l=Math.max(box[0],x-pad), t=Math.max(box[1],y-pad);
    const buffered=render(img,[l,t,Math.min(box[2],right+pad),Math.min(box[3],bottom+pad)],r);
    const out=surface(Math.ceil((right-x)/step),Math.ceil((bottom-y)/step));
    out.getContext("2d").drawImage(buffered,x-l,y-t,right-x,bottom-y,0,0,out.width,out.height);
    return toBlob(out,"image/png");
  }
  function histogram(canvas) {
    const small=surface(160,100), ctx=small.getContext("2d",{willReadFrequently:true});
    ctx.drawImage(canvas,0,0,160,100);
    const d=ctx.getImageData(0,0,160,100).data, h=Array.from({length:3},()=>Array(64).fill(0));
    for(let i=0;i<d.length;i+=4) for(let c=0;c<3;c++) h[c][d[i+c]>>2]++;
    return h;
  }
  const crcTable=Uint32Array.from({length:256},(_,n)=>{
    for(let i=0;i<8;i++) n=(n&1) ? 0xedb88320^(n>>>1) : n>>>1;
    return n>>>0;
  });
  async function entry(name,blob) {
    let crc=0xffffffff;
    const bytes=new Uint8Array(await blob.arrayBuffer());
    for(const b of bytes) crc=crcTable[(crc^b)&255]^(crc>>>8);
    return {name:new TextEncoder().encode(name), blob, crc:(crc^0xffffffff)>>>0};
  }
  function zip(files) {
    const chunks=[], directory=[];
    const header = size => {const bytes=new Uint8Array(size);return [bytes,new DataView(bytes.buffer)];};
    let offset=0,centralSize=0;
    if (files.length>65535) throw Error("This ZIP format supports up to 65,535 files.");
    for(const file of files) {
      const n=file.name.length, s=file.blob.size;
      if(offset+s+30+n>0xffffffff) throw Error("Export fewer than 4 GB at once in the browser trial.");
      const [a,v]=header(30),[b,q]=header(46);
      v.setUint32(0,0x04034b50,true); v.setUint16(4,20,true); v.setUint16(6,0x800,true);
      v.setUint16(12,33,true); v.setUint32(14,file.crc,true); v.setUint32(18,s,true); v.setUint32(22,s,true); v.setUint16(26,n,true);
      q.setUint32(0,0x02014b50,true); q.setUint16(4,20,true); q.setUint16(6,20,true); q.setUint16(8,0x800,true);
      q.setUint16(14,33,true); q.setUint32(16,file.crc,true); q.setUint32(20,s,true); q.setUint32(24,s,true);
      q.setUint16(28,n,true); q.setUint32(42,offset,true);
      chunks.push(a,file.name,file.blob); directory.push(b,file.name);
      offset+=30+n+s; centralSize+=46+n;
    }
    const [last,v]=header(22); v.setUint32(0,0x06054b50,true); v.setUint16(8,files.length,true);
    v.setUint16(10,files.length,true); v.setUint32(12,centralSize,true); v.setUint32(16,offset,true);
    return new Blob([...chunks,...directory,last],{type:"application/zip"});
  }
  const exportNames = new Set();
  function outputName(p) {
    const stem=p.name.replace(/\.[^.]+$/,"").replace(/[\\/:*?"<>|\x00-\x1f]/g,"_") || "Photo";
    let name=stem+".jpg", n=1;
    while(exportNames.has(name.toLowerCase())) name=stem+" ("+(++n)+").jpg";
    exportNames.add(name.toLowerCase());
    return "Edited/"+name;
  }
  async function request(route, data) {
    if(route==="boot") return {places:[{name:"Sample roll",path:samplePath},{name:"Your folders",path:"/"}],defaults:copy(defaults),
      raw:true,folder:listing(samplePath)};
    if(route==="folder" || route==="directories") return listing(data.path);
    const p=photos.get(data.id || data.ticket);
    if(!p) throw Error("Choose the photo again.");
    if(route==="trash") {p.hidden=true;return {ticket:p.id};}
    if(route==="undo") {p.hidden=false;return {id:p.id,folder:p.folder,name:p.name};}
    const r=data.recipe || defaults;
    if (route==="thumbnail" && FieldroomRaw.isRaw(p.name) && !decoded.has(p.id) &&
        JSON.stringify(r)===JSON.stringify(defaults)) {
      const embedded=await FieldroomRaw.embedded(p.source);
      if (embedded) return embedded;
    }
    const img=await loadImage(p), box=bounds(img,r.crop);
    if(route==="tile") return tile(img,box,r,data.column,data.row,data.step);
    if(route==="thumbnail") return toBlob(render(img,box,r,210),"image/jpeg",.8);
    if(route==="preview") {
      const out=render(img,box,r,1440);
      return {size:[img.naturalWidth||img.width,img.naturalHeight||img.height], box, pixels:[out.width,out.height], histogram:histogram(out),
        metadata:{camera:p.credit,rawError:p.rawFailure}, image:out.toDataURL("image/jpeg",.92).split(",")[1]};
    }
    if(route==="export") {
      if(p.previewOnly) throw Error("Only the camera's embedded preview could be opened. Full-resolution RAW export is unavailable for this compression.");
      const path=outputName(p), blob=await toBlob(render(img,box,r),"image/jpeg",.95);
      entries.push(await entry(path,blob)); p.recipe=copy(r);
      return {path,skipped:false};
    }
    throw Error("Unknown browser action.");
  }
  function importFiles(files) {
    if (!files.length) return;
    const valid = [...files].filter(f => /\.(dng|nef|jpe?g|png|webp|bmp|avif)$/i.test(f.name));
    if(!valid.length) { get("importNote").textContent="Choose a Nikon NEF, Adobe DNG, JPEG, PNG, WebP, BMP or AVIF.";
      if (!get("trialFiles").open) get("trialFiles").showModal(); return; }
    const folderName=valid[0].webkitRelativePath?.split("/")[0] || "Selected photos";
    let base="/"+folderName, n=1;
    while(folders.has(base)) base="/"+folderName+" ("+(++n)+")";
    for(const file of valid) {
      const subs=(file.webkitRelativePath||"").split("/").slice(1,-1);
      addPhoto([base,...subs].join("/"),file.name,file);
    }
    get("trialFiles").close();
    const first=folders.get(base)?.length ? base : [...folders.keys()].find(k=>k.startsWith(base+"/")&&folders.get(k).length);
    open(first || base);
  }
  for(const item of JSON.parse(get("trialSamples").textContent)) addPhoto(samplePath,item.name,item.url,item.credit);
  globalThis.fieldroomTrial = {
    request,
    startExport() { entries=[]; exportNames.clear(); },
    finishExport() { if(zipUrl) URL.revokeObjectURL(zipUrl); zipUrl=URL.createObjectURL(zip(entries)); entries=[]; },
    download() { if(!zipUrl)return; const a=document.createElement("a"); a.href=zipUrl; a.download="Fieldroom-Edited.zip";
      document.body.append(a); a.click(); a.remove(); },
    showFiles() { get("importNote").textContent="Nikon NEF, Adobe DNG, JPEG, PNG, WebP, BMP or AVIF. Selected photos stay in this tab.";
      if(!get("trialFiles").open) get("trialFiles").showModal(); },
    ready(openFolder, status) {
      open=openFolder; message=status;
      for(const name of ["trialPhotos","trialFolder"]) {
        const input=get(name);
        input.onchange=()=>{importFiles(input.files); input.value="";};
      }
      get("pickPhotos").onclick=()=>get("trialPhotos").click();
      get("pickFolder").onclick=()=>get("trialFolder").click();
      get("dismissImport").onclick=()=>get("trialFiles").close();
      window.addEventListener("dragover",e=>{if(e.dataTransfer.types.includes("Files")) e.preventDefault();});
      window.addEventListener("drop",e=>{if(e.dataTransfer?.files.length){e.preventDefault();importFiles(e.dataTransfer.files);}});
      message("RAW browser trial · Drop a DNG, NEF or JPEG. Click the photo to zoom.");
    }
  };
})();
