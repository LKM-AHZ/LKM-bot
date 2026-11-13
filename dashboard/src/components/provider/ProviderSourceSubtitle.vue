<template>
  <div class="source-subtitle">
    <span>{{ apiBase || 'N/A' }}</span>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  apiBase?: string
}>()
</script>

<style scoped>
.source-subtitle {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 14px;
}

.source-subtitle > span {
  min-width: 0;
  overflow-wrap: anywhere;
}
</style>
