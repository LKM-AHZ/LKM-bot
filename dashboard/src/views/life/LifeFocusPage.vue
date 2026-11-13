<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('focus.title')"
    :subtitle="tm('focus.subtitle')"
    :loading="loading"
    :error="error"
    @refresh="load"
  >
    <v-table density="compact" class="life-table">
      <thead>
        <tr>
          <th>{{ tm('common.umo') }}</th>
          <th>{{ tm('focus.columns.compressed') }}</th>
          <th>{{ tm('focus.columns.messages') }}</th>
          <th>{{ tm('focus.columns.lastTurn') }}</th>
          <th>{{ tm('focus.columns.updated') }}</th>
          <th class="text-right">{{ tm('common.actions') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in sessions" :key="String(row.session_key)">
          <td class="text-caption">{{ row.session_key }}</td>
          <td>{{ row.compressed_turns }}</td>
          <td>{{ row.total_messages }}</td>
          <td>{{ row.last_turn_index }}</td>
          <td class="text-caption">{{ formatTs(Number(row.updated_at)) }}</td>
          <td class="text-right">
            <v-btn
              size="small"
              variant="text"
              prepend-icon="mdi-text-box-search-outline"
              @click="openDetail(row)"
            >
              {{ tm('focus.detail') }}
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
        <tr v-if="!sessions.length">
          <td colspan="6" class="text-center text-medium-emphasis py-6">
            {{ tm('focus.empty') }}
          </td>
        </tr>
      </tbody>
    </v-table>

    <v-dialog v-model="detailOpen" max-width="820">
      <v-card v-if="detail">
        <v-card-title class="text-h6">{{ String(detail.umo || '') }}</v-card-title>
        <v-card-text>
          <p class="text-body-2 mb-2 font-weight-medium">
            {{ tm('focus.digest') }}
          </p>
          <pre class="life-digest">{{ detail.digest || tm('focus.noDigest') }}</pre>
          <p class="text-body-2 mt-4 mb-2 font-weight-medium">
            {{ tm('focus.turns') }}
          </p>
          <v-table density="compact">
            <thead>
              <tr>
                <th>#</th>
                <th>{{ tm('focus.columns.summary') }}</th>
                <th>{{ tm('focus.columns.updated') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="turn in detail.turns" :key="String(turn.index)">
                <td>{{ turn.index }}</td>
                <td class="life-content">{{ truncate(turn.summary, 200) }}</td>
                <td class="text-caption">{{ formatTs(Number(turn.ended_ts)) }}</td>
              </tr>
              <tr v-if="!detail.turns.length">
                <td colspan="3" class="text-center py-3">
                  {{ tm('focus.noTurns') }}
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="detailOpen = false">
            {{ tm('common.close') }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <LifeConfirmDialog
      v-model="confirmOpen"
      :title="tm('focus.deleteTitle')"
      :text="tm('focus.deleteHint')"
      :loading="deleting"
      @confirm="confirmDelete"
    />
  </LifePageShell>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { lifeApi } from '@/api/life';
import { useModuleI18n } from '@/i18n/composables';
import { formatTs, truncate } from './format';
import LifeConfirmDialog from './LifeConfirmDialog.vue';
import LifePageShell from './LifePageShell.vue';

const { tm } = useModuleI18n('features/life');

const sessions = ref<Record<string, any>[]>([]);
const loading = ref(false);
const error = ref('');
const message = ref('');
const detailOpen = ref(false);
const detail = ref<Record<string, any> | null>(null);
const confirmOpen = ref(false);
const deleting = ref(false);
const pendingDelete = ref<Record<string, any> | null>(null);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const payload = await lifeApi.focus();
    sessions.value = Array.isArray(payload) ? payload : [];
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
  }
}

async function openDetail(row: Record<string, any>) {
  try {
    const payload = await lifeApi.focus(String(row.session_key));
    detail.value = { ...payload, umo: row.session_key };
    detailOpen.value = true;
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
    const result = await lifeApi.focusDelete(String(row.session_key));
    message.value = tm('focus.deleted', { count: result.deleted });
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
.life-digest {
  max-height: 240px;
  overflow: auto;
  padding: 12px;
  border-radius: 8px;
  background: rgba(var(--v-theme-surface-variant), 0.35);
  white-space: pre-wrap;
  word-break: break-word;
}
.life-content {
  max-width: 420px;
  white-space: normal;
  word-break: break-word;
}
</style>
