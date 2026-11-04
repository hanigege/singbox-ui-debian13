const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../singbox-rule-ui/static/app.js"), "utf8");

/** @param {string} name @returns {string} */
function extract(name) {
  const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.ok(start >= 0, `缺少函数：${name}`);
  const tail = source.slice(start);
  const end = tail.indexOf("\n}\n");
  assert.ok(end >= 0, `函数未闭合：${name}`);
  return tail.slice(0, end + 2);
}

/** @typedef {{proxy?: {ok: boolean, data?: {now: string}}, delays?: {delays: Record<string, {ok: boolean, delay: number|null, error?: string|null}>}, state?: {groups: {proxy: {default: string}}}, proxyAfterProbe?: {now: string}, check?: {code: number}, restart?: {code: number}} Payload */
/** @typedef {(url: string, options?: {body: string}) => Promise<Payload>} Api */

/** @param {string} tag @returns {Payload} */
function payload(tag) {
  return { proxy: { ok: true, data: { now: tag } }, delays: { delays: {} } };
}

/** @returns {{promise: Promise<Payload>, resolve: (value: Payload) => void, reject: (error: Error) => void}} */
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

/** @param {Api} api @returns {import("node:vm").Context} */
function contextFor(api) {
  // 只替换外部网络与 DOM 边界，实际执行生产代码中的刷新、切换和渲染逻辑。
  const context = vm.createContext({
    api, warnings: [], statuses: [], renders: [],
    console: { warn: (...args) => context.warnings.push(args) },
    setBusy() {}, updateButtons() {}, setDirty() {}, syncDraftSettings() {},
    pulseActionButton() {}, finishActionButton() {},
    stopRuntimeLogs() {}, stopRuntimePolling() {}, showLogin() {}, renderMeta() {},
    localStorage: { removeItem() {} }, $: () => ({ value: "" }),
    setStatus: (message, tone) => context.statuses.push({ message, tone }),
    t: (key) => key,
  });
  vm.runInContext(`
    let state = {groups: {proxy: {default: "A"}}};
    let maintenance = {};
    let runtimeProxy = {now: "A", available: true};
    let delays = {};
    let dirty = true;
    let editingNodeTag = null;
    function render() { renders.push(runtimeProxy.now); }
  `, context);
  for (const name of ["delayRefreshInFlight", "delayRefreshPending", "proxyRequestVersion"]) {
    const declaration = source.match(new RegExp(`^let ${name} = .*;$`, "m"));
    assert.ok(declaration, `缺少状态：${name}`);
    vm.runInContext(declaration[0], context);
  }
  for (const name of ["loadProxyPayload", "loadProxyInfo", "loadProxyDelays", "applyProxyPayload", "chooseDefault", "refreshDelaysInBackground", "refreshDelays", "formatDelay", "logout", "save", "restart"]) {
    vm.runInContext(extract(name), context);
  }
  return context;
}

/** @returns {Promise<void>} */
function flush() {
  return new Promise(setImmediate);
}

test("连续切换时忽略旧测速响应，并合并补测最新节点", async () => {
  const oldProbe = deferred();
  const newProbe = deferred();
  let probes = 0;
  const context = contextFor(async (url, options) => {
    if (url === "/api/proxy/default") {
      const { tag } = JSON.parse(options.body);
      return { state: { groups: { proxy: { default: tag } } }, proxyAfterProbe: { now: tag } };
    }
    assert.equal(url, "/api/delays?test=1");
    return ++probes === 1 ? oldProbe.promise : newProbe.promise;
  });
  await vm.runInContext('chooseDefault("A")', context);
  await vm.runInContext('chooseDefault("B")', context);
  await vm.runInContext('chooseDefault("C")', context);
  assert.equal(probes, 1);
  oldProbe.resolve(payload("A"));
  await flush();
  assert.equal(vm.runInContext("runtimeProxy.now", context), "C");
  assert.equal(probes, 2);
  newProbe.resolve(payload("C"));
  await flush();
  assert.equal(vm.runInContext("state.groups.proxy.default === runtimeProxy.now", context), true);
  assert.equal(vm.runInContext("delayRefreshInFlight || delayRefreshPending", context), false);
});

test("后发读取优先，旧响应及旧错误均不能覆盖当前状态", async () => {
  for (const outcome of ["response", "error"]) {
    const old = deferred();
    let calls = 0;
    const context = contextFor(async () => ++calls === 1 ? old.promise : payload("B"));
    const request = vm.runInContext("loadProxyInfo()", context);
    await vm.runInContext("loadProxyInfo()", context);
    if (outcome === "response") old.resolve(payload("A"));
    else old.reject(new Error("旧连接超时"));
    assert.equal(await request, null);
    assert.equal(vm.runInContext("runtimeProxy.now", context), "B");
  }
});

test("测速失败不会被空历史覆盖，下一次有效测速可以清除失败", async () => {
  const context = contextFor(async (url) => {
    assert.equal(url, "/api/delays?test=1");
    return { ...payload("A"), delays: { delays: { A: { ok: false, delay: null, error: "timeout" } } } };
  });
  await vm.runInContext("refreshDelaysInBackground()", context);
  assert.equal(vm.runInContext('formatDelay("A")', context), "delayFailed");
  vm.runInContext('applyProxyPayload({delays: {delays: {A: {ok: true, delay: null, error: null}}}})', context);
  assert.equal(vm.runInContext('formatDelay("A")', context), "delayFailed");
  vm.runInContext('applyProxyPayload({delays: {delays: {A: {ok: true, delay: 42, error: null}}}})', context);
  assert.equal(vm.runInContext('formatDelay("A")', context), "42 ms");
});

test("后台读取错误保留确认状态并记录错误，手动测速明确提示失败", async () => {
  const error = new Error("network unavailable");
  const context = contextFor(async () => { throw error; });
  await vm.runInContext("refreshDelaysInBackground()", context);
  assert.equal(vm.runInContext("runtimeProxy.now", context), "A");
  assert.equal(vm.runInContext("runtimeProxy.available", context), true);
  assert.equal(context.warnings.length, 1);
  assert.equal(context.warnings[0][1].error, error);
  assert.equal(context.statuses.length, 0);
  await vm.runInContext("refreshDelays()", context);
  assert.equal(context.statuses.at(-1).message, error.message);
  assert.equal(context.statuses.at(-1).tone, "bad");
});

test("切换失败仅回读运行态，不启动全量测速", async () => {
  const calls = [];
  const context = contextFor(async (url) => {
    calls.push(url);
    if (url === "/api/proxy/default") throw new Error("switch rejected");
    assert.equal(url, "/api/proxy");
    return payload("A");
  });
  await vm.runInContext('chooseDefault("B")', context);
  await flush();
  assert.deepEqual(calls, ["/api/proxy/default", "/api/proxy"]);
  assert.equal(context.statuses.at(-1).tone, "bad");
  assert.equal(context.renders.at(-1), "A");
});

test("手动测速期间到达的后台请求会在结束后补测", async () => {
  const first = deferred();
  let calls = 0;
  const context = contextFor(async () => ++calls === 1 ? first.promise : payload("B"));
  const manual = vm.runInContext("refreshDelays()", context);
  await vm.runInContext("refreshDelaysInBackground()", context);
  first.resolve(payload("A"));
  await manual;
  await flush();
  assert.equal(calls, 2);
  assert.equal(vm.runInContext("runtimeProxy.now", context), "B");
  assert.equal(vm.runInContext("delayRefreshInFlight || delayRefreshPending", context), false);
});

test("退出后旧测速响应不能恢复缓存或继续补测", async () => {
  const probe = deferred();
  let calls = 0;
  const context = contextFor(async () => { calls++; return probe.promise; });
  const running = vm.runInContext("refreshDelaysInBackground()", context);
  await vm.runInContext("refreshDelaysInBackground()", context);
  vm.runInContext("logout()", context);
  probe.resolve(payload("A"));
  await running;
  assert.equal(vm.runInContext("runtimeProxy.now", context), null);
  assert.equal(calls, 1);
});

test("保存或重启成功后的读取错误不会把操作显示为失败", async () => {
  for (const operation of ["save", "restart"]) {
    const context = contextFor(async (url) => {
      if (url === `/api/${operation}`) {
        return { ...payload("B"), state: { groups: { proxy: { default: "B" } } }, check: { code: 0 }, restart: { code: 0 } };
      }
      throw new Error("refresh unavailable");
    });
    await vm.runInContext(operation === "save" ? "save(true)" : "restart()", context);
    await flush();
    assert.equal(context.statuses.at(-1).tone, "ok");
    assert.equal(context.warnings.length, 1);
  }
});
