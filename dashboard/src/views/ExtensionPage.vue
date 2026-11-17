<script setup>
import BotConfig from "@/components/shared/BotConfig.vue";
import ConsoleDisplayer from "@/components/shared/ConsoleDisplayer.vue";
import ReadmeDialog from "@/components/shared/ReadmeDialog.vue";
import ProxySelector from "@/components/shared/ProxySelector.vue";
import UninstallConfirmDialog from "@/components/shared/UninstallConfirmDialog.vue";
import { useExtensionPage } from "./extension/useExtensionPage";
import { computed, defineAsyncComponent } from "vue";
import defaultPluginIcon from "/favicon.svg";

const props = defineProps({
  initialTab: {
    type: String,
    default: "installed",
  },
});

const InstalledPluginsTab = defineAsyncComponent(
  () => import("./extension/InstalledPluginsTab.vue"),
);
const PluginDetailPage = defineAsyncComponent(
  () => import("./extension/PluginDetailPage.vue"),
);

const pageState = useExtensionPage(props.initialTab);

const {
  tm,
  router,
  route,
  conflictDialog,
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
  loading_,
  installUrlValidation,
  versionSupportDialog,
  showUninstallDialog,
  extension_url,
  dialog,
  upload_file,
  uploadTab,
  updatableExtensions,
  resetLoadingDialog,
  handleUninstallConfirm,
  closeUpdateConfirmDialog,
  confirmUpdatePlugin,
  confirmUpdateAll,
  cancelUpdateAll,
  openExtensionConfig,
  updateConfig,
  updatePluginLogLevel,
  pluginLogLevelSaving,
  closeInstallDialog,
  continueInstallIgnoringVersionWarning,
  cancelInstallOnVersionWarning,
  newExtension,
  selectedUpdateExtension,
  selectedUpdateSourceUrl,
  installUsesRepositorySource,
  installUsesGithubArchiveSource,
  updateUsesRepositorySource,
  updateUsesGithubArchiveSource,
} = pageState;

const logLevelItems = computed(() => [
  { title: tm("dialogs.config.coreSettings.followGlobal"), value: null },
  { title: "DEBUG", value: "DEBUG" },
  { title: "INFO", value: "INFO" },
  { title: "WARNING", value: "WARNING" },
  { title: "ERROR", value: "ERROR" },
  { title: "CRITICAL", value: "CRITICAL" },
]);

const selectedPluginId = computed(() => {
  const pluginId = route.params.pluginId;
  return Array.isArray(pluginId) ? pluginId[0] : pluginId || "";
});

const selectedInstalledPlugin = computed(() => {
  if (!selectedPluginId.value) return null;
  const data = Array.isArray(extension_data?.data) ? extension_data.data : [];
  return data.find((plugin) => plugin.name === selectedPluginId.value) || null;
});

const selectedDetailPlugin = computed(() => selectedInstalledPlugin.value);

const updateDialogPluginName = computed(() => {
  const plugin = selectedUpdateExtension.value;
  return plugin ? String(plugin.display_name || plugin.name || "") : "";
});

const updateDialogCurrentVersion = computed(() =>
  String(selectedUpdateExtension.value?.version || "").trim(),
);

const updateDialogPluginLogo = computed(() => {
  const logo = selectedUpdateExtension.value?.logo;
  return typeof logo === "string" && logo.trim() ? logo : defaultPluginIcon;
});
</script>

<template>
  <PluginDetailPage
    v-if="selectedPluginId && selectedDetailPlugin"
    :plugin="selectedDetailPlugin"
    :state="pageState"
  />

  <div v-else-if="selectedPluginId && loading_" class="pa-4">
    <v-progress-linear indeterminate color="primary" />
  </div>

  <div v-else-if="selectedPluginId" class="pa-4">
    <div class="d-flex align-center mb-6">
      <v-btn
        icon="mdi-arrow-left"
        variant="text"
        density="comfortable"
        @click="router.push({ name: 'Extensions' })"
      />
      <h2 class="text-h3 mb-0 ml-2">
        {{ tm("titles.installedBotPlugins") }}
        <v-icon icon="mdi-chevron-right" size="24" class="mx-1" />
        {{ selectedPluginId }}
      </h2>
    </div>
    <v-alert type="warning" variant="tonal">
      {{ tm("detail.notFound") }}
    </v-alert>
  </div>

  <v-row v-else class="extension-page">
    <v-col cols="12" md="12">
      <v-card variant="flat" style="background-color: transparent">
        <!-- 标签页 -->
        <v-card-text style="padding: 0px 12px">
          <InstalledPluginsTab
            v-if="activeTab === 'installed'"
            :state="pageState"
          />
        </v-card-text>
      </v-card>
    </v-col>
  </v-row>

  <!-- 配置对话框 -->
  <v-dialog v-model="configDialog" max-width="900" scrollable>
    <v-card>
      <v-card-title class="text-h3 pa-4 pb-0 pl-6">{{
        tm("dialogs.config.title")
      }}</v-card-title>
      <v-card-text class="config-form plugin-config-form">
        <v-row class="config-row">
          <v-col cols="12" sm="6" class="property-info">
            <v-list-item density="compact">
              <v-list-item-title class="property-name">
                {{ tm("dialogs.config.coreSettings.logLevel") }}
              </v-list-item-title>
              <v-list-item-subtitle class="property-hint">
                {{ tm("dialogs.config.coreSettings.logLevelHint") }}
              </v-list-item-subtitle>
            </v-list-item>
          </v-col>
          <v-col cols="12" sm="6" class="config-input">
            <v-select
              :model-value="extension_config.log_level"
              :items="logLevelItems"
              :loading="pluginLogLevelSaving"
              density="compact"
              variant="outlined"
              hide-details
              @update:model-value="updatePluginLogLevel"
            ></v-select>
          </v-col>
        </v-row>
        <v-divider class="config-divider"></v-divider>
        <BotConfig
          v-if="extension_config.metadata"
          :metadata="extension_config.metadata"
          :iterable="extension_config.config"
          :metadataKey="curr_namespace"
          :pluginName="curr_namespace"
          :pluginI18n="extension_config.i18n"
          enable-default-reset
          foldable
        />
        <p v-else>{{ tm("dialogs.config.noConfig") }}</p>
      </v-card-text>
      <v-card-actions>
        <v-spacer></v-spacer>
        <v-btn
          v-if="extension_config.metadata"
          color="blue-darken-1"
          variant="text"
          @click="updateConfig"
          >{{ tm("buttons.saveAndClose") }}</v-btn
        >
        <v-btn
          color="blue-darken-1"
          variant="text"
          @click="configDialog = false"
          >{{ tm("buttons.close") }}</v-btn
        >
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 加载对话框 -->
  <v-dialog
    v-model="loadingDialog.show"
    width="700"
    persistent
    transition="dialog-transition"
  >
    <v-card>
      <v-card-title class="text-h3 pa-4 pb-0 pl-6">{{ loadingDialog.title }}</v-card-title>
      <v-card-text style="max-height: calc(100vh - 200px); overflow-y: auto">
        <v-progress-linear
          v-if="loadingDialog.statusCode === 0"
          indeterminate
          color="primary"
          class="mb-4"
        ></v-progress-linear>

        <v-fade-transition>
          <div
            v-if="loadingDialog.statusCode !== 0"
            class="py-8 text-center"
          >
            <v-icon
              class="mb-6"
              :color="loadingDialog.statusCode === 1 ? 'success' : 'error'"
              :icon="
                loadingDialog.statusCode === 1
                  ? 'mdi-check-circle-outline'
                  : 'mdi-alert-circle-outline'
              "
              size="128"
            ></v-icon>
            <div class="text-h4 font-weight-bold">{{ loadingDialog.result }}</div>
          </div>
        </v-fade-transition>

        <div style="margin-top: 32px">
          <h3>{{ tm("dialogs.loading.logs") }}</h3>
          <ConsoleDisplayer
            historyNum="10"
            style="height: 200px; margin-top: 16px; margin-bottom: 24px"
          >
          </ConsoleDisplayer>
        </div>
      </v-card-text>

      <v-divider></v-divider>

      <v-card-actions class="pa-4">
        <v-spacer></v-spacer>
        <v-btn
          color="blue-darken-1"
          variant="text"
          @click="resetLoadingDialog"
          >{{ tm("buttons.close") }}</v-btn
        >
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-snackbar
    :timeout="2000"
    elevation="6"
    :color="snack_success"
    v-model="snack_show"
    location="bottom center"
  >
    {{ snack_message }}
  </v-snackbar>

  <ReadmeDialog
    v-model:show="readmeDialog.show"
    :plugin-name="readmeDialog.pluginName"
    :repo-url="readmeDialog.repoUrl"
  />

  <!-- 插件更新日志对话框（复用 ReadmeDialog） -->
  <ReadmeDialog
    v-model:show="changelogDialog.show"
    :plugin-name="changelogDialog.pluginName"
    :repo-url="changelogDialog.repoUrl"
    mode="changelog"
  />

  <!-- 卸载插件确认对话框（列表模式用） -->
  <UninstallConfirmDialog
    v-model="showUninstallDialog"
    @confirm="handleUninstallConfirm"
  />

  <!-- 更新全部插件确认对话框 -->
  <v-dialog v-model="updateAllConfirmDialog.show" max-width="420">
    <v-card class="rounded-lg">
      <v-card-title class="text-h3 pa-4 pb-0 pl-6 d-flex align-center">
        <v-icon color="warning" class="mr-2">mdi-update</v-icon>
        {{ tm("dialogs.updateAllConfirm.title") }}
      </v-card-title>
      <v-card-text>
        <p class="text-body-1">
          {{
            tm("dialogs.updateAllConfirm.message", {
              count: updatableExtensions.length,
            })
          }}
        </p>
      </v-card-text>
      <v-card-actions class="pa-4">
        <v-spacer></v-spacer>
        <v-btn variant="text" @click="cancelUpdateAll">{{
          tm("buttons.cancel")
        }}</v-btn>
        <v-btn color="warning" variant="tonal" @click="confirmUpdateAll">{{
          tm("dialogs.updateAllConfirm.confirm")
        }}</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 指令冲突提示对话框 -->
  <v-dialog v-model="conflictDialog.show" max-width="420">
    <v-card class="rounded-lg">
      <v-card-title class="text-h3 pa-4 pb-0 pl-6 d-flex align-center">
        <v-icon color="warning" class="mr-2">mdi-alert-circle</v-icon>
        {{ tm("conflicts.title") }}
      </v-card-title>
      <v-card-text class="px-4 pb-2">
        <div class="d-flex align-center mb-3">
          <v-chip
            color="warning"
            variant="tonal"
            size="large"
            class="font-weight-bold"
          >
            {{ conflictDialog.count }}
          </v-chip>
          <span class="ml-2 text-body-1">{{ tm("conflicts.pairs") }}</span>
        </div>
        <p
          class="text-body-2"
          style="color: rgba(var(--v-theme-on-surface), 0.7)"
        >
          {{ tm("conflicts.message") }}
        </p>
      </v-card-text>
      <v-card-actions class="pa-4 pt-2">
        <v-spacer></v-spacer>
        <v-btn variant="text" @click="conflictDialog.show = false">{{
          tm("conflicts.later")
        }}</v-btn>
        <v-btn color="warning" variant="tonal" @click="handleConflictConfirm">
          {{ tm("conflicts.goToManage") }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 版本不兼容警告对话框 -->
  <v-dialog v-model="versionSupportDialog.show" width="520" persistent>
    <v-card>
      <v-card-title class="text-h3 pa-4 pb-0 pl-6 d-flex align-center">
        <v-icon color="warning" class="mr-2">mdi-alert</v-icon>
        {{ tm("dialogs.versionSupport.title") }}
      </v-card-title>
      <v-card-text>
        <div class="mb-2">{{ tm("dialogs.versionSupport.message") }}</div>
        <div class="text-medium-emphasis">
          {{ versionSupportDialog.message }}
        </div>
      </v-card-text>
      <v-card-actions>
        <v-spacer></v-spacer>
        <v-btn color="grey" variant="text" @click="cancelInstallOnVersionWarning">
          {{ tm("dialogs.versionSupport.cancel") }}
        </v-btn>
        <v-btn color="warning" variant="tonal" @click="continueInstallIgnoringVersionWarning">
          {{ tm("dialogs.versionSupport.confirm") }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 上传插件对话框 -->
  <v-dialog v-model="dialog" width="500">
    <div
      class="v-card v-card--density-default rounded-lg v-card--variant-elevated"
    >
      <v-card-title class="text-h3 pa-4 pb-0 pl-6">
        {{ tm("dialogs.install.title") }}
      </v-card-title>

      <div class="v-card-text">
        <v-tabs v-model="uploadTab" color="primary">
          <v-tab value="file">{{ tm("dialogs.install.fromFile") }}</v-tab>
          <v-tab value="url">{{ tm("dialogs.install.fromUrl") }}</v-tab>
        </v-tabs>

          <v-window v-model="uploadTab" class="mt-4">
            <v-window-item value="file">
              <div class="d-flex flex-column align-center justify-center pa-4">
                <v-file-input
                  ref="fileInput"
                  v-model="upload_file"
                  :label="tm('upload.selectFile')"
                  accept=".zip"
                  hide-details
                  hide-input
                  class="d-none"
                ></v-file-input>

                <v-btn
                  color="primary"
                  variant="tonal"
                  size="large"
                  prepend-icon="mdi-upload"
                  @click="$refs.fileInput.click()"
                >
                  {{ tm("buttons.selectFile") }}
                </v-btn>

                <div class="text-body-2 text-medium-emphasis mt-2">
                  {{ tm("messages.supportedFormats") }}
                </div>

                <div v-if="upload_file" class="mt-4 text-center">
                  <v-chip
                    color="primary"
                    size="large"
                    closable
                    @click:close="upload_file = null"
                  >
                    {{ upload_file.name }}
                    <template v-slot:append>
                      <span class="text-caption ml-2"
                        >({{ (upload_file.size / 1024).toFixed(1) }}KB)</span
                      >
                    </template>
                  </v-chip>
                </div>
              </div>
            </v-window-item>

            <v-window-item value="url">
              <div class="pa-4">
                <v-text-field
                  v-model="extension_url"
                  :label="tm('upload.enterUrl')"
                  variant="outlined"
                  prepend-inner-icon="mdi-link"
                  hide-details
                  class="rounded-lg mb-4"
                  placeholder="https://github.com/owner/repo or git@host:owner/repo.git"
                ></v-text-field>

                <v-alert
                  v-if="installUrlValidation.status === 'error'"
                  type="error"
                  variant="tonal"
                  density="comfortable"
                  class="market-install-alert mb-4"
                >
                  {{ installUrlValidation.message }}
                </v-alert>
                <div
                  v-else-if="
                    installUrlValidation.validating ||
                    installUrlValidation.status === 'valid'
                  "
                  class="d-flex align-center text-caption text-medium-emphasis mb-4"
                  style="gap: 8px"
                >
                  <v-progress-circular
                    v-if="installUrlValidation.validating"
                    indeterminate
                    size="16"
                    width="2"
                    color="primary"
                  />
                  <v-icon
                    v-else
                    icon="mdi-check-circle"
                    size="16"
                    color="success"
                  />
                  <span>
                    {{ installUrlValidation.message }}
                    <span v-if="installUrlValidation.version">
                      · {{ tm("table.headers.version") }}:
                      {{ installUrlValidation.version }}
                    </span>
                  </span>
                </div>

                <v-alert
                  v-if="installUsesRepositorySource"
                  type="warning"
                  variant="tonal"
                  density="comfortable"
                  class="market-install-alert mb-4"
                >
                  {{ tm("dialogs.install.repositorySecurityWarning") }}
                </v-alert>

                <ProxySelector
                  v-if="installUsesGithubArchiveSource"
                ></ProxySelector>
              </div>
            </v-window-item>
          </v-window>
      </div>

      <div class="v-card-actions">
        <v-spacer></v-spacer>
        <v-btn color="grey" variant="text" @click="closeInstallDialog">{{
          tm("buttons.cancel")
        }}</v-btn>
        <v-btn
          color="primary"
          variant="text"
          :loading="loading_ || installUrlValidation.validating"
          :disabled="loading_ || installUrlValidation.validating"
          @click="newExtension()"
          >{{ tm("buttons.install") }}</v-btn
        >
      </div>
    </div>
  </v-dialog>

  <!-- Update plugin confirmation dialog -->
  <v-dialog v-model="updateConfirmDialog.show" width="500">
    <v-card class="rounded-lg">
      <v-card-title class="text-h3 pa-4 pb-0 pl-6">
        {{ tm("dialogs.update.title") }}
      </v-card-title>
      <v-card-text>
        <div class="market-install-confirm">
          <div class="market-install-confirm__header">
            <img
              :src="updateDialogPluginLogo"
              :alt="updateDialogPluginName"
              class="market-install-confirm__logo"
            />
            <div class="market-install-confirm__meta">
              <div class="market-install-confirm__name">
                {{ updateDialogPluginName }}
              </div>
              <div
                v-if="updateDialogCurrentVersion"
                class="market-install-confirm__author"
              >
                {{ tm("detail.info.version") }}:
                {{ updateDialogCurrentVersion }}
              </div>
            </div>
          </div>

          <v-divider class="my-4" />

          <div
            v-if="selectedUpdateSourceUrl"
            class="market-install-confirm__section-title"
          >
            {{ tm("dialogs.update.sectionTitle") }}
          </div>
          <div
            v-if="selectedUpdateSourceUrl"
            class="market-install-source text-caption text-medium-emphasis mb-3"
          >
            <div>{{ tm("dialogs.update.downloadSource") }}</div>
            <div class="market-install-source__url">
              {{ selectedUpdateSourceUrl }}
            </div>
          </div>

          <v-alert
            v-if="updateUsesRepositorySource"
            type="warning"
            variant="tonal"
            density="comfortable"
            class="market-install-alert mt-4 mb-4"
          >
            {{ tm("dialogs.install.repositorySecurityWarning") }}
          </v-alert>

          <ProxySelector
            v-if="updateUsesGithubArchiveSource"
            class="mt-4"
          />
        </div>
      </v-card-text>
      <v-card-actions>
        <v-spacer></v-spacer>
        <v-btn color="grey" variant="text" @click="closeUpdateConfirmDialog">
          {{ tm("buttons.cancel") }}
        </v-btn>
        <v-btn
          color="primary"
          variant="tonal"
          @click="confirmUpdatePlugin"
        >
          {{ tm("dialogs.update.confirm") }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

</template>

<style scoped>
.plugin-config-form {
  /* v-card-text padding, published for the fold headers so their hover highlight
     reaches the card edges. */
  --config-group-inset: 16px;
}

.plugin-config-form > :deep(.config-section) {
  display: none;
}

.extension-page {
  margin: 0 auto;
  max-width: 1200px;
  width: 100%;
}

.plugin-handler-item {
  margin-bottom: 10px;
  padding: 5px;
  border-radius: 5px;
  background-color: #f5f5f5;
}

.fab-button {
  transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
}

.fab-button:hover {
  transform: translateY(-4px) scale(1.05);
  box-shadow: 0 12px 20px rgba(var(--v-theme-primary), 0.4);
}

.market-install-confirm {
  padding: 8px;
}

.market-install-confirm__header {
  align-items: center;
  display: flex;
  gap: 16px;
}

.market-install-confirm__logo {
  border-radius: 14px;
  height: 64px;
  object-fit: cover;
  width: 64px;
}

.market-install-confirm__meta {
  min-width: 0;
}

.market-install-confirm__name {
  color: rgba(var(--v-theme-on-surface), 0.92);
  font-size: 1.25rem;
  font-weight: 700;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.market-install-confirm__author,
.market-install-confirm__desc {
  color: rgba(var(--v-theme-on-surface), 0.64);
  line-height: 1.55;
  font-size: 0.875rem;
}

.market-install-confirm__section-title {
  color: rgba(var(--v-theme-on-surface), 0.92);
  font-weight: 700;
  margin-bottom: 8px;
}

.market-install-alert {
  font-size: 0.8125rem;
  line-height: 1.45;
}

.market-install-source {
  min-width: 0;
}

.market-install-source__url {
  overflow-x: auto;
  white-space: nowrap;
}
</style>

<style>
.v-theme--PurpleThemeDark .extension-page .plugin-handler-item {
  background-color: rgb(var(--v-theme-mcpCardBg));
}
</style>

<style scoped src="@/styles/config-form.css"></style>
