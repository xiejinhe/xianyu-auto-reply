/**
 * 闲鱼个人主页弹窗
 *
 * 上半部分用系统内已有的真实数据（后端带账号Cookie查到的头像/昵称/ID），
 * 下半部分用 iframe 加载闲鱼网页本身。
 *
 * 注意：iframe 里的登录状态取决于用户浏览器自身是否登录过闲鱼网页版，
 * 系统的账号Cookie在数据库里，无法注入到浏览器对 goofish.com 的请求中。
 */
import { useEffect } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ExternalLink, User, X } from 'lucide-react'
import { cn } from '@/utils/cn'
import { InteractiveFrame } from './InteractiveFrame'

export interface XianyuProfileModalProps {
  isOpen: boolean
  /** 闲鱼用户ID */
  userId: string
  /** 昵称 */
  name: string
  /** 头像URL */
  avatar: string
  onClose: () => void
}

export function XianyuProfileModal({
  isOpen,
  userId,
  name,
  avatar,
  onClose,
}: XianyuProfileModalProps) {
  // Esc 关闭
  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [isOpen, onClose])

  const profileUrl = `https://www.goofish.com/personal?userId=${encodeURIComponent(userId)}`

  return (
    <AnimatePresence>
      {isOpen && (
        <div className="fixed inset-0 z-[1000] flex items-center justify-center">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
          />

          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 20 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: 20 }}
            className={cn(
              'relative w-full max-w-6xl mx-4 flex flex-col h-[80vh]',
              'bg-white dark:bg-slate-800 rounded-2xl shadow-2xl',
              'border border-slate-200 dark:border-slate-700'
            )}
          >
            {/* 头部：系统内查到的真实用户信息 */}
            <div className="flex items-center gap-3 p-4 border-b border-slate-200 dark:border-slate-700">
              {avatar ? (
                <img src={avatar} className="w-11 h-11 rounded-full object-cover" alt="" />
              ) : (
                <div className="w-11 h-11 rounded-full bg-slate-200 dark:bg-slate-600 flex items-center justify-center">
                  <User className="w-5 h-5 text-slate-400" />
                </div>
              )}
              <div className="flex-1 min-w-0">
                <div className="text-sm font-semibold text-slate-900 dark:text-white truncate">
                  {name || '未知用户'}
                </div>
                <div className="text-xs text-slate-400 truncate">闲鱼用户ID：{userId}</div>
              </div>
              <button
                onClick={() => window.open(profileUrl, '_blank', 'noopener,noreferrer')}
                title="在浏览器中打开闲鱼主页"
                className="flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors"
              >
                <ExternalLink className="w-3.5 h-3.5" />
                在闲鱼打开
              </button>
              <button
                onClick={onClose}
                className="p-1 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* 登录态说明 */}
            <div className="px-4 py-2 text-xs text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/20 border-b border-slate-200 dark:border-slate-700">
              下方为闲鱼网页，登录状态取决于你的浏览器是否已登录闲鱼；未登录会显示登录页，可点右上角「在闲鱼打开」。
              按 Esc 关闭弹窗；点进页面操作后 Esc 暂时失效，点一下顶部即可恢复。
            </div>

            {/* 闲鱼页面：默认拦截交互保证 Esc 可用，点击遮罩后才放行 */}
            <div className="flex-1 min-h-0">
              <InteractiveFrame
                src={profileUrl}
                title="闲鱼个人主页"
                frameClass="w-full h-full border-0 rounded-b-2xl bg-white"
                referrerPolicy="no-referrer"
              />
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
