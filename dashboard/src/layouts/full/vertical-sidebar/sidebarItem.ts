import { markRaw, type Component } from 'vue';
import {
  Activity,
  ArrowRightLeft,
  BookOpenText,
  BookSearch,
  Bot,
  Box,
  Brain,
  Clock3,
  Database,
  Gauge,
  Hand,
  Heart,
  Image,
  MessageSquareQuote,
  PencilRuler,
  Puzzle,
  Radar,
  SlidersHorizontal,
  Users,
} from '@lucide/vue';

export interface menu {
  header?: string;
  value?: string;
  title?: string;
  icon?: string | Component;
  to?: string;
  divider?: boolean;
  chip?: string;
  chipColor?: string;
  chipVariant?: string;
  chipIcon?: string;
  children?: menu[];
  disabled?: boolean;
  type?: string;
  subCaption?: string;
  isRawTitle?: boolean;
  collapsible?: boolean;
  pluginInfo?: {
    id: string;
    displayName?: string;
    author?: string | null;
    version?: string;
  };
  groupToggle?: boolean;
}

export const SYSTEM_GROUP_KEY = 'core.navigation.groups.system';
export const EXTENSION_GROUP_KEY = 'core.navigation.groups.extension';

// 注意：这个文件现在包含i18n键值而不是直接的文本
// 在组件中使用时需要通过t()函数进行翻译
// 所有键名都使用 core.navigation.* 格式
const sidebarItem: menu[] = [
  {
    header: SYSTEM_GROUP_KEY,
  },
  {
    title: 'core.navigation.welcome',
    icon: markRaw(Hand),
    to: '/welcome',
  },
  {
    title: 'core.navigation.platforms',
    icon: markRaw(Bot),
    to: '/platforms',
  },
  {
    title: 'core.navigation.providers',
    icon: markRaw(Box),
    to: '/providers',
  },
  {
    title: 'core.navigation.extension',
    icon: markRaw(Puzzle),
    to: '/extension',
  },
  {
    title: 'core.navigation.config',
    icon: markRaw(SlidersHorizontal),
    to: '/config',
  },
  {
    title: 'core.navigation.data',
    icon: markRaw(Database),
    to: '/data'
  },
  {
    title: 'core.navigation.sessionManagement',
    icon: markRaw(PencilRuler),
    to: '/session-management'
  },
  {
    header: 'core.navigation.groups.life',
  },
  {
    title: 'core.navigation.life.memory',
    icon: markRaw(Brain),
    to: '/life/memory',
  },
  {
    title: 'core.navigation.life.people',
    icon: markRaw(Users),
    to: '/life/people',
  },
  {
    title: 'core.navigation.life.expressions',
    icon: markRaw(MessageSquareQuote),
    to: '/life/expressions',
  },
  {
    title: 'core.navigation.life.jargon',
    icon: markRaw(BookOpenText),
    to: '/life/jargon',
  },
  {
    title: 'core.navigation.life.emojis',
    icon: markRaw(Image),
    to: '/life/emojis',
  },
  {
    title: 'core.navigation.life.focus',
    icon: markRaw(Activity),
    to: '/life/focus',
  },
  {
    title: 'core.navigation.life.monitor',
    icon: markRaw(Radar),
    to: '/life/monitor',
  },
  {
    title: 'core.navigation.life.rollout',
    icon: markRaw(Gauge),
    to: '/life/rollout',
  },
  {
    title: 'core.navigation.life.migration',
    icon: markRaw(ArrowRightLeft),
    to: '/life/migration',
  },
  {
    header: EXTENSION_GROUP_KEY,
    collapsible: true,
    groupToggle: true,
  },
  {
    title: 'core.navigation.persona',
    icon: markRaw(Heart),
    to: '/persona'
  },
  {
    title: 'core.navigation.knowledgeBase',
    icon: markRaw(BookSearch),
    to: '/knowledge-base',
  },
  {
    title: 'core.navigation.cron',
    icon: markRaw(Clock3),
    to: '/cron'
  },
];

export default sidebarItem;
