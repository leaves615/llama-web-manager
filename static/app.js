const els = {
  instances: document.getElementById("instances"),
  refreshBtn: document.getElementById("refreshBtn"),
  addInstanceBtn: document.getElementById("addInstanceBtn"),
  currentInstance: document.getElementById("currentInstance"),
  logOutput: document.getElementById("logOutput"),
  logOutputLarge: document.getElementById("logOutputLarge"),
  enlargeLogBtn: document.getElementById("enlargeLogBtn"),
  logModal: document.getElementById("logModal"),
  closeLogModalBtn: document.getElementById("closeLogModalBtn"),

  formModal: document.getElementById("formModal"),
  formModalTitle: document.getElementById("formModalTitle"),
  closeFormModalBtn: document.getElementById("closeFormModalBtn"),
  name: document.getElementById("name"),
  serverDir: document.getElementById("server_dir"),
  versionSelect: document.getElementById("version_select"),
  modelPath: document.getElementById("model_path"),
  modelSelect: document.getElementById("model_select"),
  host: document.getElementById("host"),
  port: document.getElementById("port"),
  nThreads: document.getElementById("n_threads"),
  gpuLayers: document.getElementById("gpu_layers"),
  nCtxSelect: document.getElementById("n_ctx_select"),
  addFlag: document.getElementById("addFlag"),
  flagRows: document.getElementById("flagRows"),
  flagRowTemplate: document.getElementById("flagRowTemplate"),
  previewBtn: document.getElementById("previewBtn"),
  saveFormBtn: document.getElementById("saveFormBtn"),
  previewText: document.getElementById("previewText"),
  freeform: document.getElementById("freeform"),
  daemonStatus: document.getElementById("daemonStatus"),
  daemonToggleBtn: document.getElementById("daemonToggleBtn"),
  loading: document.getElementById("loading"),
};

let selectedInstanceId = null;
let logStream = null;
let editingInstanceId = null;
let lastAutoFilledName = "";
let loadingCount = 0;
let daemonStream = null;

function showLoading(btn) {
  if (btn) {
    btn.classList.add("loading");
  } else {
    loadingCount++;
    els.loading.classList.remove("hidden");
  }
}

function hideLoading(btn) {
  if (btn) {
    btn.classList.remove("loading");
  } else {
    loadingCount--;
    if (loadingCount <= 0) {
      loadingCount = 0;
      els.loading.classList.add("hidden");
    }
  }
}

function isRunningStatus(status) {
  return String(status || "").startsWith("running");
}

function setLogText(text) {
  const nextText = text || "";
  updateLogView(els.logOutput, nextText);
  updateLogView(els.logOutputLarge, nextText);
}

function isAtBottom(element, threshold = 8) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function updateLogView(element, nextText) {
  const followBottom = isAtBottom(element);
  const previousTop = element.scrollTop;

  element.textContent = nextText;

  if (followBottom) {
    element.scrollTop = element.scrollHeight;
    return;
  }

  const maxTop = Math.max(0, element.scrollHeight - element.clientHeight);
  element.scrollTop = Math.min(previousTop, maxTop);
}

function appendLogLines(lines) {
  if (!Array.isArray(lines) || !lines.length) {
    return;
  }

  const existing = els.logOutput.textContent;
  const appended = lines.join("\n");
  const next = existing ? `${existing}\n${appended}` : appended;
  setLogText(next);
}

function stopLogStream() {
  if (!logStream) {
    return;
  }
  logStream.close();
  logStream = null;
}

function openLogModal() {
  els.logModal.classList.remove("hidden");
  els.logOutputLarge.scrollTop = els.logOutputLarge.scrollHeight;
}

function closeLogModal() {
  els.logModal.classList.add("hidden");
}

function openFormModal() {
  els.formModal.classList.remove("hidden");
}

function closeFormModal() {
  els.formModal.classList.add("hidden");
}

function addFlagRow(key = "", value = "", enabled = true) {
  const frag = els.flagRowTemplate.content.cloneNode(true);
  const row = frag.querySelector(".flag-row");
  row.querySelector(".flag-key").value = key;
  row.querySelector(".flag-value").value = value;
  row.querySelector(".flag-enabled").checked = enabled;
  row.querySelector(".remove-row").addEventListener("click", () => row.remove());
  els.flagRows.appendChild(frag);
}

function readExtraFlags() {
  return [...els.flagRows.querySelectorAll(".flag-row")].map((row) => ({
    key: row.querySelector(".flag-key").value.trim(),
    value: row.querySelector(".flag-value").value.trim(),
    enabled: row.querySelector(".flag-enabled").checked,
  }));
}

function collectPayload() {
  return {
    name: els.name.value.trim(),
    server_dir: els.serverDir.value.trim(),
    visual_args: {
      model_path: els.modelPath.value.trim(),
      host: els.host.value.trim(),
      port: Number(els.port.value) || null,
      n_ctx: Number(els.nCtxSelect.value) || null,
      n_threads: Number(els.nThreads.value) || null,
      gpu_layers: els.gpuLayers.value === "" ? null : Number(els.gpuLayers.value),
      extra_flags: readExtraFlags(),
    },
    freeform_args: els.freeform.value,
  };
}

function clearForm() {
  els.name.value = "";
  els.serverDir.value = "";
  els.versionSelect.value = "";
  els.modelPath.value = "";
  els.modelSelect.value = "";
  els.host.value = "0.0.0.0";
  els.port.value = "8080";
  els.nCtxSelect.value = "32768";
  els.nThreads.value = "8";
  els.gpuLayers.value = "0";
  els.freeform.value = "";
  els.flagRows.innerHTML = "";
  addFlagRow("--temp", "0.7", true);
  addFlagRow("--top-p", "0.9", true);
  els.previewText.textContent = "尚未生成";
  lastAutoFilledName = "";
}

function populateContextSizeOptions() {
  if (!els.ctxSizeOptions) {
    return;
  }

  els.ctxSizeOptions.innerHTML = "";
  for (let value = 2048; value <= 1048576; value *= 2) {
    const option = document.createElement("option");
    option.value = String(value);
    option.label = `${Math.floor(value / 1024)}k/${value}`;
    els.ctxSizeOptions.appendChild(option);
  }
}

function startCreate() {
  editingInstanceId = null;
  els.formModalTitle.textContent = "添加实例";
  els.saveFormBtn.textContent = "创建实例";
  clearForm();
  refreshAutoDiscoveries();
  openFormModal();
}

function startEdit(item) {
  editingInstanceId = item.instance_id;
  els.formModalTitle.textContent = `编辑实例 ${item.name}`;
  els.saveFormBtn.textContent = "保存并重启";

  const visual = item.visual_args || {};
  els.name.value = item.name || "";
  els.serverDir.value = item.executable_path || "";
  els.versionSelect.value = "";
  els.modelPath.value = visual.model_path || "";
  els.modelSelect.value = "";
  els.host.value = visual.host || "0.0.0.0";
  els.port.value = visual.port ?? "";
  els.nCtxSelect.value = String(visual.n_ctx ?? "32768");
  els.nThreads.value = visual.n_threads ?? "";
  els.gpuLayers.value = visual.gpu_layers ?? "";
  els.freeform.value = item.freeform_args || "";
  lastAutoFilledName = "";

  els.flagRows.innerHTML = "";
  const extraFlags = Array.isArray(visual.extra_flags) ? visual.extra_flags : [];
  if (extraFlags.length) {
    extraFlags.forEach((flag) => addFlagRow(flag.key || "", flag.value || "", flag.enabled !== false));
  } else {
    addFlagRow();
  }

  refreshAutoDiscoveries();
  openFormModal();
  previewCommand();
}

async function postJson(url, body, btn) {
  showLoading(btn);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || "请求失败");
    }
    return data;
  } finally {
    hideLoading(btn);
  }
}

async function putJson(url, body, btn) {
  showLoading(btn);
  try {
    const res = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || "更新失败");
    }
    return data;
  } finally {
    hideLoading(btn);
  }
}

async function deleteReq(url, btn) {
  showLoading(btn);
  try {
    const res = await fetch(url, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || "删除失败");
    }
    return data;
  } finally {
    hideLoading(btn);
  }
}

async function previewCommand() {
  try {
    const payload = collectPayload();
    const data = await postJson("/api/command-preview", payload, els.previewBtn);
    els.previewText.textContent = data.command.join(" ");
  } catch (e) {
    els.previewText.textContent = `错误：${e.message}`;
  }
}

async function saveForm() {
  try {
    const payload = collectPayload();
    let instance;
    if (editingInstanceId) {
      instance = await putJson(`/api/instances/${editingInstanceId}`, payload, els.saveFormBtn);
    } else {
      instance = await postJson("/api/instances", payload, els.saveFormBtn);
    }
    closeFormModal();
    await refreshInstances();
    if (instance?.instance_id) {
      selectInstance(instance.instance_id, instance.name || "实例");
    }
  } catch (e) {
    alert(e.message);
  }
}

async function toggleInstance(item, btn) {
  try {
    if (isRunningStatus(item.status)) {
      await deleteReq(`/api/instances/${item.instance_id}`, btn);
    } else {
      await postJson(`/api/instances/${item.instance_id}/start`, {}, btn);
    }
    await refreshInstances();
  } catch (e) {
    alert(e.message);
  }
}

async function discoverVersions() {
  try {
    const res = await fetch("/api/llama/discover");
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    const currentSelection = els.versionSelect.value;

    els.versionSelect.innerHTML = "<option value=''>扫描中...</option>";
    items.forEach((item) => {
      const path = typeof item === "string" ? item : item.path;
      const name = typeof item === "string" ? item : item.name;
      if (!path) {
        return;
      }
      const opt = document.createElement("option");
      opt.value = path;
      opt.textContent = name || path;
      els.versionSelect.appendChild(opt);
    });

    const pathSet = new Set(items.map((item) => (typeof item === "string" ? item : item.path)).filter(Boolean));
    if (currentSelection && pathSet.has(currentSelection)) {
      els.versionSelect.value = currentSelection;
    }
  } catch (_e) {
    // 后台自动扫描失败时不打断用户手工输入。
  }
}

async function discoverModels() {
  try {
    const res = await fetch("/api/models/discover");
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    const currentSelection = els.modelSelect.value;

    els.modelSelect.innerHTML = "<option value=''>扫描中...</option>";
    items.forEach((item) => {
      const path = typeof item === "string" ? item : item.path;
      const name = typeof item === "string" ? item : item.name;
      if (!path) {
        return;
      }
      const opt = document.createElement("option");
      opt.value = path;
      opt.textContent = name || path;
      els.modelSelect.appendChild(opt);
    });

    const pathSet = new Set(items.map((item) => (typeof item === "string" ? item : item.path)).filter(Boolean));
    if (currentSelection && pathSet.has(currentSelection)) {
      els.modelSelect.value = currentSelection;
    }
  } catch (_e) {
    // 后台自动扫描失败时不打断用户手工输入。
  }
}

async function refreshAutoDiscoveries() {
  await Promise.all([discoverVersions(), discoverModels()]);
}

function renderInstances(items) {
  if (!items.length) {
    els.instances.innerHTML = "<p>暂无实例</p>";
    return;
  }

  const firstRunning = items.find((item) => isRunningStatus(item.status));

  els.instances.innerHTML = "";
  items.forEach((item) => {
    const toggleText = isRunningStatus(item.status) ? "停止" : "启动";
    const toggleClass = isRunningStatus(item.status) ? "danger" : "primary";
    const activeClass = item.instance_id === selectedInstanceId ? " selected" : "";
    const card = document.createElement("div");
    card.className = `instance-card${activeClass}`;
    card.innerHTML = `
      <strong>${item.name}</strong>
      <div class="meta">ID: ${item.instance_id} | PID: ${item.pid} | ${item.status}</div>
      <div class="cmd">${(item.command || []).join(" ")}</div>
      <div class="instance-actions">
        <button class="view-log">查看日志</button>
        <button class="edit">编辑</button>
        <button class="toggle ${toggleClass}">${toggleText}</button>
      </div>
    `;

    card.querySelector(".view-log").addEventListener("click", () => {
      selectInstance(item.instance_id, item.name);
    });
    card.querySelector(".edit").addEventListener("click", () => {
      startEdit(item);
    });
    card.querySelector(".toggle").addEventListener("click", async (e) => {
      await toggleInstance(item, e.target);
    });

    els.instances.appendChild(card);
  });

  if (firstRunning && !selectedInstanceId) {
    selectInstance(firstRunning.instance_id, firstRunning.name);
  }
}

async function refreshInstances() {
  showLoading();
  try {
    const res = await fetch("/api/instances");
    const data = await res.json();
    renderInstances(data.items || []);
  } finally {
    hideLoading();
  }
}

function startLogStream(instanceId) {
  if (!instanceId) {
    return;
  }

  stopLogStream();
  setLogText("日志连接中...");

  const stream = new EventSource(`/api/instances/${instanceId}/logs/stream?lines=300`);
  logStream = stream;

  stream.addEventListener("snapshot", (event) => {
    if (selectedInstanceId !== instanceId) {
      return;
    }
    try {
      const payload = JSON.parse(event.data || "{}");
      setLogText((payload.lines || []).join("\n"));
    } catch (_e) {
      setLogText("日志解析失败");
    }
  });

  stream.addEventListener("append", (event) => {
    if (selectedInstanceId !== instanceId) {
      return;
    }
    try {
      const payload = JSON.parse(event.data || "{}");
      const line = payload.line;
      if (line) {
        appendLogLines([line]);
      }
    } catch (_e) {
    }
  });

  stream.addEventListener("log-error", (event) => {
    if (selectedInstanceId !== instanceId) {
      return;
    }
    try {
      const payload = JSON.parse(event.data || "{}");
      setLogText(payload.error || "无法读取日志");
    } catch (_e) {
      setLogText("无法读取日志");
    }
  });

  stream.addEventListener("end", () => {
    if (logStream === stream) {
      stopLogStream();
    }
  });

  stream.onerror = () => {
    if (selectedInstanceId !== instanceId) {
      return;
    }
    if (stream.readyState === EventSource.CLOSED && logStream === stream) {
      setLogText("日志连接已断开");
      stopLogStream();
    }
  };
}

function selectInstance(instanceId, name) {
  selectedInstanceId = instanceId;
  els.currentInstance.textContent = `当前：${name} (${instanceId})`;
  refreshInstances();
  startLogStream(instanceId);
}

els.refreshBtn.addEventListener("click", refreshInstances);
els.addInstanceBtn.addEventListener("click", startCreate);
els.closeFormModalBtn.addEventListener("click", closeFormModal);
els.previewBtn.addEventListener("click", previewCommand);
els.saveFormBtn.addEventListener("click", saveForm);
els.addFlag.addEventListener("click", () => addFlagRow());

els.versionSelect.addEventListener("change", () => {
  if (els.versionSelect.value) {
    els.serverDir.value = els.versionSelect.value;
    previewCommand();
  }
});

els.modelSelect.addEventListener("change", () => {
  if (els.modelSelect.value) {
    els.modelPath.value = els.modelSelect.value;
    const selectedOption = els.modelSelect.options[els.modelSelect.selectedIndex];
    const modelName = (selectedOption?.textContent || "").trim();
    const currentName = els.name.value.trim();
    if (!currentName || currentName === lastAutoFilledName) {
      els.name.value = modelName;
      lastAutoFilledName = modelName;
    }
    previewCommand();
  }
});

els.name.addEventListener("input", () => {
  if (els.name.value.trim() !== lastAutoFilledName) {
    lastAutoFilledName = "";
  }
});

els.formModal.addEventListener("click", (event) => {
  if (event.target === els.formModal) {
    closeFormModal();
  }
});

els.enlargeLogBtn.addEventListener("click", openLogModal);
els.closeLogModalBtn.addEventListener("click", closeLogModal);
els.logModal.addEventListener("click", (event) => {
  if (event.target === els.logModal) {
    closeLogModal();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeLogModal();
    closeFormModal();
  }
});

window.addEventListener("beforeunload", () => {
  stopLogStream();
});

setLogText("请选择左侧实例以查看日志...");
populateContextSizeOptions();
clearForm();
refreshInstances();
refreshAutoDiscoveries();
initDaemonStatusStream();

function initDaemonStatusStream() {
  if (daemonStream) {
    daemonStream.close();
  }
  daemonStream = new EventSource("/api/daemon/status/stream");
  daemonStream.addEventListener("status", (event) => {
    try {
      const data = JSON.parse(event.data || "{}");
      if (data.running) {
        els.daemonStatus.textContent = "运行中";
        els.daemonStatus.className = "daemon-status running";
        els.daemonToggleBtn.textContent = "停止";
      } else {
        els.daemonStatus.textContent = "未运行";
        els.daemonStatus.className = "daemon-status stopped";
        els.daemonToggleBtn.textContent = "启动";
      }
    } catch (_e) {}
  });
  daemonStream.addEventListener("instances", (event) => {
    try {
      const data = JSON.parse(event.data || "{}");
      renderInstances(data.items || []);
    } catch (_e) {}
  });
  daemonStream.onerror = () => {
    setTimeout(initDaemonStatusStream, 5000);
  };
}

els.daemonToggleBtn.addEventListener("click", async (e) => {
  const isRunning = els.daemonToggleBtn.textContent === "停止";
  showLoading(e.target);
  try {
    const url = isRunning ? "/api/daemon/stop" : "/api/daemon/start";
    const res = await fetch(url, { method: "POST" });
    const data = await res.json();
    if (data.error) {
      alert(data.error);
    }
  } catch (_e) {
    alert(isRunning ? "停止失败" : "启动失败");
  } finally {
    hideLoading(e.target);
  }
});