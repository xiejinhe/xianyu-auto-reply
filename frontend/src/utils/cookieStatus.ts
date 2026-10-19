/**
 * 账号网页 Cookie 会话状态（cookie_status）的展示元信息。
 *
 * 后端基于现有字段推断，不做任何数据库迁移：
 * - normal        : 正常
 * - risk_control  : 风控中（近 24h 内有滑块挑战记录）
 * - expired       : 网页 Cookie 会话失效，需要重新登录
 * - disabled      : 账号被禁用（业务开关）
 */

export type CookieStatus = 'normal' | 'risk_control' | 'expired' | 'disabled'

export interface CookieStatusMeta {
  label: string
  /** 与项目其它 badge 一致的配色（同时提供 light/dark 变体） */
  badgeClass: string
  desc: string
}

export const COOKIE_STATUS_META: Record<CookieStatus, CookieStatusMeta> = {
  normal: {
    label: '正常',
    badgeClass:
      'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
    desc: '网页 Cookie 会话正常',
  },
  risk_control: {
    label: '风控中',
    badgeClass: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
    desc: '近 24 小时触发了闲鱼风控（滑块挑战），网页会话可能受限，建议稍后重试或重新登录',
  },
  expired: {
    label: '失效需重登',
    badgeClass:
      'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
    desc: '网页 Cookie 会话已失效或长时间未续期，需要重新登录该账号',
  },
  disabled: {
    label: '已禁用',
    badgeClass:
      'bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-400',
    desc: '账号已被禁用（业务开关）',
  },
}

/** 把任意后端 cookie_status 值安全地映射到展示元信息，未知值按 normal 处理。 */
export function getCookieStatusMeta(
  status?: string | null,
): CookieStatusMeta {
  if (status && status in COOKIE_STATUS_META) {
    return COOKIE_STATUS_META[status as CookieStatus]
  }
  return COOKIE_STATUS_META.normal
}

/** 把 ISO 时间格式化为「x天前 / x小时前 / x分钟前」，用于展示“最后刷新/登录”。 */
export function formatRelativeTime(iso?: string | null): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return '—'
  const diffMs = Date.now() - t
  const min = Math.floor(diffMs / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min}分钟前`
  const hours = Math.floor(min / 60)
  if (hours < 24) return `${hours}小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}天前`
  const months = Math.floor(days / 30)
  return `${months}个月前`
}
