import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './styles.css'

const router = createRouter({
  history: createWebHashHistory('/debug/'),
  routes: [
    { path: '/', redirect: '/tasks' },
    { path: '/tasks', component: () => import('./views/TasksPage.vue') },
    { path: '/tasks/:recordId', component: () => import('./views/TaskDetailPage.vue') },
    { path: '/config', component: () => import('./views/ConfigPage.vue') },
    { path: '/trigger', component: () => import('./views/TriggerPage.vue') },
  ],
})

createApp(App).use(router).mount('#app')
