const { createApp, ref, computed, onMounted, onUnmounted, nextTick } = Vue;

const api = {
  async getLogsBefore(instanceId, offset, limit = 200) {
    const res = await fetch(`/api/instances/${instanceId}/logs/before?offset=${offset}&limit=${limit}`);
    const data = await res.json();
    return data;
  },
  async getInstances() {
    const res = await fetch("/api/instances");
    return await res.json();
  },
  async createInstance(data) {
    const res = await fetch("/api/instances", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data)
    });
    return await res.json();
  },
  async updateInstance(id, data) {
    const res = await fetch(`/api/instances/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data)
    });
    return await res.json();
  },
  async deleteInstance(id) {
    const res = await fetch(`/api/instances/${id}`, { method: "DELETE" });
    return await res.json();
  },
  async stopInstance(id) {
    const res = await fetch(`/api/instances/${id}/stop`, { method: "POST" });
    return await res.json();
  },
  async startInstance(id) {
    const res = await fetch(`/api/instances/${id}/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    return await res.json();
  },
  async previewCommand(data) {
    const res = await fetch("/api/command-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data)
    });
    return await res.json();
  },
  async discoverVersions() {
    const res = await fetch("/api/llama/discover");
    return await res.json();
  },
  async discoverModels() {
    const res = await fetch("/api/models/discover");
    return await res.json();
  },
  async daemonStart() {
    const res = await fetch("/api/daemon/start", { method: "POST" });
    return await res.json();
  },
  async daemonStop() {
    const res = await fetch("/api/daemon/stop", { method: "POST" });
    return await res.json();
  }
};

const DaemonPanel = {
  props: ["status", "loading"],
  emits: ["toggle"],
  template: `
    <div class="daemon-panel">
      <div class="daemon-info">
        <span :class="['daemon-status', status.running ? 'running' : 'stopped']">
          {{ status.running ? '运行中' : '未运行' }}
        </span>
        <span class="daemon-label">守护进程</span>
      </div>
      <button 
        type="button" 
        class="small" 
        :class="{ loading: loading }"
        @click="$emit('toggle')"
      >
        {{ status.running ? '停止' : '启动' }}
      </button>
    </div>
  `
};

const InstanceList = {
  props: ["instances", "selectedId", "loadingIds"],
  emits: ["select", "edit", "toggle"],
  computed: {
    firstRunning() {
      return this.instances.find(i => String(i.status).startsWith("running"));
    }
  },
  methods: {
    isRunning(status) {
      return String(status || "").startsWith("running");
    },
    isLoading(id) {
      return this.loadingIds?.has(id);
    }
  },
  template: `
    <div class="instances">
      <div v-if="instances.length === 0" class="empty">暂无实例</div>
      <div
        v-for="item in instances"
        :key="item.instance_id"
        :class="['instance-card', { selected: item.instance_id === selectedId }]"
        @click="$emit('select', item.instance_id, item.name)"
      >
        <strong>{{ item.name }}</strong>
        <div class="meta">ID: {{ item.instance_id }} | PID: {{ item.pid }} | {{ item.status }}</div>
        <div class="cmd">{{ (item.command || []).join(" ") }}</div>
        <div class="instance-actions">
          <button class="view-log" @click.stop="$emit('select', item.instance_id, item.name)">查看日志</button>
          <button class="edit" @click.stop="$emit('edit', item)">编辑</button>
          <button
            :class="['toggle', 'loading-effect', isRunning(item.status) ? 'danger' : 'primary', { loading: isLoading(item.instance_id) }]"
            :disabled="isLoading(item.instance_id)"
            @click.stop="$emit('toggle', item)"
          >
            {{ isRunning(item.status) ? '停止' : '启动' }}
          </button>
        </div>
      </div>
    </div>
  `
};

const LogViewer = {
  props: ["logs", "loading", "cssClass", "emptyText"],
  emits: ["loadMore"],
  data() {
    return {
      autoScroll: true,
      lastScrollHeight: 0,
      loadingMore: false
    };
  },
  computed: {
    isLoading() {
      return this.loading || this.loadingMore;
    }
  },
  watch: {
    logs: {
      handler() {
        this.updateContent();
        this.$nextTick(() => {
          if (this.autoScroll) {
            this.scrollToBottom();
          } else {
            this.keepScrollPosition();
          }
        });
        this.loadingMore = false;
      },
      deep: true
    }
  },
  mounted() {
    this.updateContent();
    this.$nextTick(() => {
      this.scrollToBottom();
    });
  },
  methods: {
    updateContent() {
      if (this.$refs.output) {
        const emptyMsg = this.emptyText || "请选择左侧实例以查看日志...";
        this.$refs.output.innerHTML = this.logs.length === 0 ? emptyMsg : this.logs.join("\n");
      }
    },
    scrollToBottom() {
      const el = this.$refs.output;
      if (el) {
        el.scrollTop = el.scrollHeight;
      }
    },
    keepScrollPosition() {
      const el = this.$refs.output;
      if (el && this.lastScrollHeight > 0) {
        el.scrollTop = el.scrollHeight - this.lastScrollHeight;
      }
    },
    onScroll() {
      const el = this.$refs.output;
      if (!el) return;
      this.lastScrollHeight = el.scrollHeight;
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= 50;
      this.autoScroll = atBottom;
    },
    onScrollTop() {
      const el = this.$refs.output;
      if (!el) return;
      if (el.scrollTop <= 10 && !this.loadingMore && !this.loading) {
        this.loadingMore = true;
        this.$emit("loadMore");
        if (window.loadMoreLogs) {
          window.loadMoreLogs();
        }
      }
    }
  },
  template: `
    <pre
      ref="output"
      :class="['log-output', cssClass, { loading: loading }]"
      @scroll="onScroll(); onScrollTop()"
    ></pre>
  `
};

const InstanceForm = {
  props: ["instance", "loading"],
  emits: ["save", "close"],
  data() {
    return {
      name: "",
      serverDir: "",
      modelPath: "",
      host: "0.0.0.0",
      port: "8080",
      nCtx: "32768",
      nThreads: "8",
      gpuLayers: "0",
      freeform: "",
      extraFlags: [{ key: "--temp", value: "0.7", enabled: true }, { key: "--top-p", value: "0.9", enabled: true }],
      previewText: "尚未生成",
      versionOptions: [],
      modelOptions: [],
      nCtxOptions: ["2048", "4096", "8192", "16384", "32768", "65536", "131072", "262144", "524288", "1048576"]
    };
  },
  computed: {
    isEdit() {
      return !!this.instance?.instance_id;
    },
    nCtxOptionsFormatted() {
      return this.nCtxOptions.map(v => {
        const n = parseInt(v);
        return {
          value: v,
          label: n >= 1048576 ? (n / 1048576) + 'M' : n >= 1024 ? (n / 1024) + 'k' : v
        };
      });
    }
  },
  watch: {
    instance: {
      immediate: true,
      handler(val) {
        if (val) {
          this.name = val.name || "";
          this.serverDir = val.executable_path || "";
          this.modelPath = val.visual_args?.model_path || "";
          this.host = val.visual_args?.host || "0.0.0.0";
          this.port = val.visual_args?.port || "8080";
          this.nCtx = String(val.visual_args?.n_ctx || "32768");
          this.nThreads = val.visual_args?.n_threads || "8";
          this.gpuLayers = val.visual_args?.gpu_layers || "0";
          this.freeform = val.freeform_args || "";
          this.extraFlags = (val.visual_args?.extra_flags || []).length 
            ? val.visual_args.extra_flags 
            : [{ key: "--temp", value: "0.7", enabled: true }, { key: "--top-p", value: "0.9", enabled: true }];
        } else {
          this.reset();
        }
      }
    },
    name() { this.debouncedPreview(); },
    serverDir() { this.debouncedPreview(); },
    modelPath() { this.debouncedPreview(); },
    host() { this.debouncedPreview(); },
    port() { this.debouncedPreview(); },
    nCtx() { this.debouncedPreview(); },
    nThreads() { this.debouncedPreview(); },
    gpuLayers() { this.debouncedPreview(); },
    freeform() { this.debouncedPreview(); },
    extraFlags: { deep: true, handler() { this.debouncedPreview(); } }
  },
  mounted() {
    this.loadOptions();
    this.$nextTick(() => this.preview());
  },
  methods: {
    reset() {
      this.name = "";
      this.serverDir = "";
      this.modelPath = "";
      this.host = "0.0.0.0";
      this.port = "8080";
      this.nCtx = "32768";
      this.nThreads = "8";
      this.gpuLayers = "0";
      this.freeform = "";
      this.extraFlags = [{ key: "--temp", value: "0.7", enabled: true }, { key: "--top-p", value: "0.9", enabled: true }];
    },
    async loadOptions() {
      try {
        const [vData, mData] = await Promise.all([api.discoverVersions(), api.discoverModels()]);
        this.versionOptions = (vData.items || []).map(item => ({
          value: item.path || item,
          label: item.name || item.path || item
        }));
        this.modelOptions = (mData.items || []).map(item => ({
          value: item.path || item,
          label: item.name || item.path || item
        }));
      } catch (e) {
        console.error(e);
      }
    },
    addFlag() {
      this.extraFlags.push({ key: "", value: "", enabled: true });
    },
    removeFlag(index) {
      this.extraFlags.splice(index, 1);
    },
    async preview() {
      const payload = this.collectPayload();
      try {
        const data = await api.previewCommand(payload);
        this.previewText = data.command.join(" ");
      } catch (e) {
        console.warn("Preview error:", e.message);
      }
    },
    debouncedPreview() {
      clearTimeout(this._previewTimer);
      this._previewTimer = setTimeout(() => this.preview(), 300);
    },
    collectPayload() {
      return {
        name: this.name.trim(),
        server_dir: this.serverDir.trim(),
        visual_args: {
          model_path: this.modelPath.trim(),
          host: this.host.trim(),
          port: Number(this.port) || null,
          n_ctx: Number(this.nCtx) || null,
          n_threads: Number(this.nThreads) || null,
          gpu_layers: this.gpuLayers === "" ? null : Number(this.gpuLayers),
          extra_flags: this.extraFlags.filter(f => f.enabled && f.key)
        },
        freeform_args: this.freeform
      };
    },
    async save() {
      const payload = this.collectPayload();
      this.$emit("save", payload);
    }
  },
  template: `
    <div class="form-modal-card">
      <div class="form-modal-head">
        <strong>{{ isEdit ? '编辑实例' : '添加实例' }}</strong>
        <button type="button" @click="$emit('close')">✕</button>
      </div>
      <div class="form-modal-body">
        <div class="form-section">
          <div class="section-title">实例信息</div>
          <div class="form-grid">
            <div class="form-group">
              <label>实例名称</label>
              <input v-model="name" placeholder="例如: qa-model-a" />
            </div>
            <div class="form-group">
              <label>llama 启动路径</label>
              <input v-model="serverDir" placeholder="目录或可执行文件" />
            </div>
            <div class="form-group full-width">
              <label>或选择扫描版本</label>
              <select v-model="serverDir">
                <option value="">请选择</option>
                <option v-for="v in versionOptions" :key="v.value" :value="v.value">{{ v.label }}</option>
              </select>
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="section-title">模型配置</div>
          <div class="form-grid">
            <div class="form-group">
              <label>Model 路径</label>
              <input v-model="modelPath" placeholder="例如: D:\\models\\qwen.gguf" />
            </div>
            <div class="form-group">
              <label>或选择扫描模型</label>
              <select v-model="modelPath">
                <option value="">请选择</option>
                <option v-for="m in modelOptions" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="section-title">服务器配置</div>
          <div class="form-grid four">
            <div class="form-group">
              <label>Host</label>
              <input v-model="host" />
            </div>
            <div class="form-group">
              <label>Port</label>
              <input v-model="port" type="number" />
            </div>
            <div class="form-group">
              <label>Threads</label>
              <input v-model="nThreads" type="number" />
            </div>
            <div class="form-group">
              <label>GPU Layers</label>
              <input v-model="gpuLayers" type="number" />
            </div>
          </div>
          <div class="form-grid">
            <div class="form-group">
              <label>Context Size</label>
              <select v-model="nCtx">
                <option v-for="c in nCtxOptionsFormatted" :key="c.value" :value="c.value">{{ c.label }}</option>
              </select>
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="section-title">
            额外参数
            <button type="button" class="small" @click="addFlag">+ 添加</button>
          </div>
          <div v-for="(flag, idx) in extraFlags" :key="idx" class="flag-row">
            <input v-model="flag.key" placeholder="--temp" />
            <input v-model="flag.value" placeholder="0.8" />
            <label class="flag-enable">
              <input type="checkbox" v-model="flag.enabled" />启用
            </label>
            <button type="button" class="danger small" @click="removeFlag(idx)">删除</button>
          </div>
        </div>

        <div class="form-section">
          <div class="section-title">自由文本参数</div>
          <textarea v-model="freeform" rows="2" placeholder="例如: --temp 0.7 --top-p 0.9"></textarea>
        </div>

        <div class="form-actions">
          <button type="button" @click="preview" :class="{ loading: false }">命令预览</button>
          <button type="button" class="primary" @click="save" :class="{ loading: loading }">
            {{ isEdit ? '保存并重启' : '创建实例' }}
          </button>
        </div>

        <div class="preview">
          <div class="preview-label">命令预览</div>
          <pre>{{ previewText }}</pre>
        </div>
      </div>
    </div>
  `
};

const LogModal = {
  props: ["logs", "loading"],
  emits: ["close", "loadMore"],
  components: { LogViewer },
  template: `
    <div class="log-modal" @click.self="$emit('close')">
      <div class="log-modal-card-full">
        <div class="log-modal-head">
          <strong>日志放大查看</strong>
          <button type="button" @click="$emit('close')">✕</button>
        </div>
        <log-viewer :logs="logs" :loading="loading" :cssClass="'log-output-large'" :emptyText="'暂无日志'" @loadMore="$emit('loadMore')"></log-viewer>
      </div>
    </div>
  `
};

const app = createApp({
  components: {
    DaemonPanel,
    InstanceList,
    LogViewer,
    InstanceForm,
    LogModal
  },
  data() {
    return {
      instances: [],
      selectedInstanceId: null,
      currentLogs: [],
      logLoading: false,
      logLoadMoreLoading: false,
      logOffset: 0,
      daemonStatus: { running: false, pid: null },
      daemonLoading: false,
      showForm: false,
      editingInstance: null,
      formLoading: false,
      showLogLarge: false,
      logStream: null,
      daemonStream: null,
      loadingIds: new Set()
    };
  },
  computed: {
    currentInstanceName() {
      if (!this.selectedInstanceId) return "未选择实例";
      const inst = this.instances.find(i => i.instance_id === this.selectedInstanceId);
      return inst ? `当前：${inst.name}` : "未选择实例";
    }
  },
  mounted() {
    this.initSSE();
    this.refreshInstances().then(() => {
      const running = this.instances.find(i => String(i.status).startsWith("running"));
      if (running) {
        this.selectInstance(running.instance_id, running.name);
      }
    });
    window.appVm = this;
  },
  beforeUnmount() {
    this.closeStreams();
  },
  methods: {
    initSSE() {
      this.daemonStream = new EventSource("/api/daemon/status/stream");
      this.daemonStream.addEventListener("status", (e) => {
        this.daemonStatus = JSON.parse(e.data || "{}");
      });
      this.daemonStream.addEventListener("instances", (e) => {
        this.instances = JSON.parse(e.data || "{}").items || [];
      });
      this.daemonStream.onerror = () => {
        setTimeout(() => this.initSSE(), 5000);
      };
    },
    async refreshInstances() {
      try {
        const data = await api.getInstances();
        this.instances = data.items || [];
      } catch (e) {
        console.error(e);
      }
    },
    async toggleDaemon() {
      this.daemonLoading = true;
      try {
        if (this.daemonStatus.running) {
          await api.daemonStop();
        } else {
          await api.daemonStart();
        }
      } catch (e) {
        alert(e.message);
      } finally {
        this.daemonLoading = false;
      }
    },
    selectInstance(id, name) {
      this.selectedInstanceId = id;
      this.startLogStream(id);
    },
    startLogStream(id) {
      if (this.logStream) {
        this.logStream.close();
      }
      this.logLoading = true;
      this.currentLogs = [];
      this.logOffset = 0;
      this.logStream = new EventSource(`/api/instances/${id}/logs/stream?lines=300`);
      this.logStream.addEventListener("snapshot", (e) => {
        this.logLoading = false;
        const data = JSON.parse(e.data || "{}");
        this.currentLogs = data.lines || [];
        this.logOffset = this.currentLogs.length;
      });
      this.logStream.addEventListener("append", (e) => {
        const data = JSON.parse(e.data || "{}");
        if (data.line) {
          this.currentLogs.push(data.line);
          this.logOffset = this.currentLogs.length;
        }
      });
      this.logStream.onerror = () => {
        this.logLoading = false;
      };
    },
    async loadMoreLogs() {
      if (this.logLoadMoreLoading || !this.selectedInstanceId) return;
      this.logLoadMoreLoading = true;
      try {
        const data = await api.getLogsBefore(this.selectedInstanceId, this.logOffset, 200);
        if (data.lines && data.lines.length > 0) {
          this.currentLogs = [...data.lines, ...this.currentLogs];
          this.logOffset = this.currentLogs.length;
        }
      } catch (e) {
        console.error(e);
      } finally {
        this.logLoadMoreLoading = false;
      }
    },
    openForm(instance) {
      this.editingInstance = instance;
      this.showForm = true;
    },
    async saveForm(payload) {
      this.formLoading = true;
      try {
        if (this.editingInstance?.instance_id) {
          await api.updateInstance(this.editingInstance.instance_id, payload);
        } else {
          await api.createInstance(payload);
        }
        this.showForm = false;
        await this.refreshInstances();
      } catch (e) {
        alert(e.message);
      } finally {
        this.formLoading = false;
      }
    },
    async toggleInstance(item) {
      this.loadingIds.add(item.instance_id);
      this.selectInstance(item.instance_id, item.name);
      try {
        if (String(item.status).startsWith("running")) {
          await api.stopInstance(item.instance_id);
        } else {
          this.currentLogs = [];
          await api.startInstance(item.instance_id);
        }
        await this.refreshInstances();
      } catch (e) {
        alert(e.message);
      } finally {
        this.loadingIds.delete(item.instance_id);
      }
    },
    closeStreams() {
      if (this.logStream) {
        this.logStream.close();
        this.logStream = null;
      }
      if (this.daemonStream) {
        this.daemonStream.close();
        this.daemonStream = null;
      }
    }
  }
});

app.mount("#app");

window.loadMoreLogs = function() {
  if (window.appVm) {
    window.appVm.loadMoreLogs();
  }
};