<template>
  <div class="life-page pa-4 pa-md-6">
    <div class="d-flex flex-wrap align-start justify-space-between ga-4 mb-4">
      <div>
        <h1 class="text-h5 font-weight-medium">{{ title }}</h1>
        <p v-if="subtitle" class="text-body-2 text-medium-emphasis mt-1 mb-0">
          {{ subtitle }}
        </p>
      </div>
      <div class="d-flex align-center ga-2">
        <slot name="actions" />
        <v-btn
          variant="text"
          prepend-icon="mdi-refresh"
          :loading="loading"
          @click="emit('refresh')"
        >
          {{ tm('common.refresh') }}
        </v-btn>
      </div>
    </div>

    <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-4">
      {{ error }}
    </v-alert>

    <slot />

    <v-snackbar
      :model-value="!!message"
      color="primary"
      @update:model-value="clearMessage"
    >
      {{ message }}
      <template #actions>
        <v-btn variant="text" @click="clearMessage">{{ tm('common.close') }}</v-btn>
      </template>
    </v-snackbar>
  </div>
</template>

<script setup lang="ts">
import { useModuleI18n } from '@/i18n/composables';

const { tm } = useModuleI18n('features/life');

defineProps<{
  title: string;
  subtitle?: string;
  loading?: boolean;
  error?: string;
  message?: string;
}>();

const emit = defineEmits<{
  refresh: [];
  'update:message': [value: string];
}>();

function clearMessage() {
  emit('update:message', '');
}
</script>
