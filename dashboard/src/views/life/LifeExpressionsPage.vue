<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('expressions.title')"
    :subtitle="tm('expressions.subtitle')"
    :loading="loading"
    :error="error"
    @refresh="load"
  >
    <v-card variant="flat" class="life-toolbar pa-3 mb-4">
      <div class="d-flex flex-wrap ga-3 align-center">
        <v-text-field
          v-model="umo"
          :label="tm('common.umo')"
          density="compact"
          variant="outlined"
          hide-details
          style="min-width: 280px"
          @keyup.enter="load"
        />
        <v-text-field
          v-model.number="limit"
          :label="tm('people.limit')"
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

    <v-table density="compact" class="life-table">
      <thead>
        <tr>
          <th>{{ tm('expressions.columns.scene') }}</th>
          <th>{{ tm('expressions.columns.situation') }}</th>
          <th>{{ tm('expressions.columns.style') }}</th>
          <th>{{ tm('expressions.columns.weight') }}</th>
          <th>{{ tm('expressions.columns.usage') }}</th>
          <th>{{ tm('expressions.columns.source') }}</th>
          <th class="text-right">{{ tm('common.actions') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row.scene }}</td>
          <td class="life-content">{{ truncate(row.situation, 80) }}</td>
          <td>{{ row.style }}</td>
          <td>{{ row.weight }}</td>
          <td>{{ row.usage_count }}</td>
          <td>{{ row.source }}</td>
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
        <tr v-if="!rows.length">
          <td colspan="7" class="text-center text-medium-emphasis py-6">
            {{ tm('expressions.empty') }}
          </td>
        </tr>
      </tbody>
    </v-table>

    <LifeConfirmDialog
      v-model="confirmOpen"
      :title="tm('expressions.deleteTitle')"
      :text="tm('expressions.deleteHint')"
      :loading="deleting"
      @confirm="confirmDelete"
    />
  </LifePageShell>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { lifeApi } from '@/api/life';
import { useModuleI18n } from '@/i18n/composables';
import { truncate } from './format';
import LifeConfirmDialog from './LifeConfirmDialog.vue';
import LifePageShell from './LifePageShell.vue';

const { tm } = useModuleI18n('features/life');

const umo = ref('');
const limit = ref(50);
const rows = ref<Record<string, any>[]>([]);
const loading = ref(false);
const error = ref('');
const message = ref('');
const confirmOpen = ref(false);
const deleting = ref(false);
const pendingDelete = ref<Record<string, any> | null>(null);

async function load() {
  if (!umo.value) {
    rows.value = [];
    return;
  }
  loading.value = true;
  error.value = '';
  try {
    rows.value = await lifeApi.expressions({
      umo: umo.value,
      limit: Math.min(Math.max(Number(limit.value) || 50, 1), 200),
    });
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
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
    const result = await lifeApi.expressionDelete({ ids: String(row.id) });
    message.value = tm('expressions.deleted', { count: result.deleted });
    confirmOpen.value = false;
    await load();
  } catch (err) {
    error.value = String(err);
  } finally {
    deleting.value = false;
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
