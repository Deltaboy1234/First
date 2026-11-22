/* RAWs develop locally, one at a time, in short-lived workers that release their WASM heap. */
const FieldroomRaw = (() => {
  const isRaw = name => /\.(dng|nef)$/i.test(name);
  let compiled, workerURL, tail = Promise.resolve();
  function engine() {
    if (!compiled) {
      const encoded = document.getElementById("rawWasm").textContent.trim();
      const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
      compiled = WebAssembly.compile(bytes);
    }
    if (!workerURL) workerURL = URL.createObjectURL(new Blob([document.getElementById("rawWorker").textContent],
      {type:"text/javascript"}));
    return compiled;
  }
  async function decode(file) {
    const module = await engine(), bytes = await file.arrayBuffer();
    const worker = new Worker(workerURL);
    try {
      return await new Promise((resolve,reject) => {
        worker.onmessage = ({data}) => data.bitmap ? resolve(data.bitmap) : reject(Error(data.error));
        worker.onerror = e => reject(Error(e.message || "The RAW decoder could not start."));
        worker.onmessageerror = () => reject(Error("The RAW decoder could not return this photo."));
        worker.postMessage({module,bytes,nef:/\.nef$/i.test(file.name)},[bytes]);
      });
    } finally {worker.terminate();}
  }
  function queued(file) {
    const next = tail.then(() => decode(file));
    tail = next.catch(() => {});
    return next;
  }
  // Process JPEG segments and validate their frame dimensions. A RAW strip using
  // lossless JPEG (SOF3) is sensor data and must never be presented as a photograph.
  function embeddedCandidates(bytes) {
    const candidates = [];
    for (let i=0;i+10<bytes.length;i++) {
      if (bytes[i]!==255 || bytes[i+1]!==216 || bytes[i+2]!==255) continue;
      let at=i+2, width=0, height=0, end=0;
      while (at+3<bytes.length && bytes[at]===255) {
        while (bytes[at]===255) at++;
        const marker=bytes[at++];
        if(marker===217) {end=at;break;}
        if(marker===218) {
          for(let k=at+2;k+1<bytes.length;k++) {
            if(bytes[k]===255 && bytes[k+1]===217) {end=k+2;break;}
          }
          break;
        }
        const length=bytes[at]*256+bytes[at+1];
        if(length<2 || at+length>bytes.length || marker===195) break;
        if([192,193,194].includes(marker) && length>=8) {
          height=bytes[at+3]*256+bytes[at+4]; width=bytes[at+5]*256+bytes[at+6];
        }
        at+=length;
      }
      if(width && height && end>i) {
        candidates.push({start:i,end,area:width*height});
        i=end-1;
      }
    }
    return candidates.sort((a,b)=>b.area-a.area);
  }
  const previews=new WeakMap();
  function embedded(file) {
    if (!previews.has(file)) previews.set(file,(async()=>{
      const bytes=new Uint8Array(await file.arrayBuffer());
      for(const frame of embeddedCandidates(bytes)) {
        const blob=file.slice(frame.start,frame.end,"image/jpeg");
        try {const bitmap=await createImageBitmap(blob);bitmap.close();return blob;} catch {}
      }
      return null;
    })());
    return previews.get(file);
  }
  return {isRaw,decode:queued,embedded};
})();
