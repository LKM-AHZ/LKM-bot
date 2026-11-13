<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('monitor.title')"
    :subtitle="tm('monitor.subtitle')"
    :loading="loading"
    :error="error"
    @refresh="load"
  >
    <v-card variant="flat" class="life-toolbar pa-3 mb-4">
      <div class="d-flex flex-wrap ga-3 align-center">
        <v-text-field
          v-model="session"
          :label="tm('monitor.session')"
          density="compact"
          variant="outlined"
          hide-details
          clearable
          style="min-width: 280px"
          @keyup.enter="load"
        />
        <v-text-field
          v-model.number="limit"
          :label="tm('memory.limit')"
          type="number"
          min="1"
          max="200"
          density="compact"
          variant="outlined"
          hide-details
          style="max-width: 120px"
          @update:model-value="load"
        />
      </div>
    </v-card>

    <v-row v-if="status" class="mb-2">
      <v-col cols="12" md="6">
        <v-card variant="tonal" class="pa-3">
          <div class="text-caption text-medium-emphasis">{{ tm('monitor.counters') }}</div>
          <div class="d-flex flex-wrap ga-2 mt-2">
            <v-chip
              v-for="(value, key) in status.counters || {}"
              :key="key"
              size="small"
              variant="flat"
            >
              {{ key }}: {{ value }}
            </v-chip>
          </div>
        </v-card>
      </v-col>
      <v-col cols="12" md="6">
        <v-card variant="tonal" class="pa-3">
          <div class="text-caption text-medium-emphasis">{{ tm('monitor.ring') }}</div>
          <div class="text-body-2 mt-2">
            {{ tm('monitor.ringHint', {
              records: status.records ?? 0,
              size: status.recent_size ?? 0,
            }) }}
          </div>
        </v-card>
      </v-col>
    </v-row>

    <v-table density="compact" class="life-table">
      <thead>
        <tr>
          <th>{{ tm('monitor.columns.time') }}</th>
          <th>{{ tm('monitor.columns.kind') }}</th>
          <th>{{ tm('monitor.columns.session') }}</th>
          <th>{{ tm('monitor.columns.action') }}</th>
          <th>{{ tm('monitor.columns.likelihood') }}</th>
          <th>{{ tm('monitor.columns.chars') }}</th>
          <th>{{ tm('monitor.columns.reason') }}</th>
          <th>{{ tm('monitor.columns.reasoning') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in records" :key="index">
          <td class="text-caption">{{ formatTs(Number(row.created_at)) }}</td>
          <td>
            <v-chip
              size="x-small"
              variant="tonal"
              :color="row.kind === 'decision' ? 'info' : 'success'"
            >
              {{ row.kind }}
            </v-chip>
          </td>
          <td class="text-caption">{{ row.session_id }}</td>
          <td>{{ row.action }}</td>
          <td>
            <span v-if="row.kind === 'decision'">
              {{ row.likelihood }} / {{ row.threshold }}
            </span>
            <span v-else>-</span>
          </td>
          <td>{{ row.chars }}</td>
          <td class="text-caption">{{ row.reason }}</td>
          <td class="life-content">{{ truncate(row.reasoning, 120) }}</td>
        </tr>
        <tr v-if="!records.length">
          <td colspan="8" class="text-center text-medium-emphasis py-6">
            {{ tm('monitor.empty') }}
          </td>
        </tr>
      </tbody>
    </v-table>
  </LifePageShell>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { lifeApi } from '@/api/life';
import { useModuleI18n } from '@/i18n/composables';
import { formatTs, truncate } from './format';
import LifePageShell from './LifePageShell.vue';

const { tm } = useModuleI18n('features/life');

const session = ref('');
const limit = ref(50);
const records = ref<Record<string, any>[]>([]);
const status = ref<Record<string, any> | null>(null);
const loading = ref(false);
const error = ref('');
const message = ref('');

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const payload = await lifeApi.monitor({
      session: session.value || undefined,
      limit: Math.min(Math.max(Number(limit.value) || 50, 1), 200),
    });
    records.value = (payload.records as Record<string, any>[]) || [];
    status.value = (payload.status as Record<string, any>) || null;
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.life-toolbar {
  background: rgba(var(--v-theme-surface-variant), 0.25);
}
.life-content {
  max-width: 320px;
  white-space: normal;
  word-break: break-word;
}
</style>
