<script setup lang="ts">
defineProps<{ title: string; detail?: string }>()
</script>
<template><div class="empty-state"><strong>{{ title }}</strong><p>{{ detail }}</p></div></template>
