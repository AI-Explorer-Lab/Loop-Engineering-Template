import { createRouter, createWebHashHistory, type RouteRecordRaw } from "vue-router";

import ChangesView from "../views/ChangesView.vue";
import CreateView from "../views/CreateView.vue";
import HistoryView from "../views/HistoryView.vue";
import MonitorView from "../views/MonitorView.vue";
import ProjectsView from "../views/ProjectsView.vue";
import ReviewView from "../views/ReviewView.vue";
import SettingsView from "../views/SettingsView.vue";

export const routes: RouteRecordRaw[] = [
    { path: "/", redirect: "/create" },
    { path: "/create", name: "create", component: CreateView, meta: { title: "创建" } },
    { path: "/monitor", name: "monitor", component: MonitorView, meta: { title: "监控" } },
    { path: "/changes", name: "changes", component: ChangesView, meta: { title: "变更" } },
    { path: "/review", name: "review", component: ReviewView, meta: { title: "审核" } },
    { path: "/history", name: "history", component: HistoryView, meta: { title: "历史" } },
    { path: "/projects", name: "projects", component: ProjectsView, meta: { title: "项目" } },
    { path: "/settings", name: "settings", component: SettingsView, meta: { title: "设置" } },
    { path: "/:pathMatch(.*)*", redirect: "/create" },
];

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
});
