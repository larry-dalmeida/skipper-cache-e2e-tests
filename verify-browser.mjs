// Optional render check: Node >=22 and a local Chrome/Chromium executable.
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile, open, rm } from "node:fs/promises";
import { resolve } from "node:path";

if (!process.env.CHROME) throw new Error("Set CHROME to a Chrome/Chromium executable");
const runtime = resolve(".runtime");
await mkdir(runtime, { recursive: true });
const profile = resolve(runtime, "browser-profile");
await rm(resolve(profile, "DevToolsActivePort"), { force: true });
const log = await open(resolve(runtime, "browser.log"), "w");
const chrome = spawn(process.env.CHROME, [
  "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-background-networking", "--disable-extensions", "--disable-sync",
  "--disable-component-update", "--remote-debugging-port=0",
  `--user-data-dir=${profile}`, "about:blank",
], { stdio: ["ignore", log.fd, log.fd] });
let socket;
const delay = () => new Promise(resolve => setTimeout(resolve, 250));
async function until(action, timeout = 60000) {
  const deadline = Date.now() + timeout;
  let error;
  while (Date.now() < deadline) {
    if (chrome.exitCode !== null) throw new Error("Chrome exited; inspect .runtime/browser.log");
    try { const value = await action(); if (value) return value; }
    catch (e) { error = e; }
    await delay();
  }
  throw new Error(`Browser readiness timeout: ${error || "condition not met"}`);
}
try {
  const port = await until(async () =>
    (await readFile(resolve(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]);
  const pages = await until(async () => {
    const response = await fetch(`http://127.0.0.1:${port}/json/list`);
    return (await response.json()).find(p => p.type === "page");
  });
  socket = new WebSocket(pages.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  let id = 0;
  const pending = new Map();
  const queries = new Set();
  let completedQueries = 0;
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.requestWillBeSent" &&
        message.params.request.url.includes("/api/ds/query")) {
      queries.add(message.params.requestId);
    }
    if (message.method === "Network.loadingFinished" &&
        queries.delete(message.params.requestId)) completedQueries++;
    const entry = pending.get(message.id);
    if (entry) {
      pending.delete(message.id);
      clearTimeout(entry.timer);
      if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
      else entry.resolve(message.result);
    }
  };
  function call(method, params = {}) {
    return new Promise((resolve, reject) => {
      const next = ++id;
      const timer = setTimeout(() => {
        pending.delete(next);
        reject(new Error(`CDP timeout: ${method}`));
      }, 15000);
      pending.set(next, { resolve, reject, timer });
      socket.send(JSON.stringify({ id: next, method, params }));
    });
  }
  await call("Page.enable");
  await call("Network.enable");
  await call("Emulation.setDeviceMetricsOverride", {
    width: 1800, height: 1800, deviceScaleFactor: 1, mobile: false,
  });
  await call("Page.navigate", {
    url: "http://127.0.0.1:3000/d/ag1x69aba3qpse/skipper-cache-verification",
  });
  const text = await until(async () => {
    const result = await call("Runtime.evaluate", {
      expression: "document.body.innerText", returnByValue: true,
    });
    const value = result.result.value || "";
    return completedQueries >= 8 && queries.size === 0 &&
      value.includes("Storage faults (absent means NOT EXERCISED)") &&
      value.includes("L2 set p95") && value.includes("Prometheus") &&
      !value.includes("Loading") && value;
  });
  if (/Data source not found|Templating initialization failed|An error occurred/.test(text)) {
    throw new Error(text);
  }
  await writeFile(resolve(runtime, "browser-text.txt"), text);
  await call("Runtime.evaluate", {
    expression: "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
    awaitPromise: true,
  });
  const image = await call("Page.captureScreenshot", { format: "png" });
  await writeFile(resolve(runtime, "dashboard.png"), Buffer.from(image.data, "base64"));
  console.log("PASS: dashboard rendered, datasource selected, eight panel titles loaded");
  console.log("Inspect .runtime/dashboard.png for visual confirmation; storage faults may have no data.");
} finally {
  socket?.close();
  chrome.kill("SIGTERM");
  await log.close();
}
