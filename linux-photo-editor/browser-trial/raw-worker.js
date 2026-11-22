/* Appended to the pinned dcraw_emu module when a trial opens a camera RAW. */
function unpackPpm(output) {
  let pos = 0;
  const white = n => n === 9 || n === 10 || n === 13 || n === 32;
  function token() {
    while (pos < output.length) {
      if (white(output[pos])) { pos++; continue; }
      if (output[pos] !== 35) break;
      while (pos < output.length && output[pos] !== 10) pos++;
    }
    const from = pos;
    while (pos < output.length && !white(output[pos])) pos++;
    return String.fromCharCode(...output.subarray(from, pos));
  }
  const magic = token(), width = Number(token()), height = Number(token()), max = Number(token());
  if (output[pos++] === 13 && output[pos] === 10) pos++;
  const channels = magic === "P6" ? 3 : magic === "P5" ? 1 : 0;
  const count = width * height;
  if (!channels || max !== 255 || !Number.isSafeInteger(count) || count < 1 ||
      output.length - pos !== count * channels) throw Error("The decoder returned an incomplete image.");
  const rgba = new Uint8ClampedArray(count * 4);
  for (let i = 0; i < rgba.length; i += 4) {
    const red = output[pos++];
    rgba[i] = red; rgba[i+1] = channels === 3 ? output[pos++] : red;
    rgba[i+2] = channels === 3 ? output[pos++] : red; rgba[i+3] = 255;
  }
  return new ImageData(rgba, width, height);
}

self.onmessage = async ({data}) => {
  let log = "";
  try {
    const runtime = await Module({
      noInitialRun: true, locateFile: () => "dcraw_emu.wasm", print: () => {},
      printErr: line => { log = (log + " " + line).slice(-1100); },
      instantiateWasm: (imports, done) => {
        const instance = new WebAssembly.Instance(data.module, imports);
        done(instance, data.module); return instance.exports;
      }
    });
    const src = data.nef ? "/input.nef" : "/input.dng", dest = "/result.ppm";
    runtime.FS.writeFile(src, new Uint8Array(data.bytes), {canOwn:true});
    const code = runtime.callMain(["-w", "-o", "1", "-q", "3", "-g", "2.4", "12.92", "-Z", dest, src]);
    if (code || !runtime.FS.analyzePath(dest).exists) throw Error(log.trim() || "Unsupported RAW compression.");
    const pixels = unpackPpm(runtime.FS.readFile(dest));
    const bitmap = await createImageBitmap(pixels);
    self.postMessage({bitmap}, [bitmap]);
  } catch (error) { self.postMessage({error:String(error.message || error) + (log ? " " + log.trim() : "")}); }
};
