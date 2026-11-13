<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('emojis.title')"
    :subtitle="tm('emojis.subtitle')"
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
          :label="tm('emojis.columns.status')"
          density="compact"
          variant="outlined"
          hide-details
          style="max-width: 160px"
          @update:model-value="load"
        />
      </div>
    </v-card>

    <v-table density="compact" class="life-table">
      <thead>
        <tr>
          <th>{{ tm('emojis.columns.id') }}</th>
          <th>{{ tm('emojis.columns.tags') }}</th>
          <th>{{ tm('emojis.columns.caption') }}</th>
          <th>{{ tm('emojis.columns.usage') }}</th>
          <th>{{ tm('emojis.columns.status') }}</th>
          <th class="text-right">{{ tm('common.actions') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row.id }}</td>
          <td>
            <v-chip
              v-for="tag in row.tags"
              :key="tag"
              size="x-small"
              variant="tonal"
              class="mr-1"
            >
              {{ tag }}
            </v-chip>
          </td>
          <td class="life-content">{{ truncate(row.caption, 90) }}</td>
          <td>{{ row.usage_count }}</td>
          <td>{{ row.status }}</td>
          <td class="text-right">
            <v-btn
              size="small"
              variant="text"
              :color="row.status === 'active' ? 'warning' : 'success'"
              @click="toggle(row)"
            >
              {{ row.status === 'active' ? tm('emojis.disable') : tm('emojis.enable') }}
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
            {{ tm('emojis.empty') }}
          </td>
        </tr>
      </tbody>
    </v-table>

    <LifeConfirmDialog
      v-model="confirmOpen"
      :title="tm('emojis.deleteTitle')"
      :text="tm('emojis.deleteHint')"
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
const status = ref('active');
const statuses = ['active', 'rejected'];
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
    rows.value = await lifeApi.emojis({
      umo: umo.value,
      status: status.value,
      limit: 200,
    });
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
  }
}

async function toggle(row: Record<string, any>) {
  try {
    await lifeApi.emojiUpdate({
      id: Number(row.id),
      status: row.status === 'active' ? 'rejected' : 'active',
    });
    message.value = tm('emojis.updated');
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
    const result = await lifeApi.emojiDelete({ ids: String(row.id) });
    message.value = tm('emojis.deleted', { count: result.deleted });
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
  max-width: 360px;
  white-space: normal;
  word-break: break-word;
}
</style>
