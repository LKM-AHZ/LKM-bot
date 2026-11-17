import { commandApi, pluginApi } from "@/api/v1";
import { pluginSidebarState } from "@/composables/usePluginSidebarItems";
import { useModuleI18n } from "@/i18n/composables";
import { resolveErrorMessage } from "@/utils/errorUtils";
import { getValidHashTab, replaceTabRoute } from "@/utils/hashRouteTabs.mjs";
import {
  buildSearchQuery,
  matchesPluginSearch,
  normalizeStr,
} from "@/utils/pluginSearch";
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

const buildFailedPluginItems = (raw) => {
  return Object.entries(raw || {}).map(([dirName, info]) => {
    const detail = info && typeof info === "object" ? info : {};
    return {
      ...detail,
      dir_name: dirName,
      name: detail.name || dirName,
      display_name: detail.display_name || detail.name || dirName,
      error: detail.error || "",
      traceback: detail.traceback || "",
      reserved: !!detail.reserved,
    };
  });
};

export const useExtensionPage = (initialTab = "installed") => {
  const { tm } = useModuleI18n("features/extension");
  const router = useRouter();
  const route = useRoute();

  const getSelectedGitHubProxy = () => {
    if (typeof window === "undefined" || !window.localStorage) return "";
    return localStorage.getItem("githubProxyRadioValue") === "1"
      ? localStorage.getItem("selectedGitHubProxy") || ""
      : "";
  };

  // 检查指令冲突并提示
  const conflictDialog = reactive({
    show: false,
    count: 0,
  });
  const checkAndPromptConflicts = async () => {
    try {
      const res = await commandApi.list();
      if (res.data.status === "ok") {
        const conflicts = res.data.data.summary?.conflicts || 0;
        if (conflicts > 0) {
          conflictDialog.count = conflicts;
          conflictDialog.show = true;
        }
      }
    } catch (err) {
      console.debug("Failed to check command conflicts:", err);
    }
  };
  const handleConflictConfirm = () => {
    conflictDialog.show = false;
    void router.push({ name: "ExtensionComponents" });
  };

  const fileInput = ref(null);
  const validTabs = ["installed", "mcp", "skills", "components"];
  const activeTab = ref(
    validTabs.includes(initialTab) ? initialTab : "installed",
  );
  const isValidTab = (tab) => validTabs.includes(tab);
  const getLocationHash = () => route.hash || "";
  const extractTabFromHash = (hash) => getValidHashTab(hash, validTabs);
  const syncTabFromHash = (hash) => {
    const tab = extractTabFromHash(hash);
    if (tab) {
      activeTab.value = tab;
      return true;
    }
    return false;
  };
  const extension_data = reactive({
    data: [],
    message: "",
  });

  const snack_message = ref("");
  const snack_show = ref(false);
  const snack_success = ref("success");
  const configDialog = ref(false);
  const extension_config = reactive({
    metadata: {},
    config: {},
    i18n: {},
    log_level: null,
  });
  const pluginLogLevelSaving = ref(false);
  const loadingDialog = reactive({
    show: false,
    title: "",
    statusCode: 0, // 0: loading, 1: success, 2: error,
    result: "",
  });
  const curr_namespace = ref("");
  const currentConfigPlugin = ref("");
  const updatingAll = ref(false);

  const readmeDialog = reactive({
    show: false,
    pluginName: "",
    repoUrl: null,
  });

  const updateConfirmDialog = reactive({
    show: false,
    extensionName: "",
  });

  // 更新全部插件确认对话框
  const updateAllConfirmDialog = reactive({
    show: false,
  });

  // 插件更新日志对话框（复用 ReadmeDialog）
  const changelogDialog = reactive({
    show: false,
    pluginName: "",
    repoUrl: null,
  });

  const pluginSearch = ref("");
  const loading_ = ref(false);

  const installUrlValidation = reactive({
    validating: false,
    status: "idle",
    message: "",
    version: "",
    metadata: null,
  });

  // LKMBot 版本范围不兼容警告对话框
  const versionSupportDialog = reactive({
    show: false,
    message: "",
  });

  // 卸载插件确认对话框（列表模式用）
  const showUninstallDialog = ref(false);
  const uninstallTarget = ref(null);

  // 插件安装对话框相关
  const extension_url = ref("");
  const dialog = ref(false);
  const upload_file = ref(null);
  const uploadTab = ref("file");

  // 过滤要显示的插件
  const filteredExtensions = computed(() => {
    const data = Array.isArray(extension_data?.data) ? extension_data.data : [];
    return data;
  });

  const compareInstalledPluginNames = (left, right) =>
    normalizeStr(left?.name ?? "").localeCompare(
      normalizeStr(right?.name ?? ""),
      undefined,
      {
        sensitivity: "base",
      },
    );

  const compareInstalledFallback = (left, right) => {
    const reservedDiff =
      Number(!!left.plugin?.reserved) - Number(!!right.plugin?.reserved);
    if (reservedDiff !== 0) {
      return reservedDiff;
    }

    const nameCompare = compareInstalledPluginNames(left.plugin, right.plugin);
    return nameCompare !== 0 ? nameCompare : left.index - right.index;
  };

  const sortInstalledPlugins = (plugins) => {
    return plugins
      .map((plugin, index) => ({
        plugin,
        index,
      }))
      .sort(compareInstalledFallback)
      .map((item) => item.plugin);
  };

  // 通过搜索过滤插件
  const filteredPlugins = computed(() => {
    const query = buildSearchQuery(pluginSearch.value);
    const filtered = query
      ? filteredExtensions.value.filter((plugin) =>
          matchesPluginSearch(plugin, query),
        )
      : filteredExtensions.value;

    return sortInstalledPlugins(filtered);
  });

  // 可批量更新的已安装插件（后端只对记录为 git 仓库来源的插件启用更新）
  const updatableExtensions = computed(() => {
    const data = Array.isArray(extension_data?.data) ? extension_data.data : [];
    return data.filter((ext) => ext.updates_enabled);
  });

  // 方法
  const toast = (message, success) => {
    snack_message.value = message;
    snack_show.value = true;
    snack_success.value = success;
  };

  const resetLoadingDialog = () => {
    loadingDialog.show = false;
    loadingDialog.title = tm("dialogs.loading.title");
    loadingDialog.statusCode = 0;
    loadingDialog.result = "";
  };

  const onLoadingDialogResult = (statusCode, result, timeToClose = 2000) => {
    loadingDialog.statusCode = statusCode;
    loadingDialog.result = result;
    if (timeToClose === -1) return;
    setTimeout(resetLoadingDialog, timeToClose);
  };

  const failedPluginsDict = ref({});
  const failedPluginItems = computed(() =>
    buildFailedPluginItems(failedPluginsDict.value),
  );

  const getExtensions = async ({ withLoading = true } = {}) => {
    if (withLoading) {
      loading_.value = true;
    }
    try {
      const res = await pluginApi.list();
      Object.assign(extension_data, res.data);

      // 同步插件数据到侧边栏共享状态
      pluginSidebarState.plugins = res.data?.data || [];

      const failRes = await pluginApi.failed();
      failedPluginsDict.value = failRes.data.data || {};
    } catch (err) {
      toast(err, "error");
    } finally {
      if (withLoading) {
        loading_.value = false;
      }
    }
  };

  const handleReloadAllFailed = async () => {
    const dirNames = Object.keys(failedPluginsDict.value);
    if (dirNames.length === 0) {
      toast("没有需要重载的失败插件", "info");
      return;
    }

    loading_.value = true;
    try {
      const promises = dirNames.map((dir) => pluginApi.reloadFailed(dir));
      await Promise.all(promises);

      toast("已尝试重载所有失败插件", "success");

      // 清空 message 关闭对话框
      extension_data.message = "";

      // 刷新列表
      await getExtensions();
    } catch (e) {
      console.error("重载失败:", e);
      toast("批量重载过程中出现错误", "error");
    } finally {
      loading_.value = false;
    }
  };

  const reloadFailedPlugin = async (dirName) => {
    if (!dirName) return;

    try {
      const res = await pluginApi.reloadFailed(dirName);
      if (res.data.status === "error") {
        toast(res.data.message || tm("messages.reloadFailed"), "error");
        return;
      }
      toast(res.data.message || tm("messages.reloadSuccess"), "success");
      await getExtensions();
    } catch (err) {
      toast(resolveErrorMessage(err, tm("messages.reloadFailed")), "error");
    }
  };

  const requestUninstall = (target) => {
    if (!target?.id || !target?.kind) return;
    uninstallTarget.value = target;
    showUninstallDialog.value = true;
  };

  const uninstall = async (
    target,
    { deleteConfig = false, deleteData = false, skipConfirm = false } = {},
  ) => {
    if (!target?.id || !target?.kind) return;

    if (!skipConfirm) {
      requestUninstall(target);
      return;
    }

    const isFailed = target.kind === "failed";
    const options = {
      delete_config: deleteConfig,
      delete_data: deleteData,
    };

    toast(`${tm("messages.uninstalling")} ${target.id}`, "primary");

    try {
      const res = isFailed
        ? await pluginApi.uninstallFailed(target.id, options)
        : await pluginApi.uninstall(target.id, options);
      if (res.data.status === "error") {
        toast(res.data.message, "error");
        return;
      }
      if (!isFailed) {
        Object.assign(extension_data, res.data);
      }
      toast(res.data.message, "success");
      await getExtensions();
    } catch (err) {
      toast(resolveErrorMessage(err, tm("messages.operationFailed")), "error");
    }
  };

  const requestUninstallPlugin = (name) => {
    if (!name) return;
    uninstall({ kind: "normal", id: name }, { skipConfirm: false });
  };

  const requestUninstallFailedPlugin = (dirName) => {
    if (!dirName) return;
    uninstall({ kind: "failed", id: dirName }, { skipConfirm: false });
  };

  const normalizeInstallUrl = (value) =>
    String(value || "")
      .trim()
      .replace(/\/+$/, "");

  const isGithubArchiveUrl = (value) => {
    const url = normalizeInstallUrl(value);
    return (
      /^(?:https?:\/\/(?:www\.)?)?github\.com\/[^/\s]+\/[^/\s]+(?:\.git)?(?:\/tree\/[^\s]+)?$/i.test(
        url,
      ) || /^[^/:\s]+\/[^/\s]+(?:\.git)?$/i.test(url)
    );
  };

  const isGitCloneUrl = (value) => {
    const url = normalizeInstallUrl(value);
    if (/^git@[A-Za-z0-9.-]+:[^?#\s]+$/i.test(url)) return true;
    let parsed;
    try {
      parsed = new URL(
        /^(?:https?|ssh):\/\//i.test(url) ? url : `https://${url}`,
      );
    } catch {
      return false;
    }
    if (!["http:", "https:", "ssh:"].includes(parsed.protocol)) return false;
    if (!parsed.hostname || parsed.search || parsed.hash) return false;
    if (
      ["github.com", "www.github.com"].includes(parsed.hostname.toLowerCase()) &&
      ["http:", "https:"].includes(parsed.protocol)
    ) {
      return false;
    }
    if (["http:", "https:"].includes(parsed.protocol) && parsed.username) {
      return false;
    }
    const parts = parsed.pathname.split("/").filter(Boolean);
    return parts.length >= 2 && !parts.at(-1).toLowerCase().endsWith(".zip");
  };

  const isRepositoryUrl = (value) =>
    isGithubArchiveUrl(value) || isGitCloneUrl(value);

  const getRepositoryProxy = (value) =>
    isGithubArchiveUrl(value) ? getSelectedGitHubProxy() : "";

  const getInstalledExtensionByName = (extensionName) => {
    const data = Array.isArray(extension_data?.data) ? extension_data.data : [];
    return data.find((extension) => extension.name === extensionName) || null;
  };

  const uninstallExtension = async (
    extensionName,
    optionsOrSkipConfirm = false,
  ) => {
    if (!extensionName) return;

    if (typeof optionsOrSkipConfirm === "boolean") {
      return uninstall(
        { kind: "normal", id: extensionName },
        { skipConfirm: optionsOrSkipConfirm },
      );
    }

    return uninstall(
      { kind: "normal", id: extensionName },
      { ...(optionsOrSkipConfirm || {}), skipConfirm: true },
    );
  };

  // 处理卸载确认对话框的确认事件
  const handleUninstallConfirm = async (options) => {
    const target = uninstallTarget.value;
    if (!target) return;

    try {
      await uninstall(target, { ...(options || {}), skipConfirm: true });
    } finally {
      uninstallTarget.value = null;
      showUninstallDialog.value = false;
    }
  };

  const openUpdateConfirmDialog = (extensionName) => {
    updateConfirmDialog.extensionName = extensionName;
    updateConfirmDialog.show = true;
  };

  const closeUpdateConfirmDialog = () => {
    updateConfirmDialog.show = false;
    updateConfirmDialog.extensionName = "";
  };

  const updateExtension = (extension_name) => {
    const ext = getInstalledExtensionByName(extension_name);
    if (!ext) return;

    const repoUrl = normalizeInstallUrl(
      ext.repo || ext.install_source?.repo,
    );
    if (ext.updates_enabled === false && !repoUrl) {
      toast(
        ext.update_disabled_reason || tm("messages.updateDisabled"),
        "info",
      );
      return;
    }

    // No online version check anymore; every update is a forced update.
    openUpdateConfirmDialog(extension_name);
  };

  const confirmUpdatePlugin = async () => {
    const extensionName = updateConfirmDialog.extensionName;
    const ext = getInstalledExtensionByName(extensionName);
    if (!extensionName || !ext) {
      closeUpdateConfirmDialog();
      return;
    }

    closeUpdateConfirmDialog();
    loadingDialog.title = tm("status.loading");
    loadingDialog.statusCode = 0;
    loadingDialog.result = "";
    loadingDialog.show = true;
    try {
      const res = await pluginApi.update(extensionName, {
        proxy: getRepositoryProxy(ext?.repo || ext?.install_source?.repo),
      });

      if (res.data.status === "error") {
        onLoadingDialogResult(2, res.data.message, -1);
        return;
      }

      Object.assign(extension_data, res.data);
      onLoadingDialogResult(1, res.data.message);
      setTimeout(async () => {
        toast(tm("messages.refreshing"), "info", 2000);
        try {
          await getExtensions();
          toast(tm("messages.refreshSuccess"), "success");

          // 更新完成后弹出更新日志
          viewChangelog({
            name: extensionName,
            repo: ext?.repo || null,
          });
        } catch (error) {
          const errorMsg =
            error.response?.data?.message || error.message || String(error);
          toast(`${tm("messages.refreshFailed")}: ${errorMsg}`, "error");
        }
      }, 1000);
    } catch (err) {
      toast(err, "error");
    }
  };

  // 确认强制更新
  // 显示更新全部插件确认对话框
  const showUpdateAllConfirm = () => {
    if (updatableExtensions.value.length === 0) {
      toast(tm("messages.noUpdatesAvailable"), "info");
      return;
    }
    updateAllConfirmDialog.show = true;
  };

  // 确认更新全部插件
  const confirmUpdateAll = () => {
    updateAllConfirmDialog.show = false;
    updateAllExtensions();
  };

  // 取消更新全部插件
  const cancelUpdateAll = () => {
    updateAllConfirmDialog.show = false;
  };

  const updateAllExtensions = async () => {
    if (updatingAll.value) return;
    if (updatableExtensions.value.length === 0) {
      toast(tm("messages.noUpdatesAvailable"), "info");
      return;
    }
    updatingAll.value = true;
    loadingDialog.title = tm("status.loading");
    loadingDialog.statusCode = 0;
    loadingDialog.result = "";
    loadingDialog.show = true;

    const targets = updatableExtensions.value.map((ext) => ext.name);
    try {
      const res = await pluginApi.updateMany({
        names: targets,
        proxy: getSelectedGitHubProxy(),
      });

      if (res.data.status === "error") {
        onLoadingDialogResult(
          2,
          res.data.message ||
            tm("messages.updateAllFailed", {
              failed: targets.length,
              total: targets.length,
            }),
          -1,
        );
        return;
      }

      const results = res.data.data?.results || [];
      const failures = results.filter((r) => r.status !== "ok");
      try {
        await getExtensions();
      } catch (err) {
        const errorMsg =
          err.response?.data?.message || err.message || String(err);
        failures.push({ name: "refresh", status: "error", message: errorMsg });
      }

      if (failures.length === 0) {
        onLoadingDialogResult(1, tm("messages.updateAllSuccess"));
      } else {
        const failureText = tm("messages.updateAllFailed", {
          failed: failures.length,
          total: targets.length,
        });
        const detail = failures
          .map((f) => `${f.name}: ${f.message}`)
          .join("\n");
        onLoadingDialogResult(2, `${failureText}\n${detail}`, -1);
      }
    } catch (err) {
      const errorMsg =
        err.response?.data?.message || err.message || String(err);
      onLoadingDialogResult(2, errorMsg, -1);
    } finally {
      updatingAll.value = false;
    }
  };

  const pluginOn = async (extension) => {
    const previousActivated = extension.activated;
    extension.activated = true;
    try {
      const res = await pluginApi.setEnabled(extension.name, true);
      if (res.data.status === "error") {
        extension.activated = previousActivated;
        toast(res.data.message, "error");
        return;
      }
      toast(res.data.message, "success");
      await getExtensions();

      await checkAndPromptConflicts();
    } catch (err) {
      extension.activated = previousActivated;
      toast(err, "error");
    }
  };

  const pluginOff = async (extension) => {
    const previousActivated = extension.activated;
    extension.activated = false;
    try {
      const res = await pluginApi.setEnabled(extension.name, false);
      if (res.data.status === "error") {
        extension.activated = previousActivated;
        toast(res.data.message, "error");
        return;
      }
      toast(res.data.message, "success");
      await getExtensions();
    } catch (err) {
      extension.activated = previousActivated;
      toast(err, "error");
    }
  };

  const openExtensionConfig = async (extension_name) => {
    curr_namespace.value = extension_name;
    currentConfigPlugin.value = extension_name;
    configDialog.value = true;
    // Drop the previous plugin's schema immediately: the dialog keeps rendering the old
    // metadata while the request is in flight, which would let the user toggle groups of
    // one plugin under another plugin's namespace.
    extension_config.metadata = {};
    extension_config.config = {};
    extension_config.log_level = null;
    try {
      const res = await pluginApi.config(extension_name);
      // Discard the response if the user has already switched to another
      // plugin's config dialog while the request was in flight.
      if (curr_namespace.value !== extension_name) return;
      extension_config.metadata = res.data.data.metadata;
      extension_config.config = res.data.data.config;
      extension_config.i18n = res.data.data.i18n || {};
      extension_config.log_level = res.data.data.log_level ?? null;
    } catch (err) {
      toast(err, "error");
    }
  };

  const updatePluginLogLevel = async (level) => {
    if (pluginLogLevelSaving.value) return;
    const pluginName = curr_namespace.value;
    const previous = extension_config.log_level;
    extension_config.log_level = level;
    pluginLogLevelSaving.value = true;
    try {
      const res = await pluginApi.updateLogLevel(pluginName, level);
      if (res.data.status === "ok") {
        const serverLevel = res.data.data?.log_level;
        // Preserve null as "follow global" and only skip synchronization
        // when an older server does not return the field.
        if (
          curr_namespace.value === pluginName &&
          serverLevel !== undefined
        ) {
          extension_config.log_level = serverLevel;
        }
        toast(tm("messages.logLevelUpdated"), "success");
      } else {
        // Roll back the optimistic update, unless the dialog has already
        // been switched to another plugin.
        if (curr_namespace.value === pluginName) {
          extension_config.log_level = previous;
        }
        toast(res.data.message || tm("messages.operationFailed"), "error");
      }
    } catch (err) {
      if (curr_namespace.value === pluginName) {
        extension_config.log_level = previous;
      }
      toast(err, "error");
    } finally {
      pluginLogLevelSaving.value = false;
    }
  };

  const updateConfig = async () => {
    loadingDialog.title = tm("status.loading");
    loadingDialog.statusCode = 0;
    loadingDialog.result = "";
    loadingDialog.show = true;
    try {
      const res = await pluginApi.updateConfig(
        curr_namespace.value,
        extension_config.config,
      );
      if (res.data.status !== "ok") {
        toast(res.data.message, "error");
        onLoadingDialogResult(2, res.data.message, -1);
        return;
      }

      toast(res.data.message, "success");
      onLoadingDialogResult(1, res.data.message);
      configDialog.value = false;
      currentConfigPlugin.value = "";
      extension_config.metadata = {};
      extension_config.config = {};
      extension_config.i18n = {};
      extension_config.log_level = null;
      getExtensions();
    } catch (err) {
      const errMsg = resolveErrorMessage(err, tm("messages.operationFailed"));
      toast(errMsg, "error");
      onLoadingDialogResult(2, errMsg, -1);
    }
  };

  const showPluginInfo = (plugin) => {
    if (!plugin?.name) return;
    router.push({
      name: "ExtensionDetails",
      params: { pluginId: plugin.name },
      hash: "#plugin-components",
    });
  };

  const reloadPlugin = async (plugin_name) => {
    try {
      const res = await pluginApi.reload(plugin_name);
      if (res.data.status === "error") {
        toast(res.data.message || tm("messages.reloadFailed"), "error");
        return;
      }
      toast(tm("messages.reloadSuccess"), "success");
      await getExtensions();
    } catch (err) {
      toast(resolveErrorMessage(err, tm("messages.reloadFailed")), "error");
    }
  };

  const viewReadme = (plugin) => {
    readmeDialog.pluginName = plugin.name;
    readmeDialog.repoUrl = plugin.repo;
    readmeDialog.show = true;
  };

  // 查看更新日志
  const viewChangelog = (plugin) => {
    changelogDialog.pluginName = plugin.name;
    changelogDialog.repoUrl = plugin.repo;
    changelogDialog.show = true;
  };

  const resetInstallDialogState = () => {
    extension_url.value = "";
    upload_file.value = null;
    uploadTab.value = "file";
  };

  const openInstallDialog = () => {
    resetInstallDialogState();
    dialog.value = true;
  };

  const closeInstallDialog = () => {
    dialog.value = false;
    resetInstallDialogState();
  };

  const selectedInstallSourceUrl = computed(() =>
    String(extension_url.value || "").trim(),
  );

  const installUsesRepositorySource = computed(() =>
    isRepositoryUrl(extension_url.value),
  );

  const installUsesGithubArchiveSource = computed(() =>
    isGithubArchiveUrl(extension_url.value),
  );

  const resetInstallUrlValidation = () => {
    installUrlValidation.validating = false;
    installUrlValidation.status = "idle";
    installUrlValidation.message = "";
    installUrlValidation.version = "";
    installUrlValidation.metadata = null;
  };

  const validatePluginRepo = async (payload) => {
    const res = await pluginApi.validateRepo(payload);
    if (res.data.status === "error") {
      throw new Error(res.data.message || tm("messages.pluginValidateFailed"));
    }
    return {
      data: res.data.data || {},
      message: res.data.message || tm("messages.pluginValidateSuccess"),
    };
  };

  const buildInstallRepoValidationPayload = () => {
    const url = String(extension_url.value || "").trim();
    if (!url) {
      return null;
    }
    return {
      url,
      proxy: getRepositoryProxy(url),
    };
  };

  const showVersionSupportWarning = (message) => {
    versionSupportDialog.message = message;
    versionSupportDialog.show = true;
  };

  const refreshExtensionsAfterInstallFailure = async () => {
    try {
      await getExtensions();
    } catch (error) {
      console.debug(
        "Failed to refresh extensions after install failure:",
        error,
      );
    }
  };

  const continueInstallIgnoringVersionWarning = async () => {
    versionSupportDialog.show = false;
    await newExtension(true);
  };

  const cancelInstallOnVersionWarning = () => {
    versionSupportDialog.show = false;
  };

  const handleInstallResponse = async (resData) => {
    if (
      resData.status === "warning" &&
      resData.data?.warning_type === "bot_version_unsupported"
    ) {
      toast(resData.message, "warning");
      showVersionSupportWarning(resData.message);
      await refreshExtensionsAfterInstallFailure();
      return false;
    }

    if (resData.status === "error") {
      toast(resData.message, "error");
      await refreshExtensionsAfterInstallFailure();
      return false;
    }

    return true;
  };

  const performInstallRequest = async ({ source, ignoreVersionCheck }) => {
    const shouldIgnoreVersionCheck = ignoreVersionCheck === true;
    if (source === "file") {
      const formData = new FormData();
      formData.append("file", upload_file.value);
      formData.append("ignore_version_check", String(shouldIgnoreVersionCheck));
      return pluginApi.installUpload(formData);
    }

    const payload = {
      url: extension_url.value,
      proxy: getRepositoryProxy(extension_url.value),
      ignore_version_check: shouldIgnoreVersionCheck,
    };

    if (!installUsesRepositorySource.value) {
      return pluginApi.installUrl(payload);
    }
    return isGithubArchiveUrl(extension_url.value)
      ? pluginApi.installGithub(payload)
      : pluginApi.installGit(payload);
  };

  const finalizeSuccessfulInstall = async (resData, source) => {
    if (source === "file") {
      upload_file.value = null;
    } else {
      extension_url.value = "";
    }

    toast(resData.message, "success");
    dialog.value = false;
    await getExtensions();

    viewReadme({
      name: resData.data.name,
      repo: resData.data.repo || null,
    });

    await checkAndPromptConflicts();
  };

  const newExtension = async (ignoreVersionCheck = false) => {
    const shouldIgnoreVersionCheck = ignoreVersionCheck === true;
    if (extension_url.value === "" && upload_file.value === null) {
      toast(tm("messages.fillUrlOrFile"), "error");
      return;
    }

    if (extension_url.value !== "" && upload_file.value !== null) {
      toast(tm("messages.dontFillBoth"), "error");
      return;
    }
    const source = upload_file.value !== null ? "file" : "url";
    loading_.value = true;

    try {
      if (source === "url") {
        if (!installUsesRepositorySource.value) {
          toast(tm("messages.invalidRepositoryUrl"), "error");
          loading_.value = false;
          return;
        }

        const validationPayload = buildInstallRepoValidationPayload();
        if (!validationPayload) {
          toast(tm("messages.fillUrlOrFile"), "error");
          loading_.value = false;
          return;
        }
        installUrlValidation.validating = true;
        installUrlValidation.status = "loading";
        installUrlValidation.message = tm("messages.validatingPlugin");
        installUrlValidation.version = "";
        installUrlValidation.metadata = null;
        try {
          const { data, message } = await validatePluginRepo(validationPayload);
          installUrlValidation.status = "valid";
          installUrlValidation.message = message;
          installUrlValidation.version = data.version || "";
          installUrlValidation.metadata = data;
        } catch (error) {
          const message = resolveErrorMessage(
            error,
            tm("messages.pluginValidateFailed"),
          );
          installUrlValidation.status = "error";
          installUrlValidation.message = message;
          toast(message, "error");
          loading_.value = false;
          return;
        } finally {
          installUrlValidation.validating = false;
        }
      }

      const res = await performInstallRequest({
        source,
        ignoreVersionCheck: shouldIgnoreVersionCheck,
      });
      loading_.value = false;

      const canContinue = await handleInstallResponse(res.data);
      if (!canContinue) return;

      await finalizeSuccessfulInstall(res.data, source);
    } catch (err) {
      loading_.value = false;
      const message = resolveErrorMessage(err, tm("messages.installFailed"));
      toast(message, "error");
      await refreshExtensionsAfterInstallFailure();
    }
  };

  const selectedUpdateExtension = computed(() =>
    getInstalledExtensionByName(updateConfirmDialog.extensionName),
  );

  const selectedUpdateSourceUrl = computed(() =>
    String(selectedUpdateExtension.value?.repo || "").trim(),
  );

  const updateUsesRepositorySource = computed(() =>
    isRepositoryUrl(selectedUpdateSourceUrl.value),
  );

  const updateUsesGithubArchiveSource = computed(() =>
    isGithubArchiveUrl(selectedUpdateSourceUrl.value),
  );

  // 生命周期
  onMounted(async () => {
    const hasRouteTab = isValidTab(route.meta.extensionTab);
    if (!hasRouteTab && !syncTabFromHash(getLocationHash())) {
      await replaceTabRoute(router, route, activeTab.value);
    }
    loading_.value = true;
    try {
      await getExtensions({ withLoading: false });

      // 检查是否有 open_config 参数
      const plugin_name = Array.isArray(route.query.open_config)
        ? route.query.open_config[0]
        : route.query.open_config;
      if (plugin_name) {
        console.log(`Opening config for plugin: ${plugin_name}`);
        openExtensionConfig(plugin_name);
      }
    } catch (err) {
      toast(err, "error");
    } finally {
      loading_.value = false;
    }
  });

  // 处理语言切换事件，重新加载插件配置以获取插件的 i18n 数据
  const handleLocaleChange = () => {
    // 如果配置对话框是打开的，重新加载当前插件的配置
    if (configDialog.value && currentConfigPlugin.value) {
      openExtensionConfig(currentConfigPlugin.value);
    }
  };

  // 监听语言切换事件
  window.addEventListener("lkmbot-locale-changed", handleLocaleChange);

  // 清理事件监听器
  onUnmounted(() => {
    window.removeEventListener("lkmbot-locale-changed", handleLocaleChange);
  });

  watch(
    [() => dialog.value, () => extension_url.value, () => uploadTab.value],
    () => {
      resetInstallUrlValidation();
    },
  );

  watch(
    () => route.hash,
    (newHash) => {
      if (isValidTab(route.meta.extensionTab)) return;
      const tab = extractTabFromHash(newHash);
      if (tab && tab !== activeTab.value) {
        activeTab.value = tab;
      }
    },
  );

  watch(activeTab, (newTab) => {
    if (isValidTab(route.meta.extensionTab)) return;
    if (!isValidTab(newTab)) return;
    if (route.hash === `#${newTab}`) return;
    void replaceTabRoute(router, route, newTab);
  });

  return {
    tm,
    router,
    route,
    conflictDialog,
    checkAndPromptConflicts,
    handleConflictConfirm,
    fileInput,
    activeTab,
    extension_data,
    snack_message,
    snack_show,
    snack_success,
    configDialog,
    extension_config,
    loadingDialog,
    curr_namespace,
    updatingAll,
    readmeDialog,
    updateConfirmDialog,
    updateAllConfirmDialog,
    changelogDialog,
    pluginSearch,
    loading_,
    installUrlValidation,
    versionSupportDialog,
    showUninstallDialog,
    extension_url,
    dialog,
    upload_file,
    uploadTab,
    filteredPlugins,
    updatableExtensions,
    resetLoadingDialog,
    failedPluginItems,
    reloadFailedPlugin,
    uninstallExtension,
    requestUninstallFailedPlugin,
    handleUninstallConfirm,
    updateExtension,
    closeUpdateConfirmDialog,
    confirmUpdatePlugin,
    showUpdateAllConfirm,
    confirmUpdateAll,
    cancelUpdateAll,
    pluginOn,
    pluginOff,
    openExtensionConfig,
    updateConfig,
    updatePluginLogLevel,
    pluginLogLevelSaving,
    showPluginInfo,
    reloadPlugin,
    viewReadme,
    viewChangelog,
    openInstallDialog,
    closeInstallDialog,
    continueInstallIgnoringVersionWarning,
    cancelInstallOnVersionWarning,
    newExtension,
    selectedInstallSourceUrl,
    installUsesRepositorySource,
    installUsesGithubArchiveSource,
    selectedUpdateExtension,
    selectedUpdateSourceUrl,
    updateUsesRepositorySource,
    updateUsesGithubArchiveSource,
  };
};
