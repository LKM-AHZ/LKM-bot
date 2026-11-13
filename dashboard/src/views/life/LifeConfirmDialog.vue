<template>
  <v-dialog
    :model-value="modelValue"
    max-width="420"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <v-card>
      <v-card-title class="text-h6">{{ title }}</v-card-title>
      <v-card-text class="text-body-2">{{ text }}</v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="emit('update:modelValue', false)">
          {{ tm('common.cancel') }}
        </v-btn>
        <v-btn
          variant="tonal"
          color="error"
          :loading="loading"
          @click="emit('confirm')"
        >
          {{ confirmText || tm('common.delete') }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup lang="ts">
import { useModuleI18n } from '@/i18n/composables';

const { tm } = useModuleI18n('features/life');

defineProps<{
  modelValue: boolean;
  title: string;
  text: string;
  loading?: boolean;
  confirmText?: string;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: boolean];
  confirm: [];
}>();
</script>
