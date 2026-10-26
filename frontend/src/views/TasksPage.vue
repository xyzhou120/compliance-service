<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import type { TaskListResponse } from '../types'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
const data=ref<TaskListResponse|null>(null),loading=ref(true),error=ref(''),mine=ref(''),cycle=ref('')
let controller=new AbortController(),timer:ReturnType<typeof setTimeout>|undefined,requestId=0
async function load(offset=0,background=false){clearTimeout(timer);controller.abort();controller=new AbortController();const id=++requestId;if(!background)loading.value=true;error.value='';try{const result=await api.tasks(offset,20,mine.value.trim(),cycle.value.trim(),controller.signal);if(id===requestId)data.value=result}catch(e){if(!controller.signal.aborted&&id===requestId)error.value=e instanceof Error?e.message:'任务列表加载失败'}finally{if(id===requestId){loading.value=false;if(data.value?.records.some(r=>['queued','running'].includes(r.execution_state)))timer=setTimeout(()=>{if(!document.hidden)load(data.value?.offset||0,true)},10000)}}}
onMounted(()=>load());onBeforeUnmount(()=>{controller.abort();clearTimeout(timer);requestId++})
function formatTime(epoch:number){return new Date(epoch*1000).toLocaleString('zh-CN',{hour12:false})}
</script>
<template>
  <section class="page-stack">
    <div class="page-intro"><div><h2>任务执行记录</h2><p class="muted">按任务查看每个检查项的执行进度与结果。选择任务后再加载证据详情。</p></div><RouterLink to="/trigger" class="button primary">手动触发</RouterLink></div>
    <div class="filter-bar"><input v-model="mine" aria-label="矿井编码筛选" placeholder="矿井编码" @keyup.enter="load()" /><input v-model="cycle" aria-label="检查周期筛选" placeholder="检查周期，例如 2026-10" @keyup.enter="load()" /><button class="button secondary" @click="load()">查询</button></div>
    <p v-if="error" class="error-banner">{{ error }}</p><div v-if="loading" class="loading-line">正在加载任务摘要…</div>
    <EmptyState v-else-if="!data?.records.length" title="暂无执行记录" detail="通过手动触发页面提交一条 JSON 任务后，记录会出现在这里。" />
    <div v-else class="task-table-wrap"><table class="task-table"><thead><tr><th>触发时间</th><th>矿井 / 周期</th><th>检查项</th><th>进度</th><th>结果</th><th>执行耗时</th><th></th></tr></thead><tbody><tr v-for="task in data.records" :key="task.record_id"><td class="mono">{{ formatTime(task.created) }}</td><td><strong>{{ task.mine_code }}</strong><span class="cell-sub">{{ task.check_cycle }}</span></td><td><span v-for="item in task.items" :key="item.run_id" class="item-chip"><strong>{{ item.item_name || item.item_code }}</strong><small class="cell-sub">{{ item.item_code }}</small></span></td><td><div class="progress-label">{{ task.completed_count }}/{{ task.item_count }} 项</div><div class="progress-track"><i :style="{ width: `${task.item_count ? task.completed_count / task.item_count * 100 : 0}%` }" /></div></td><td><div v-for="item in task.items" :key="item.run_id" class="table-result"><StatusBadge :status="item.result_status || item.execution_state" /></div></td><td><span v-for="item in task.items" :key="item.run_id" class="cell-sub">{{ item.elapsed_ms===null || item.elapsed_ms===undefined?'未开始':`${(item.elapsed_ms/1000).toFixed(1)} 秒` }}</span></td><td><RouterLink class="row-link" :to="`/tasks/${encodeURIComponent(task.record_id)}`">查看详情 →</RouterLink></td></tr></tbody></table></div>
    <div v-if="data && (data.offset > 0 || data.has_more)" class="pagination"><button class="button secondary" :disabled="data.offset === 0" @click="load(Math.max(0, data.offset - data.limit))">上一页</button><span>{{ data.offset + 1 }}–{{ Math.min(data.total, data.offset + data.limit) }} / {{ data.total }}</span><button class="button secondary" :disabled="!data.has_more" @click="load(data.offset + data.limit)">下一页</button></div>
  </section>
</template>
