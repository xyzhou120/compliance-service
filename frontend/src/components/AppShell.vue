<script setup lang="ts">
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { computed } from 'vue'

const route = useRoute()
const links = [
  { to: '/tasks', label: '任务记录', caption: '执行、进度与结果' },
  { to: '/config', label: '检查项配置', caption: '清单与周期策略' },
  { to: '/trigger', label: '手动触发', caption: 'JSON 调试入口' },
]
const activeLabel = computed(() => links.find((link) => route.path.startsWith(link.to))?.label || '任务记录')
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">检</span><div><strong>合规检查</strong><small>本地调试工作台</small></div></div>
      <nav aria-label="主导航">
        <RouterLink v-for="link in links" :key="link.to" :to="link.to" class="nav-link" :class="{ active: route.path.startsWith(link.to) }">
          <span>{{ link.label }}</span><small>{{ link.caption }}</small>
        </RouterLink>
      </nav>
      <div class="sidebar-foot"><span class="status-dot" /> 仅在本地调试<br><small>结果不发送回调</small><br><a href="/debug/legacy">旧版附件调试</a></div>
    </aside>
    <main class="main-content">
      <header class="topbar"><div><h1>{{ activeLabel }}</h1></div><span class="environment-badge">本地调试 · 回调关闭</span></header>
      <RouterView />
    </main>
  </div>
</template>
