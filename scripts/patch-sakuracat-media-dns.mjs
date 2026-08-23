#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const ASAR_PATH =
  process.env.SAKURACAT_ASAR_PATH ||
  "/Applications/SakuraCat.app/Contents/Resources/app.asar";
const MAIN_ENTRY = "dist/main/main.js";
const BACKUP_PATH = path.join(
  os.homedir(),
  "Library/Application Support/sakuracat/app.asar.before-media-dns",
);
const dryRun = process.argv.includes("--dry-run");

function readArchive(archivePath) {
  const fd = fs.openSync(archivePath, "r");
  try {
    const prefix = Buffer.alloc(16);
    fs.readSync(fd, prefix, 0, prefix.length, 0);
    const headerJsonLength = prefix.readUInt32LE(12);
    const headerBuffer = Buffer.alloc(headerJsonLength);
    fs.readSync(fd, headerBuffer, 0, headerBuffer.length, 16);
    const header = JSON.parse(headerBuffer.toString("utf8"));
    const dataOffset = 8 + prefix.readUInt32LE(4);
    return { fd, prefix, headerJsonLength, header, dataOffset };
  } catch (error) {
    fs.closeSync(fd);
    throw error;
  }
}

function getEntry(header, entryPath) {
  let entry = header;
  for (const segment of entryPath.split("/")) {
    entry = entry?.files?.[segment];
    if (!entry) throw new Error(`ASAR entry not found: ${entryPath}`);
  }
  return entry;
}

function digest(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

function buildHook() {
  return `(function(){
try{
var f=require('fs'),h=require('http'),L='/tmp/sakuracat-tun-fix.log',H=['la.btc620.com','+.rsc.cdn77.org'],R=['DOMAIN,la.btc620.com,real-ip','DOMAIN-SUFFIX,rsc.cdn77.org,real-ip'];
function n(x){try{f.appendFileSync(L,new Date().toISOString()+' '+x+'\\n')}catch(e){}}
function t(p){p=String(p);return p.indexOf('com.vortex.helper')>=0&&p.indexOf('config.yaml')>=0}
function u(){try{return JSON.parse(f.readFileSync(process.env.HOME+'/Library/Application Support/sakuracat/vortex.json'))}catch(e){return{}}}
function z(s){var o=s,c=0,a;if(u().tun===true){a=o.replace(/(^|\\n)([ \\t]*tun:[ \\t]*\\n[ \\t]*enable:[ \\t]*)false/,'$1$2true');if(a!==o)o=a,c=1;a=o.replace(/(^|\\n)([ \\t]*tun:[ \\t]*\\{[^}\\n]*enable:[ \\t]*)false/,'$1$2true');if(a!==o)o=a,c=1}if(/(^|\\n)ipv6:[ \\t]*true/.test(o))o=o.replace(/(^|\\n)([ \\t]*ipv6:[ \\t]*)true/,'$1$2false'),c=1;else if(!/(^|\\n)ipv6:[ \\t]*/.test(o)){a=o.replace(/(^mixed-port:[^\\n]*\\n)/,'$1ipv6: false\\n');if(a!==o)o=a,c=1}if(!/(^|\\n)[ \\t]*fake-ip-filter-mode:[ \\t]*whitelist/.test(o)){var q=/(^|\\n)[ \\t]*fake-ip-filter-mode:[ \\t]*rule/.test(o)?R:H,m=/(^|\\n)([ \\t]*)fake-ip-filter:[ \\t]*\\n/.exec(o),d='';if(m){q.forEach(function(r){if(o.indexOf('- "'+r+'"')<0&&o.indexOf('- '+r+'\\n')<0)d+=m[2]+'  - "'+r+'"\\n'})}else{m=/(^|\\n)([ \\t]*)enhanced-mode:[ \\t]*fake-ip[ \\t]*\\n/.exec(o);if(m){d=m[2]+'fake-ip-filter:\\n';q.forEach(function(r){d+=m[2]+'  - "'+r+'"\\n'})}}if(m&&d){a=m.index+m[0].length;o=o.slice(0,a)+d+o.slice(a);c=1}}return c?o:null}
function P(){try{var m=/-ext-ctl[= ]127\\.0\\.0\\.1:(\\d+)/.exec(require('child_process').execSync('ps -Ao args=',{encoding:'utf8'}));if(m)return +m[1]}catch(e){}return 39798}
function j(o,p,m,b){try{b=b?JSON.stringify(b):'';var r=h.request({host:'127.0.0.1',port:o,path:p,method:m,headers:b?{'Content-Type':'application/json','Content-Length':Buffer.byteLength(b)}:{}},function(x){x.resume()});r.on('error',function(){});r.end(b)}catch(e){}}
function y(){var o=P();j(o,'/configs','PATCH',{ipv6:false});j(o,'/cache/fakeip/flush','POST');j(o,'/cache/dns/flush','POST')}
var w=f.writeFileSync;f.writeFileSync=function(p,b){if(t(p)){try{var B=Buffer.isBuffer(b)||b instanceof Uint8Array,s=B?Buffer.from(b).toString():typeof b==='string'?b:null,x=s===null?null:z(s);if(x!==null)arguments[1]=B?Uint8Array.from(Buffer.from(x)):x,n('[HOOK8 CONFIG FIXED]')}catch(e){}setTimeout(y,2500)}return w.apply(f,arguments)};
setTimeout(function(){try{var p=process.env.HOME+'/.config/com.vortex.helper/config.yaml',x=z(f.readFileSync(p,'utf8'));if(x!==null)w(p,x),n('[HOOK8 CURRENT CONFIG FIXED]');y()}catch(e){}},800);n('[HOOK8 MEDIA DNS LOADED] '+(process.type||'main'));
}catch(e){}
})();

`;
}

const archive = readArchive(ASAR_PATH);
try {
  const entry = getEntry(archive.header, MAIN_ENTRY);
  const mainBuffer = Buffer.alloc(entry.size);
  fs.readSync(
    archive.fd,
    mainBuffer,
    0,
    mainBuffer.length,
    archive.dataOffset + Number(entry.offset),
  );

  const mainText = mainBuffer.toString("utf8");
  if (!mainText.includes("sakuracat-tun-fix.log")) {
    throw new Error("Existing SakuraCat TUN hook was not found; refusing to patch an unknown build");
  }
  const hookBoundary = mainText.indexOf("\n\n(function(", 1) + 2;
  if (hookBoundary < 100 || hookBoundary > 10_000) {
    throw new Error("Could not locate the existing hook boundary safely");
  }

  const hook = Buffer.from(buildHook(), "utf8");
  if (hook.length > hookBoundary) {
    throw new Error(`New hook is ${hook.length} bytes; only ${hookBoundary} bytes are available`);
  }

  const updatedMain = Buffer.from(mainBuffer);
  updatedMain.fill(0x20, 0, hookBoundary);
  hook.copy(updatedMain, 0);
  updatedMain[hookBoundary - 2] = 0x0a;
  updatedMain[hookBoundary - 1] = 0x0a;

  const blockSize = entry.integrity?.blockSize || 4 * 1024 * 1024;
  const blocks = [];
  for (let offset = 0; offset < updatedMain.length; offset += blockSize) {
    blocks.push(digest(updatedMain.subarray(offset, Math.min(updatedMain.length, offset + blockSize))));
  }
  entry.integrity = {
    algorithm: "SHA256",
    hash: digest(updatedMain),
    blockSize,
    blocks,
  };

  const headerBuffer = Buffer.from(JSON.stringify(archive.header), "utf8");
  if (headerBuffer.length !== archive.headerJsonLength) {
    throw new Error(
      `ASAR header length changed (${archive.headerJsonLength} -> ${headerBuffer.length}); refusing in-place update`,
    );
  }

  console.log(`SakuraCat hook: ${hook.length}/${hookBoundary} bytes`);
  console.log("DNS exceptions: la.btc620.com, +.rsc.cdn77.org");
  if (dryRun) {
    console.log("Dry run complete; no files changed");
    process.exit(0);
  }

  if (!fs.existsSync(BACKUP_PATH)) {
    fs.mkdirSync(path.dirname(BACKUP_PATH), { recursive: true });
    fs.copyFileSync(ASAR_PATH, BACKUP_PATH);
  }

  const writeFd = fs.openSync(ASAR_PATH, "r+");
  try {
    fs.writeSync(
      writeFd,
      updatedMain.subarray(0, hookBoundary),
      0,
      hookBoundary,
      archive.dataOffset + Number(entry.offset),
    );
    fs.writeSync(writeFd, headerBuffer, 0, headerBuffer.length, 16);
    fs.fsyncSync(writeFd);
  } finally {
    fs.closeSync(writeFd);
  }
  console.log(`Patched: ${ASAR_PATH}`);
  console.log(`Backup:  ${BACKUP_PATH}`);
} finally {
  fs.closeSync(archive.fd);
}
