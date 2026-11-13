<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('memory.title')"
    :subtitle="tm('memory.subtitle')"
    :loading="loading"
    :error="error"
    @refresh="load"
  >
    <template #actions>
      <v-btn variant="tonal" prepend-icon="mdi-download" @click="exportAll">
        {{ tm('memory.export') }}
      </v-btn>
    </template>

    <v-card variant="flat" class="life-toolbar pa-3 mb-4">
      <div class="d-flex flex-wrap ga-3 align-center">
        <v-select
          v-model="kind"
          :items="kinds"
          :label="tm('memory.kind')"
          density="compact"
          variant="outlined"
          hide-details
          clearable
          style="max-width: 200px"
          @update:model-value="load"
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
        <v-text-field
          v-model="searchUmo"
          :label="tm('memory.searchUmo')"
          density="compact"
          variant="outlined"
          hide-details
          style="min-width: 240px"
          @keyup.enter="search"
        />
        <v-text-field
          v-model="searchQuery"
          :label="tm('memory.searchQuery')"
          density="compact"
          variant="outlined"
          hide-details
          style="min-width: 200px"
          @keyup.enter="search"
        />
        <v-btn variant="tonal" prepend-icon="mdi-magnify" @click="search">
          {{ tm('memory.search') }}
        </v-btn>
      </div>
    </v-card>

    <v-table density="compact" class="life-table">
      <thead>
        <tr>
          <th>{{ tm('memory.columns.id') }}</th>
          <th>{{ tm('memory.columns.kind') }}</th>
          <th>{{ tm('memory.columns.session') }}</th>
          <th>{{ tm('memory.columns.content') }}</th>
          <th>{{ tm('memory.columns.importance') }}</th>
          <th>{{ tm('memory.columns.ts') }}</th>
          <th class="text-right">{{ tm('common.actions') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in items" :key="String(row.id)">
          <td>{{ row.id }}</td>
          <td>{{ row.kind }}</td>
          <td class="text-caption">{{ row.session_key }}</td>
          <td class="life-content">{{ truncate(row.content, 120) }}</td>
          <td>{{ row.importance }}</td>
          <td class="text-caption">{{ formatTs(Number(row.ts)) }}</td>
          <td class="text-right">
            <v-btn
              icon="mdi-delete-outline"
              size="small"
              variant="text"
              color="error"
              @click="askDelete(row)"
            />
          </td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="7" class="text-center text-medium-emphasis py-6">
            {{ tm('memory.empty') }}
          </td>
        </tr>
      </tbody>
    </v-table>

    <v-dialog v-model="searchOpen" max-width="760">
      <v-card>
        <v-card-title class="text-h6">{{ tm('memory.searchTitle') }}</v-card-title>
        <v-card-text>
          <v-table density="compact">
            <thead>
              <tr>
                <th>{{ tm('memory.columns.kind') }}</th>
                <th>{{ tm('memory.columns.content') }}</th>
                <th>{{ tm('memory.columns.score') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(hit, index) in searchHits" :key="index">
                <td>{{ hit.kind }}</td>
                <td class="life-content">{{ truncate(hit.content, 160) }}</td>
                <td>{{ hit.score }}</td>
              </tr>
              <tr v-if="!searchHits.length">
                <td colspan="3" class="text-center py-4">
                  {{ tm('memory.noHits') }}
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>
      </v-card>
    </v-dialog>

    <LifeConfirmDialog
      v-model="confirmOpen"
      :title="tm('memory.deleteTitle')"
      :text="tm('memory.deleteHint')"
      :loading="deleting"
      @confirm="confirmDelete"
    />
  </LifePageShell>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { lifeApi } from '@/api/life';
import { useModuleI18n } from '@/i18n/composables';
import { downloadJson, formatTs, truncate } from './format';
import LifeConfirmDialog from './LifeConfirmDialog.vue';
import LifePageShell from './LifePageShell.vue';

const { tm } = useModuleI18n('features/life');

const kinds = ['summary', 'fact', 'episode', 'note'];
const items = ref<Record<string, any>[]>([]);
const loading = ref(false);
const error = ref('');
const message = ref('');
const kind = ref<string | null>(null);
const limit = ref(50);

const searchUmo = ref('');
const searchQuery = ref('');
const searchHits = ref<Record<string, any>[]>([]);
const searchOpen = ref(false);

const confirmOpen = ref(false);
const deleting = ref(false);
const pendingDelete = ref<Record<string, any> | null>(null);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const payload = await lifeApi.memory({
      limit: Math.min(Math.max(Number(limit.value) || 50, 1), 200),
      kind: kind.value || undefined,
    });
    items.value = (payload.items as Record<string, any>[]) || [];
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
  }
}

async function search() {
  if (!searchUmo.value || !searchQuery.value) {
    message.value = tm('memory.searchHint');
    return;
  }
  try {
    searchHits.value = await lifeApi.memorySearch({
      q: searchQuery.value,
      umo: searchUmo.value,
      mode: 'hybrid',
      limit: 10,
    });
    searchOpen.value = true;
  } catch (err) {
    error.value = String(err);
  }
}

function askDelete(row: Record<string, any>) {
  pendingDelete.value = row;
  confirmOpen.value = true;
}

async function confirmDelete() {
  const row = pendingDelete.value;
  if (!row) return;
  deleting.value = true;
  try {
    const result = await lifeApi.memoryDelete({ ids: String(row.id) });
    message.value = tm('memory.deleted', { count: result.deleted });
    confirmOpen.value = false;
    await load();
  } catch (err) {
    error.value = String(err);
  } finally {
    deleting.value = false;
  }
}

async function exportAll() {
  try {
    const payload = await lifeApi.memoryExport();
    downloadJson(`life-memory-${Date.now()}.json`, payload);
  } catch (err) {
    error.value = String(err);
  }
}

onMounted(load);
</script>

<style scoped>
.life-toolbar {
  background: rgba(var(--v-theme-surface-variant), 0.25);
}
.life-content {
  max-width: 460px;
  white-space: normal;
  word-break: break-word;
}
</style>
