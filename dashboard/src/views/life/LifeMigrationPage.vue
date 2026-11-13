<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('migration.title')"
    :subtitle="tm('migration.subtitle')"
    :loading="loading"
    :error="error"
    @refresh="load"
  >
    <v-row>
      <v-col cols="12" md="6">
        <v-card variant="tonal" class="pa-4 h-100">
          <div class="text-subtitle-1 font-weight-medium mb-1">
            {{ tm('migration.memoryTitle') }}
          </div>
          <p class="text-body-2 mb-3">{{ tm('migration.memoryHint') }}</p>
          <v-text-field
            v-model="exportUmo"
            :label="tm('migration.exportUmo')"
            density="compact"
            variant="outlined"
            hide-details
            class="mb-3"
          />
          <v-btn variant="tonal" prepend-icon="mdi-download" @click="exportMemory">
            {{ tm('migration.memoryExport') }}
          </v-btn>
        </v-card>
      </v-col>

      <v-col cols="12" md="6">
        <v-card variant="tonal" class="pa-4 h-100">
          <div class="text-subtitle-1 font-weight-medium mb-1">
            {{ tm('migration.statusTitle') }}
          </div>
          <p class="text-body-2 mb-3">{{ tm('migration.statusHint') }}</p>
          <div class="d-flex flex-wrap ga-2">
            <v-chip
              v-for="(value, key) in statusChips"
              :key="key"
              size="small"
              variant="flat"
            >
              {{ key }}: {{ value }}
            </v-chip>
          </div>
        </v-card>
      </v-col>

      <v-col cols="12">
        <v-card variant="tonal" class="pa-4">
          <div class="text-subtitle-1 font-weight-medium mb-1">
            {{ tm('migration.importTitle') }}
          </div>
          <p class="text-body-2 mb-3">{{ tm('migration.importHint') }}</p>
          <v-row density="compact">
            <v-col cols="12" md="4">
              <v-text-field
                v-model="source"
                :label="tm('migration.source')"
                :placeholder="tm('migration.sourcePlaceholder')"
                density="compact"
                variant="outlined"
                hide-details
              />
            </v-col>
            <v-col cols="12" md="3">
              <v-text-field
                v-model="targetUmo"
                :label="tm('migration.targetUmo')"
                :placeholder="tm('migration.targetsPlaceholder')"
                density="compact"
                variant="outlined"
                hide-details
              />
            </v-col>
            <v-col cols="12" md="3">
              <v-text-field
                v-model="memorySource"
                :label="tm('migration.memorySource')"
                density="compact"
                variant="outlined"
                hide-details
              />
            </v-col>
            <v-col cols="12" md="2">
              <v-text-field
                v-model.number="previewLimit"
                :label="tm('migration.limit')"
                type="number"
                min="1"
                max="5000"
                density="compact"
                variant="outlined"
                hide-details
              />
            </v-col>
          </v-row>
          <div class="d-flex align-center ga-2 mt-3">
            <v-btn
              variant="tonal"
              prepend-icon="mdi-magnify-scan"
              :loading="previewing"
              @click="preview"
            >
              {{ tm('migration.preview') }}
            </v-btn>
            <span class="text-caption text-medium-emphasis">
              {{ tm('migration.importCli') }}
            </span>
          </div>

          <v-alert
            v-if="previewResult"
            type="info"
            variant="tonal"
            density="compact"
            class="mt-3"
          >
            <div class="text-body-2">
              {{ tm('migration.previewTitle') }}: {{ previewResult.report?.source }}
            </div>
            <v-table density="compact" class="mt-2">
              <thead>
                <tr>
                  <th>{{ tm('migration.columns.dataset') }}</th>
                  <th>{{ tm('migration.columns.seen') }}</th>
                  <th>{{ tm('migration.columns.ready') }}</th>
                  <th>{{ tm('migration.columns.skipped') }}</th>
                  <th>{{ tm('migration.columns.truncated') }}</th>
                  <th>{{ tm('migration.columns.errors') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(block, name) in previewResult.report?.datasets || {}" :key="name">
                  <td>{{ name }}</td>
                  <td>{{ block.seen }}</td>
                  <td>{{ block.ready }}</td>
                  <td>{{ block.skipped }}</td>
                  <td>{{ block.truncated }}</td>
                  <td>{{ block.errors }}</td>
                </tr>
              </tbody>
            </v-table>
            <div v-if="previewWarnings.length" class="text-caption mt-2">
              {{ tm('migration.warnings') }}: {{ previewWarnings.join('; ') }}
            </div>
            <div v-if="previewUnmapped.length" class="text-caption">
              {{ tm('migration.unmapped') }}: {{ previewUnmapped.join(', ') }}
            </div>
          </v-alert>

          <v-table density="compact" class="mt-4">
            <thead>
              <tr>
                <th>{{ tm('migration.runBatch') }}</th>
                <th>{{ tm('migration.runStatus') }}</th>
                <th>{{ tm('migration.runCreated') }}</th>
                <th>{{ tm('migration.columns.dataset') }}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="run in runs" :key="String(run.batch_id)">
                <td class="text-caption">{{ run.batch_id }}</td>
                <td>
                  <v-chip
                    size="x-small"
                    variant="tonal"
                    :color="run.status === 'completed' ? 'success' : run.status === 'failed' ? 'error' : 'info'"
                  >
                    {{ run.status }}
                  </v-chip>
                </td>
                <td class="text-caption">{{ formatTs(Number(run.created_at)) }}</td>
                <td class="text-caption">{{ (run.datasets || []).join(', ') }}</td>
                <td class="text-end">
                  <v-btn
                    v-if="run.status === 'completed'"
                    variant="text"
                    size="small"
                    color="error"
                    @click="askRollback(String(run.batch_id))"
                  >
                    {{ tm('migration.rollback') }}
                  </v-btn>
                </td>
              </tr>
              <tr v-if="!runs.length">
                <td colspan="5" class="text-center text-medium-emphasis py-6">
                  {{ tm('migration.runsEmpty') }}
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card>
      </v-col>

      <v-col cols="12">
        <v-card variant="tonal" class="pa-4">
          <div class="d-flex align-center justify-space-between mb-2">
            <div>
              <div class="text-subtitle-1 font-weight-medium">
                {{ tm('migration.configTitle') }}
              </div>
              <p class="text-body-2 mb-0">{{ tm('migration.configHint') }}</p>
            </div>
            <v-btn
              variant="text"
              prepend-icon="mdi-content-save-outline"
              :loading="saving"
              @click="saveConfig"
            >
              {{ tm('migration.save') }}
            </v-btn>
          </div>
          <v-textarea
            v-model="configText"
            variant="outlined"
            density="compact"
            rows="18"
            spellcheck="false"
            :error-messages="configError ? [configError] : []"
          />
        </v-card>
      </v-col>
    </v-row>

    <LifeConfirmDialog
      v-model="rollbackOpen"
      :title="tm('migration.rollbackTitle')"
      :text="tm('migration.rollbackText', { batch: rollbackBatch })"
      :loading="rollingBack"
      @confirm="runRollback"
    />
  </LifePageShell>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { lifeApi } from '@/api/life';
import { useModuleI18n } from '@/i18n/composables';
import LifeConfirmDialog from './LifeConfirmDialog.vue';
import { downloadJson, formatTs } from './format';
import LifePageShell from './LifePageShell.vue';

const { tm } = useModuleI18n('features/life');

const loading = ref(false);
const saving = ref(false);
const previewing = ref(false);
const rollingBack = ref(false);
const error = ref('');
const message = ref('');
const configError = ref('');
const configText = ref('');
const exportUmo = ref('');
const status = ref<Record<string, any> | null>(null);
const source = ref('');
const targetUmo = ref('');
const memorySource = ref('');
const previewLimit = ref(500);
const previewResult = ref<Record<string, any> | null>(null);
const runs = ref<Record<string, any>[]>([]);
const rollbackOpen = ref(false);
const rollbackBatch = ref('');

const statusChips = computed(() => {
  const payload = (status.value || {}) as Record<string, any>;
  return {
    enabled: String(payload.enabled ?? false),
    configured: String(payload.configured ?? false),
    sessions: String(payload.sessions ?? 0),
    started_at: String(payload.started_at ?? 0),
  };
});

const previewWarnings = computed(() => {
  const payload = previewResult.value;
  if (!payload) return [];
  const inspected = (payload.inspection?.warnings as string[]) || [];
  // The dry run drops datasets it cannot convert (e.g. stickers without a
  // target session); surface those alongside the inspection warnings.
  const converted = (payload.report?.warnings as string[]) || [];
  return [...inspected, ...converted];
});
const previewUnmapped = computed(
  () => (previewResult.value?.inspection?.sessions?.unmapped_platforms as string[]) || [],
);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const [config, statusPayload, runPayload] = await Promise.all([
      lifeApi.config(),
      lifeApi.status(),
      lifeApi.migrationRuns(),
    ]);
    configText.value = JSON.stringify(config, null, 2);
    status.value = statusPayload;
    runs.value = runPayload.runs || [];
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
  }
}

async function exportMemory() {
  try {
    const payload = await lifeApi.memoryExport(exportUmo.value || undefined);
    downloadJson(`life-memory-${Date.now()}.json`, payload);
  } catch (err) {
    error.value = String(err);
  }
}

async function preview() {
  previewing.value = true;
  error.value = '';
  try {
    const targets = targetUmo.value
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
    previewResult.value = await lifeApi.migrationPreview({
      source: source.value.trim(),
      memory_source: memorySource.value.trim() || undefined,
      umos: targets,
      limit: Math.min(Math.max(Number(previewLimit.value) || 500, 1), 5000),
    });
  } catch (err) {
    error.value = String(err);
  } finally {
    previewing.value = false;
  }
}

function askRollback(batch: string) {
  rollbackBatch.value = batch;
  rollbackOpen.value = true;
}

async function runRollback() {
  rollingBack.value = true;
  try {
    const result = await lifeApi.migrationRollback(rollbackBatch.value);
    const deleted = Object.values(
      (result.deleted as Record<string, number>) || {},
    ).reduce((sum, value) => sum + Number(value || 0), 0);
    message.value = tm('migration.rolledBack', { count: deleted });
    rollbackOpen.value = false;
    await load();
  } catch (err) {
    error.value = String(err);
  } finally {
    rollingBack.value = false;
  }
}

async function saveConfig() {
  saving.value = true;
  configError.value = '';
  try {
    const parsed = JSON.parse(configText.value);
    await lifeApi.saveConfig(parsed);
    message.value = tm('migration.saved');
    await load();
  } catch (err) {
    configError.value = String(err);
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>
