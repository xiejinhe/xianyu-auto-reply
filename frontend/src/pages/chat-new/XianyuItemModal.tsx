/**
 * 闲鱼商品详情弹窗（聊天页顶部商品条点开）
 *
 * 两个视图：
 * 1. 闲鱼原页（默认）—— iframe 直接加载 goofish.com 商品页。
 *    闲鱼未设置 X-Frame-Options，可以嵌入；但页面用的是**浏览器自己的** Cookie，
 *    浏览器没登录过闲鱼网页版时会显示登录页 —— 这时点「去登录」登录一次即可，
 *    之后长期有效（Cookie 有效期很长）。
 * 2. 商品信息 —— 用系统自己的数据渲染（后端带账号Cookie查到的详情），
 *    不依赖浏览器登录，作为兜底。
 */
import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ExternalLink, ImageIcon, Package, RotateCw, X } from 'lucide-react'
import { cn } from '@/utils/cn'
import { InteractiveFrame } from './InteractiveFrame'

const VIEW_STORAGE_KEY = 'chat_item_detail_view'

export interface XianyuItemModalProps {
  isOpen: boolean
  /** 商品ID */
  itemId: string
  /** 商品标题（会话数据兜底） */
  title: string
  /** 商品主图（会话数据兜底） */
  pic: string
  price?: string
  originalPrice?: string
  transportFee?: string
  status?: string
  desc?: string
  images?: { url: string; major: boolean }[]
  browseCnt?: string
  collectCnt?: string
  quantity?: string
  sellerNick?: string
  onClose: () => void
}

type ViewMode = 'raw' | 'info'

export function XianyuItemModal({
  isOpen,
  itemId,
  title,
  pic,
  price,
  originalPrice,
  transportFee,
  status,
  desc,
  images,
  browseCnt,
  collectCnt,
  quantity,
  sellerNick,
  onClose,
}: XianyuItemModalProps) {
  const [view, setView] = useState<ViewMode>('raw')
  const [activeIdx, setActiveIdx] = useState(0)
  const [reloadKey, setReloadKey] = useState(0)

  // 打开时恢复上次选择的视图（默认闲鱼原页）
  useEffect(() => {
    if (!isOpen) return
    setActiveIdx(0)
    try {
      const saved = localStorage.getItem(VIEW_STORAGE_KEY)
      setView(saved === 'info' ? 'info' : 'raw')
    } catch {
      setView('raw')
    }
  }, [isOpen, itemId])

  // Esc 关闭
  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [isOpen, onClose])

  const switchView = (next: ViewMode) => {
    setView(next)
    try {
      localStorage.setItem(VIEW_STORAGE_KEY, next)
    } catch {
      /* 忽略隐私模式下的写入失败 */
    }
  }

  const itemUrl = `https://www.goofish.com/item?id=${encodeURIComponent(itemId)}`
  const loginUrl = 'https://www.goofish.com/'

  const gallery: string[] =
    images && images.length ? images.map((i) => i.url) : pic ? [pic] : []
  const mainPic = gallery.length ? gallery[Math.min(activeIdx, gallery.length - 1)] : ''
  const showOriginal =
    !!originalPrice && String(originalPrice) !== '0' && String(originalPrice) !== price

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
              'relative w-full max-w-5xl mx-4 flex flex-col h-[85vh]',
              'bg-white dark:bg-slate-800 rounded-2xl shadow-2xl',
              'border border-slate-200 dark:border-slate-700'
            )}
          >
            {/* 头部：标题 + 视图切换 + 操作 */}
            <div className="flex items-start gap-3 p-4 border-b border-slate-200 dark:border-slate-700">
              <div className="flex-1 min-w-0">
                <div className="text-sm font-semibold text-slate-900 dark:text-white truncate">
                  {title || '商品详情'}
                </div>
                <div className="text-xs text-slate-400 mt-0.5">商品ID：{itemId}</div>
              </div>

              <div className="flex rounded-lg bg-slate-100 dark:bg-slate-700 p-0.5 flex-shrink-0">
                <button
                  onClick={() => switchView('raw')}
                  className={cn(
                    'px-3 py-1 text-xs rounded-md transition-colors',
                    view === 'raw'
                      ? 'bg-white dark:bg-slate-600 text-slate-900 dark:text-white shadow-sm'
                      : 'text-slate-500 dark:text-slate-400'
                  )}
                >
                  闲鱼原页
                </button>
                <button
                  onClick={() => switchView('info')}
                  className={cn(
                    'px-3 py-1 text-xs rounded-md transition-colors',
                    view === 'info'
                      ? 'bg-white dark:bg-slate-600 text-slate-900 dark:text-white shadow-sm'
                      : 'text-slate-500 dark:text-slate-400'
                  )}
                >
                  商品信息
                </button>
              </div>

              <button
                onClick={() => window.open(itemUrl, '_blank', 'noopener,noreferrer')}
                title="在新标签打开闲鱼商品页"
                className="flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors flex-shrink-0"
              >
                <ExternalLink className="w-3.5 h-3.5" />
                新标签打开
              </button>
              <button
                onClick={onClose}
                className="p-1 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors flex-shrink-0"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {view === 'raw' ? (
              <>
                {/* 登录提示条 */}
                <div className="flex items-center gap-3 px-4 py-2 text-xs bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200 border-b border-slate-200 dark:border-slate-700">
                  <span className="flex-1">
                    下方是闲鱼原页面。若显示登录页，说明浏览器还没登录过闲鱼网页版 —— 登录一次即可，之后长期有效。
                  </span>
                  <button
                    onClick={() => window.open(loginUrl, '_blank', 'noopener,noreferrer')}
                    className="px-2 py-1 rounded bg-amber-200/70 dark:bg-amber-800/50 hover:bg-amber-200 dark:hover:bg-amber-800 transition-colors flex-shrink-0"
                  >
                    去登录
                  </button>
                  <button
                    onClick={() => setReloadKey((k) => k + 1)}
                    title="登录完成后点这里重新加载"
                    className="flex items-center gap-1 px-2 py-1 rounded bg-amber-200/70 dark:bg-amber-800/50 hover:bg-amber-200 dark:hover:bg-amber-800 transition-colors flex-shrink-0"
                  >
                    <RotateCw className="w-3 h-3" />
                    重新加载
                  </button>
                </div>

                <div className="flex-1 min-h-0">
                  <InteractiveFrame
                    src={itemUrl}
                    title="闲鱼商品详情"
                    frameClass="w-full h-full border-0 rounded-b-2xl bg-white"
                    reloadKey={reloadKey}
                    referrerPolicy="no-referrer"
                  />
                </div>
              </>
            ) : (
              /* 商品信息视图：系统自己查到的数据，不依赖浏览器登录 */
              <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
                {mainPic ? (
                  <div>
                    <div className="w-full aspect-[4/3] bg-slate-100 dark:bg-slate-700 rounded-xl overflow-hidden flex items-center justify-center">
                      <img src={mainPic} alt="" className="max-w-full max-h-full object-contain" />
                    </div>
                    {gallery.length > 1 && (
                      <div className="flex gap-2 mt-2 overflow-x-auto pb-1">
                        {gallery.map((u, i) => (
                          <button
                            key={u + i}
                            onClick={() => setActiveIdx(i)}
                            className={cn(
                              'w-14 h-14 rounded-lg overflow-hidden flex-shrink-0 border-2 transition-colors',
                              i === activeIdx
                                ? 'border-blue-500'
                                : 'border-transparent hover:border-slate-300 dark:hover:border-slate-500'
                            )}
                          >
                            <img src={u} alt="" className="w-full h-full object-cover" />
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="w-full aspect-[4/3] rounded-xl bg-slate-100 dark:bg-slate-700 flex items-center justify-center">
                    <Package className="w-10 h-10 text-slate-400" />
                  </div>
                )}

                <div className="flex items-center flex-wrap gap-2">
                  {price ? (
                    <span className="text-2xl font-semibold text-red-500">¥{price}</span>
                  ) : (
                    <span className="text-sm text-slate-400">价格未获取到（接口风控或商品已下架）</span>
                  )}
                  {showOriginal ? (
                    <span className="text-sm text-slate-400 line-through">¥{originalPrice}</span>
                  ) : null}
                  {transportFee ? (
                    <span className="text-xs text-slate-500 dark:text-slate-400">
                      运费 ¥{transportFee}
                    </span>
                  ) : null}
                  {status ? (
                    <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-50 dark:bg-emerald-900/20 text-emerald-600 dark:text-emerald-400">
                      {status}
                    </span>
                  ) : null}
                </div>

                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
                  {browseCnt ? <span>浏览 {browseCnt}</span> : null}
                  {collectCnt ? <span>想要 {collectCnt}</span> : null}
                  {quantity ? <span>库存 {quantity}</span> : null}
                  {sellerNick ? <span>卖家 {sellerNick}</span> : null}
                </div>

                {desc ? (
                  <div>
                    <div className="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">
                      商品描述
                    </div>
                    <div className="text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap leading-relaxed">
                      {desc}
                    </div>
                  </div>
                ) : null}

                {gallery.length === 0 && (
                  <div className="flex items-center gap-2 text-xs text-slate-400">
                    <ImageIcon className="w-3.5 h-3.5" />
                    暂无商品图片
                  </div>
                )}
              </div>
            )}
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
