<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('jargon.title')"
    :subtitle="tm('jargon.subtitle')"
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
        <v-select
          v-model="status"
          :items="statuses"
          :label="tm('jargon.columns.status')"
          density="compact"
          variant="outlined"
          hide-details
          clearable
          style="max-width: 180px"
          @update:model-value="load"
        />
      </div>
    </v-card>

    <v-table density="compact" class="life-table">
      <thead>
        <tr>
          <th>{{ tm('jargon.columns.term') }}</th>
          <th>{{ tm('jargon.columns.meaning') }}</th>
          <th>{{ tm('jargon.columns.confidence') }}</th>
          <th>{{ tm('jargon.columns.status') }}</th>
          <th>{{ tm('jargon.columns.usage') }}</th>
          <th class="text-right">{{ tm('common.actions') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td class="font-weight-medium">{{ row.term }}</td>
          <td class="life-content">{{ truncate(row.meaning, 120) }}</td>
          <td>{{ row.confidence }}</td>
          <td>
            <v-chip
              size="x-small"
              :color="row.status === 'active' ? 'success' : row.status === 'rejected' ? 'error' : 'warning'"
              variant="tonal"
            >
              {{ row.status }}
            </v-chip>
          </td>
          <td>{{ row.usage_count }}</td>
          <td class="text-right">
            <v-btn size="small" variant="text" color="success" @click="review(row, 'active')">
              {{ tm('jargon.approve') }}
            </v-btn>
            <v-btn size="small" variant="text" color="error" @click="review(row, 'rejected')">
              {{ tm('jargon.reject') }}
            </v-btn>
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
          <td colspan="6" class="text-center text-medium-emphasis py-6">
            {{ tm('jargon.empty') }}
          </td>
        </tr>
      </tbody>
    </v-table>

    <LifeConfirmDialog
      v-model="confirmOpen"
      :title="tm('jargon.deleteTitle')"
      :text="tm('jargon.deleteHint')"
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
const status = ref<string | null>(null);
const statuses = ['active', 'pending', 'rejected'];
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
    rows.value = await lifeApi.jargon({
      umo: umo.value,
      status: status.value || undefined,
      limit: 200,
    });
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
  }
}

async function review(row: Record<string, any>, next: string) {
  try {
    await lifeApi.jargonReview({ id: Number(row.id), status: next });
    message.value = tm('jargon.updated');
    await load();
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
    const result = await lifeApi.jargonDelete({ ids: String(row.id) });
    message.value = tm('jargon.deleted', { count: result.deleted });
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
  max-width: 420px;
  white-space: normal;
  word-break: break-word;
}
</style>
