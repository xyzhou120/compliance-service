<script setup lang="ts">
defineProps<{ status?: string | null }>()
const labels: Record<string, string> = { pass: '通过', no_pass: '不通过', pending: '待补证', unverifiable: '无法核验', failed: '失败', completed: '已结束', running: '执行中', queued: '排队中', paused: '已暂停' }
</script>
<template><span class="status-badge" :data-status="status || 'unknown'">{{ labels[status || ''] || status || '未开始' }}</span></template>
