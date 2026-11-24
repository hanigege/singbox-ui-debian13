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

/** @param {Array<object|Error>} responses */
function contextFor(responses) {
  const storage = new Map();
  const calls = [];
  const statuses = [];
  const context = vm.createContext({
    calls,
    statuses,
    encodeURIComponent,
    Date,
    sessionStorage: {
      setItem: (key, value) => storage.set(key, value),
      getItem: (key) => storage.get(key) || null,
      removeItem: (key) => storage.delete(key),
    },
    api: async (url) => {
      calls.push(url);
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
    enabledNodeTags: () => ["hy2-a", "vless", "hy2-b"],
    setStatus: (message) => statuses.push(message),
    sleep: async () => {},
    t: (key) => key,
  });
  vm.runInContext('const BACKUP_IMPORT_OPERATION_KEY = "ruleUiBackupImportOperation";', context);
  vm.runInContext(extract("backupImportPhaseText"), context);
  vm.runInContext(extract("pollBackupImportOperation"), context);
  return { context, storage, calls, statuses };
}

test("导入只轮询后端任务并采用唯一一轮测速结果", async () => {
  const runtime = {
    proxy: { ok: true, data: { now: "Auto" } },
    delays: { delays: { "hy2-a": { ok: true, delay: 100 }, vless: { ok: true, delay: 80 }, "hy2-b": { ok: true, delay: 120 } } },
  };
  const { context, storage, calls, statuses } = contextFor([
    { operation: { id: "job-1", status: "running", phase: "restarting" } },
    { operation: { id: "job-1", status: "running", phase: "testing-delays" } },
    { operation: { id: "job-1", status: "complete", phase: "complete", runtime } },
  ]);
  const result = await vm.runInContext('pollBackupImportOperation("job-1")', context);
  assert.deepEqual(JSON.parse(JSON.stringify(result.runtime)), runtime);
  assert.deepEqual(calls, [
    "/api/backup/import/status?id=job-1",
    "/api/backup/import/status?id=job-1",
    "/api/backup/import/status?id=job-1",
  ]);
  assert.deepEqual(statuses, ["backupImportRestarting", "backupImportTestingDelays"]);
  assert.equal(storage.size, 0);
  assert.ok(!extract("importBackupFromFile").includes("loadProxyDelays"));
  assert.ok(!source.includes("pollDelaysAfterImport"));
});

test("后台失败清理恢复标记并返回可操作错误", async () => {
  const { context, storage } = contextFor([
    { operation: { id: "job-2", status: "failed", phase: "rolled-back", error: "TProxy 同步失败: code=1" } },
  ]);
  await assert.rejects(vm.runInContext('pollBackupImportOperation("job-2")', context), /TProxy 同步失败/);
  assert.equal(storage.size, 0);
});
