<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('rollout.title')"
    :subtitle="tm('rollout.subtitle')"
    :loading="loading"
    :error="error"
    @refresh="load"
  >
    <v-card variant="flat" class="life-toolbar pa-3 mb-4">
      <div class="d-flex flex-wrap ga-3 align-center">
        <v-select
          v-model="days"
          :items="[7, 14, 30]"
          :label="tm('rollout.window')"
          density="compact"
          variant="outlined"
          hide-details
          style="max-width: 140px"
          @update:model-value="load"
        />
        <v-chip size="small" variant="tonal">
          {{ tm('rollout.percentChip', { percent: summary.percent }) }}
        </v-chip>
        <v-chip size="small" :color="summary.shadow ? 'warning' : 'success'" variant="tonal">
          {{ summary.shadow ? tm('rollout.shadow') : tm('rollout.liveMode') }}
        </v-chip>
      </div>
    </v-card>

    <v-row class="mb-2">
      <v-col cols="6" md="2">
        <v-card variant="tonal" class="pa-3">
          <div class="text-caption text-medium-emphasis">{{ tm('rollout.turns') }}</div>
          <div class="text-h6">{{ summary.turns }}</div>
        </v-card>
      </v-col>
      <v-col cols="6" md="2">
        <v-card variant="tonal" class="pa-3">
          <div class="text-caption text-medium-emphasis">{{ tm('rollout.reply') }}</div>
          <div class="text-h6">{{ summary.reply }}</div>
        </v-card>
      </v-col>
      <v-col cols="6" md="2">
        <v-card variant="tonal" class="pa-3">
          <div class="text-caption text-medium-emphasis">{{ tm('rollout.shadowed') }}</div>
          <div class="text-h6">{{ summary.shadowed }}</div>
        </v-card>
      </v-col>
      <v-col cols="6" md="2">
        <v-card variant="tonal" class="pa-3">
          <div class="text-caption text-medium-emphasis">{{ tm('rollout.replyRate') }}</div>
          <div class="text-h6">{{ summary.replyRate }}</div>
        </v-card>
      </v-col>
      <v-col cols="6" md="2">
        <v-card variant="tonal" class="pa-3">
          <div class="text-caption text-medium-emphasis">{{ tm('rollout.p50') }}</div>
          <div class="text-h6">{{ summary.p50 }}s</div>
        </v-card>
      </v-col>
      <v-col cols="6" md="2">
        <v-card variant="tonal" class="pa-3">
          <div class="text-caption text-medium-emphasis">{{ tm('rollout.p90') }}</div>
          <div class="text-h6">{{ summary.p90 }}s</div>
        </v-card>
      </v-col>
    </v-row>

    <v-card variant="tonal" class="pa-4 mb-4">
      <div class="d-flex align-center justify-space-between mb-2">
        <div>
          <div class="text-subtitle-1 font-weight-medium">{{ tm('rollout.configTitle') }}</div>
          <p class="text-body-2 mb-0">{{ tm('rollout.configHint') }}</p>
        </div>
        <v-btn
          variant="text"
          prepend-icon="mdi-content-save-outline"
          :loading="saving"
          @click="save"
        >
          {{ tm('rollout.save') }}
        </v-btn>
      </div>
      <v-row density="compact">
        <v-col cols="12" md="3">
          <v-select
            v-model="mode"
            :items="['allowlist', 'all']"
            :label="tm('rollout.mode')"
            density="compact"
            variant="outlined"
            hide-details
          />
        </v-col>
        <v-col cols="12" md="3">
          <v-select
            v-model.number="percent"
            :items="[0, 1, 10, 50, 100]"
            :label="tm('rollout.percent')"
            density="compact"
            variant="outlined"
            hide-details
          />
        </v-col>
        <v-col cols="12" md="4">
          <v-text-field
            v-model="salt"
            :label="tm('rollout.salt')"
            density="compact"
            variant="outlined"
            hide-details
            :placeholder="tm('rollout.saltPlaceholder')"
          />
        </v-col>
        <v-col cols="12" md="2" class="d-flex align-center">
          <v-switch
            v-model="shadow"
            :label="tm('rollout.shadow')"
            density="compact"
            hide-details
            color="warning"
          />
        </v-col>
      </v-row>
    </v-card>

    <v-row>
      <v-col cols="12" lg="5">
        <v-card variant="flat" class="life-card pa-3">
          <div class="text-subtitle-1 font-weight-medium mb-2">{{ tm('rollout.sessions') }}</div>
          <v-table density="compact">
            <thead>
              <tr>
                <th>{{ tm('rollout.columns.session') }}</th>
                <th>{{ tm('rollout.columns.turns') }}</th>
                <th>{{ tm('rollout.columns.reply') }}</th>
                <th>{{ tm('rollout.columns.last') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in sessions" :key="String(row.session_key)">
                <td class="text-caption">{{ row.session_key }}</td>
                <td>{{ row.turns }}</td>
                <td>{{ row.reply }}</td>
                <td class="text-caption">{{ formatTs(Number(row.last_ts)) }}</td>
              </tr>
              <tr v-if="!sessions.length">
                <td colspan="4" class="text-center text-medium-emphasis py-6">
                  {{ tm('rollout.empty') }}
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card>
      </v-col>

      <v-col cols="12" lg="7">
        <v-card variant="flat" class="life-card pa-3">
          <div class="text-subtitle-1 font-weight-medium mb-2">{{ tm('rollout.recent') }}</div>
          <v-table density="compact">
            <thead>
              <tr>
                <th>{{ tm('rollout.columns.time') }}</th>
                <th>{{ tm('rollout.columns.session') }}</th>
                <th>{{ tm('rollout.columns.action') }}</th>
                <th>{{ tm('rollout.columns.reason') }}</th>
                <th>{{ tm('rollout.columns.latency') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in recent" :key="index">
                <td class="text-caption">{{ formatTs(Number(row.ts)) }}</td>
                <td class="text-caption">{{ row.session_key }}</td>
                <td>
                  <v-chip
                    size="x-small"
                    variant="tonal"
                    :color="row.shadow ? 'warning' : row.decision === 'reply' ? 'success' : 'info'"
                  >
                    {{ row.shadow ? tm('rollout.shadowedChip') : row.decision }}
                  </v-chip>
                </td>
                <td class="text-caption">{{ row.reason }}</td>
                <td>{{ (Number(row.latency_ms || 0) / 1000).toFixed(1) }}s</td>
              </tr>
              <tr v-if="!recent.length">
                <td colspan="5" class="text-center text-medium-emphasis py-6">
                  {{ tm('rollout.empty') }}
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card>
      </v-col>
    </v-row>
  </LifePageShell>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { lifeApi } from '@/api/life';
import { useModuleI18n } from '@/i18n/composables';
import { formatTs } from './format';
import LifePageShell from './LifePageShell.vue';

const { tm } = useModuleI18n('features/life');

const loading = ref(false);
const saving = ref(false);
const error = ref('');
const message = ref('');
const days = ref(14);
const report = ref<Record<string, any> | null>(null);
const config = ref<Record<string, any> | null>(null);
const mode = ref('allowlist');
const percent = ref(0);
const salt = ref('');
const shadow = ref(false);

const summary = computed(() => {
  const payload = (report.value || {}) as Record<string, any>;
  const latency = (payload.latency_ms || {}) as Record<string, any>;
  const turns = Number(payload.turns || 0);
  const reply = Number(payload.reply || 0);
  const scope = ((config.value || {}).scope || {}) as Record<string, any>;
  const heartflow = ((config.value || {}).heartflow || {}) as Record<string, any>;
  return {
    turns,
    reply,
    shadowed: Number(payload.shadowed || 0),
    replyRate: `${((Number(payload.reply_rate || 0)) * 100).toFixed(1)}%`,
    p50: (Number(latency.p50 || 0) / 1000).toFixed(1),
    p90: (Number(latency.p90 || 0) / 1000).toFixed(1),
    percent: Number(scope.rollout_percent || 0),
    shadow: Boolean(((heartflow.runtime || {}) as Record<string, any>).shadow),
  };
});

const sessions = computed(() => (report.value?.sessions as Record<string, any>[]) || []);
const recent = computed(() => (report.value?.recent as Record<string, any>[]) || []);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const [rollout, configPayload] = await Promise.all([
      lifeApi.rollout(Number(days.value) || 14),
      lifeApi.config(),
    ]);
    report.value = rollout;
    config.value = configPayload;
    const scope = (configPayload.scope || {}) as Record<string, any>;
    const heartflow = (configPayload.heartflow || {}) as Record<string, any>;
    mode.value = String(scope.mode || 'allowlist');
    percent.value = Number(scope.rollout_percent || 0);
    salt.value = String(scope.rollout_salt || '');
    shadow.value = Boolean(((heartflow.runtime || {}) as Record<string, any>).shadow);
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const next = JSON.parse(JSON.stringify(config.value || {}));
    next.scope = { ...(next.scope || {}) };
    next.scope.mode = mode.value;
    next.scope.rollout_percent = Number(percent.value);
    next.scope.rollout_salt = salt.value;
    next.heartflow = { ...(next.heartflow || {}) };
    next.heartflow.runtime = { ...(next.heartflow.runtime || {}) };
    next.heartflow.runtime.shadow = shadow.value;
    await lifeApi.saveConfig(next);
    message.value = tm('rollout.saved');
    await load();
  } catch (err) {
    error.value = String(err);
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.life-toolbar {
  background: rgba(var(--v-theme-surface-variant), 0.25);
}
.life-card {
  background: rgba(var(--v-theme-surface-variant), 0.18);
}
</style>
