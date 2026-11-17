import { defineStore } from "pinia";
import { statsApi } from "@/api/v1";

export const useCommonStore = defineStore("common", {
  state: () => ({
    startTime: -1,
    botVersion: "",
    dashboardVersion: "",
  }),
  actions: {
    async fetchStartTime() {
      const res = await statsApi.startTime();
      this.startTime = res.data.data.start_time;
      return this.startTime;
    },
    setBotVersion(version, dashboardVersion = "") {
      this.botVersion = String(version || "").replace(/^v/i, "");
      this.dashboardVersion = String(dashboardVersion || "");
    },
    async fetchBotVersion(force = false) {
      if (!force && this.botVersion) {
        return this.botVersion;
      }
      const res = await statsApi.version();
      const data = res.data?.data || {};
      this.setBotVersion(data.version, data.dashboard_version);
      return this.botVersion;
    },
    getStartTime() {
      if (this.startTime !== -1) {
        return this.startTime;
      }
      this.fetchStartTime().catch(() => {});
      return this.startTime;
    },
  },
});
