<template>
  <LifePageShell
    v-model:message="message"
    :title="tm('people.title')"
    :subtitle="tm('people.subtitle')"
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
          <th>{{ tm('people.columns.name') }}</th>
          <th>{{ tm('people.columns.personId') }}</th>
          <th>{{ tm('people.columns.relation') }}</th>
          <th>{{ tm('people.columns.interactions') }}</th>
          <th>{{ tm('people.columns.lastSeen') }}</th>
          <th class="text-right">{{ tm('common.actions') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.person_id)">
          <td>{{ row.display_name || row.person_id }}</td>
          <td class="text-caption">{{ row.person_id }}</td>
          <td>{{ row.relation_score }}</td>
          <td>{{ row.interactions }}</td>
          <td class="text-caption">{{ formatTs(Number(row.last_seen_ts)) }}</td>
          <td class="text-right">
            <v-btn
              size="small"
              variant="text"
              prepend-icon="mdi-account-details"
              @click="openProfile(row)"
            >
              {{ tm('people.profile') }}
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
            {{ tm('people.empty') }}
          </td>
        </tr>
      </tbody>
    </v-table>

    <v-dialog v-model="profileOpen" max-width="720">
      <v-card v-if="profile">
        <v-card-title class="text-h6">
          {{ profile.display_name || profile.person_id }}
        </v-card-title>
        <v-card-subtitle class="text-caption">
          {{ tm('people.relation') }} {{ profile.relation_score }} ·
          {{ tm('people.interactions') }} {{ profile.interactions }}
        </v-card-subtitle>
        <v-card-text>
          <p class="text-body-2 mb-3">{{ profile.summary || tm('people.noSummary') }}</p>
          <div class="d-flex ga-2 mb-2 flex-wrap">
            <v-chip
              v-for="item in profile.highlight"
              :key="item"
              size="small"
              variant="tonal"
            >
              {{ item }}
            </v-chip>
          </div>
          <v-table density="compact">
            <thead>
              <tr>
                <th>{{ tm('people.columns.fact') }}</th>
                <th>{{ tm('people.columns.category') }}</th>
                <th>{{ tm('people.columns.confidence') }}</th>
                <th class="text-right">{{ tm('common.actions') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="fact in profile.facts" :key="fact.id">
                <td>{{ fact.content }}</td>
                <td>{{ fact.category }}</td>
                <td>{{ fact.confidence }}</td>
                <td class="text-right">
                  <v-btn
                    size="small"
                    variant="text"
                    color="error"
                    @click="deleteFact(fact)"
                  >
                    {{ tm('common.delete') }}
                  </v-btn>
                </td>
              </tr>
              <tr v-if="!profile.facts.length">
                <td colspan="4" class="text-center py-3">
                  {{ tm('people.noFacts') }}
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="profileOpen = false">
            {{ tm('common.close') }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <LifeConfirmDialog
      v-model="confirmOpen"
      :title="tm('people.deleteTitle')"
      :text="tm('people.deleteHint')"
      :loading="deleting"
      @confirm="confirmDelete"
    />
  </LifePageShell>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { lifeApi } from '@/api/life';
import { useModuleI18n } from '@/i18n/composables';
import { formatTs } from './format';
import LifeConfirmDialog from './LifeConfirmDialog.vue';
import LifePageShell from './LifePageShell.vue';

const { tm } = useModuleI18n('features/life');

const umo = ref('');
const limit = ref(20);
const rows = ref<Record<string, any>[]>([]);
const loading = ref(false);
const error = ref('');
const message = ref('');

const profileOpen = ref(false);
const profile = ref<Record<string, any> | null>(null);

const confirmOpen = ref(false);
const deleting = ref(false);
const pendingDelete = ref<Record<string, any> | null>(null);
const pendingFactId = ref<number | null>(null);

async function load() {
  if (!umo.value) {
    rows.value = [];
    return;
  }
  loading.value = true;
  error.value = '';
  try {
    rows.value = await lifeApi.people({
      umo: umo.value,
      limit: Math.min(Math.max(Number(limit.value) || 20, 1), 200),
    });
  } catch (err) {
    error.value = String(err);
  } finally {
    loading.value = false;
  }
}

async function openProfile(row: Record<string, any>) {
  try {
    profile.value = await lifeApi.personProfile({
      umo: umo.value,
      person: String(row.person_id),
    });
    profileOpen.value = true;
  } catch (err) {
    error.value = String(err);
  }
}

function askDelete(row: Record<string, any>) {
  pendingDelete.value = row;
  pendingFactId.value = null;
  confirmOpen.value = true;
}

function deleteFact(fact: Record<string, any>) {
  if (!profile.value) return;
  pendingDelete.value = profile.value;
  pendingFactId.value = Number(fact.id);
  confirmOpen.value = true;
}

async function confirmDelete() {
  const row = pendingDelete.value;
  if (!row) return;
  deleting.value = true;
  try {
    const params: { umo: string; person: string; fact_id?: number } = {
      umo: umo.value,
      person: String(row.person_id),
    };
    if (pendingFactId.value !== null) params.fact_id = pendingFactId.value;
    await lifeApi.peopleDelete(params);
    message.value = tm('people.deleted');
    confirmOpen.value = false;
    if (profileOpen.value && pendingFactId.value !== null) {
      profile.value = await lifeApi.personProfile({
        umo: umo.value,
        person: String(row.person_id),
        refresh: true,
      });
    } else {
      profileOpen.value = false;
      await load();
    }
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
</style>
