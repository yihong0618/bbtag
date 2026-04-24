function bluetag() {
  const apiMeta = document.querySelector('meta[name="bluetag-api"]');
  const apiBase = (apiMeta?.content || "").replace(/\/$/, "");

  return {
    version: "1.0.0",
    tab: "devices",
    tabs: [
      { id: "devices", label: "设备" },
      { id: "text", label: "文字" },
      { id: "image", label: "图片" },
    ],
    screens: [
      { name: "3.7inch", label: "3.7 寸" },
      { name: "2.13inch", label: "2.13 寸" },
    ],
    screen: "3.7inch",
    devices: [],
    defaultDevice: null,
    scanning: false,
    busy: false,
    previewing: false,
    previewUrl: "",
    toast: null,
    token: "",

    text: {
      title: "",
      body: "",
      title_color: "red",
      body_color: "black",
      separator_color: "yellow",
      align: "left",
    },
    imageFile: null,

    init() {
      this.token = localStorage.getItem("bluetag_token") || "";
      this.defaultDevice = localStorage.getItem("bluetag_device") || null;
      this.screen = localStorage.getItem("bluetag_screen") || "3.7inch";
      this.loadDevices();
      this.$watch("defaultDevice", (v) => v && localStorage.setItem("bluetag_device", v));
      this.$watch("screen", (v) => {
        localStorage.setItem("bluetag_screen", v);
        this.coerceColors();
      });
      this.$watch("tab", () => {
        this.previewUrl = "";
        if (this.tab === "text" && this.text.body) this.refreshPreview();
      });
    },

    availableColors() {
      return this.screen === "2.13inch"
        ? ["black", "red"]
        : ["black", "red", "yellow"];
    },

    coerceColors() {
      const allowed = this.availableColors();
      let changed = false;
      for (const k of ["title_color", "body_color", "separator_color"]) {
        if (!allowed.includes(this.text[k])) {
          this.text[k] = k === "title_color" ? "red" : "black";
          changed = true;
        }
      }
      if (changed && this.tab === "text" && this.text.body) this.refreshPreview();
    },

    saveToken() {
      localStorage.setItem("bluetag_token", this.token || "");
    },

    resolvedDeviceLabel() {
      return this.defaultDevice || `第一台在线 ${this.screen} 设备`;
    },

    async api(path, { method = "GET", body, params } = {}) {
      const url = new URL(apiBase + path, window.location.href);
      if (params) {
        for (const [k, v] of Object.entries(params)) {
          if (v != null && v !== "") url.searchParams.set(k, v);
        }
      }
      const headers = {};
      if (this.token) headers["X-API-Token"] = this.token;
      const res = await fetch(url, { method, headers, body });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`${res.status} ${text || res.statusText}`);
      }
      return res;
    },

    async loadDevices() {
      try {
        const res = await this.api("/api/v1/devices", { params: { screen: this.screen } });
        const data = await res.json();
        this.devices = data.items;
        if (this.defaultDevice && !this.devices.find((d) => d.name === this.defaultDevice)) {
          this.defaultDevice = null;
          localStorage.removeItem("bluetag_device");
        }
      } catch (e) {
        this.showToast(e.message, "error");
      }
    },

    async scanDevices() {
      this.scanning = true;
      try {
        const res = await this.api("/api/v1/devices/scan", {
          method: "POST",
          params: { screen: this.screen },
        });
        const data = await res.json();
        this.showToast(`发现 ${data.total} 台 ${this.screen} 设备`);
        await this.loadDevices();
      } catch (e) {
        this.showToast(e.message, "error");
      } finally {
        this.scanning = false;
      }
    },

    buildFormData({ includeFile = false } = {}) {
      const fd = new FormData();
      if (includeFile && this.imageFile) {
        fd.append("file", this.imageFile);
      } else {
        if (!this.text.body) throw new Error("正文不能为空");
        fd.append("body", this.text.body);
        if (this.text.title) fd.append("title", this.text.title);
        fd.append("title_color", this.text.title_color);
        fd.append("body_color", this.text.body_color);
        fd.append("separator_color", this.text.separator_color);
        fd.append("align", this.text.align);
      }
      return fd;
    },

    async refreshPreview() {
      if (this.tab === "text" && !this.text.body) {
        this.previewUrl = "";
        return;
      }
      if (this.tab === "image" && !this.imageFile) {
        this.previewUrl = "";
        return;
      }
      this.previewing = true;
      try {
        const fd = this.buildFormData({ includeFile: this.tab === "image" });
        const res = await this.api("/api/v1/preview", {
          method: "POST",
          params: { screen: this.screen },
          body: fd,
        });
        const blob = await res.blob();
        if (this.previewUrl) URL.revokeObjectURL(this.previewUrl);
        this.previewUrl = URL.createObjectURL(blob);
      } catch (e) {
        this.showToast(e.message, "error");
      } finally {
        this.previewing = false;
      }
    },

    async pushText() {
      if (!this.text.body) return;
      this.busy = true;
      try {
        const fd = this.buildFormData();
        const res = await this.api("/api/v1/push", {
          method: "POST",
          params: { screen: this.screen, device: this.defaultDevice },
          body: fd,
        });
        const data = await res.json();
        this.showToast(`已推送到 ${data.device}`);
      } catch (e) {
        this.showToast(e.message, "error");
      } finally {
        this.busy = false;
      }
    },

    async pushImage() {
      if (!this.imageFile) return;
      this.busy = true;
      try {
        const fd = this.buildFormData({ includeFile: true });
        const res = await this.api("/api/v1/push", {
          method: "POST",
          params: { screen: this.screen, device: this.defaultDevice },
          body: fd,
        });
        const data = await res.json();
        this.showToast(`已推送到 ${data.device}`);
      } catch (e) {
        this.showToast(e.message, "error");
      } finally {
        this.busy = false;
      }
    },

    onFile(ev) {
      const f = ev.target.files?.[0];
      if (!f) return;
      this.imageFile = f;
      this.refreshPreview();
    },

    onDrop(ev) {
      const f = ev.dataTransfer.files?.[0];
      if (!f) return;
      this.imageFile = f;
      this.refreshPreview();
    },

    showToast(msg, kind = "info") {
      this.toast = { msg, kind };
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => (this.toast = null), 3500);
    },
  };
}
