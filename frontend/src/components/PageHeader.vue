<script setup lang="ts">
defineProps<{ title: string; description?: string; eyebrow?: string }>()
</script>

<template>
  <header class="pic-page-header">
    <div class="pic-page-header__copy">
      <div v-if="eyebrow" class="pic-eyebrow">{{ eyebrow }}</div>
      <h1>{{ title }}</h1>
      <p v-if="description">{{ description }}</p>
    </div>
    <div v-if="$slots.default" class="pic-page-header__actions"><slot /></div>
  </header>
</template>
