import { createRouter, createWebHashHistory } from 'vue-router';
import MainRoutes from './MainRoutes';
import AuthRoutes from './AuthRoutes';
import ChatBoxRoutes from './ChatBoxRoutes';
import { useAuthStore } from '@/stores/auth';
import { useRouterLoadingStore } from '@/stores/routerLoading';

export const router = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes: [
    MainRoutes,
    AuthRoutes,
    ChatBoxRoutes
  ]
});

interface AuthStore {
  username: string;
  returnUrl: string | null;
  login(
    username: string,
    password: string,
    code?: string,
    trustDeviceToken?: boolean,
  ): Promise<void | 'totp_required' | 'upgrade_recovery_required'>;
  logout(): void;
  has_token(): boolean;
  hydrateFromCookie(): Promise<boolean>;
}

router.beforeEach(async (to, from, next) => {
  if (from.name && from.path !== to.path) {
    const loadingStore = useRouterLoadingStore();
    loadingStore.start();
  }

  const publicPages = ['/auth/login', '/auth/setup'];
  const authRequired = !publicPages.includes(to.path);
  const auth: AuthStore = useAuthStore();

  // 如果用户已登录且试图访问登录页面，则重定向到首页
  if (to.path === '/auth/login' && auth.has_token()) {
    return next('/welcome');
  }

  if (to.matched.some((record) => record.meta.requiresAuth)) {
    if (authRequired && !auth.has_token()) {
      // 内嵌面板（社区后台 iframe）走 SSO：会话 cookie 已由 /api/v1/auth/sso 写入，
      // 这里把它兑换成 token；失败才按未登录处理（普通访问路径不受影响——无 cookie 即返回 false）。
      if (await auth.hydrateFromCookie()) {
        return next();
      }
      auth.returnUrl = to.fullPath;
      return next('/auth/login');
    }
    return next();
  } else {
    next();
  }
});

router.afterEach(() => {
  const loadingStore = useRouterLoadingStore();
  loadingStore.finish();
});
