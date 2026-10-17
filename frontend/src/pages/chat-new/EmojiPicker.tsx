/**
 * 聊天输入框的表情选择面板
 *
 * 参考 闲管家 的弹层样式：按钮 + 上方弹出面板，分「最近使用」与
 * 「全部表情」两区。点选后写入输入框光标处；最近使用记录存
 * localStorage（每端各自记忆，最多 15 个）。
 *
 * 用 Unicode 表情而不是闲鱼的 [中文名] 代码：代码集合没有官方公开清单，
 * 猜错会原样发出 "[xxx]" 文本给买家；Unicode 表情在闲鱼 App、网页和
 * 本系统里都能正常渲染，零映射零风险。
 */
import { useEffect, useRef, useState } from 'react'
import { Smile } from 'lucide-react'
import { cn } from '@/utils/cn'

const RECENT_KEY = 'chat-new:recent-emojis'
const RECENT_MAX = 15

/** 全部表情：常用笑脸 → 负面/搞怪 → 手势 → 爱心 → 常用符号/物品/动物 */
const EMOJIS: string[] = [
  '😀', '😃', '😄', '😁', '😆', '😅', '🤣', '😂', '🙂', '😉',
  '😊', '😍', '🤩', '😘', '😗', '😚', '😙', '😋', '😛', '😜',
  '🤪', '😝', '🤗', '🤭', '🫢', '🤫', '🤔', '🫡', '🤐', '🤨',
  '😐', '😑', '😶', '😏', '😒', '🙄', '😬', '🤥', '😌', '😔',
  '😪', '🤤', '😴', '😷', '🤒', '🤕', '🤢', '🤮', '🥵', '🥶',
  '🥴', '😵', '🤯', '🥳', '😎', '🤓', '🧐', '😕', '😟', '🙁',
  '😮', '😯', '😲', '😳', '🥺', '🥹', '😦', '😧', '😨', '😰',
  '😥', '😢', '😭', '😱', '😖', '😣', '😞', '😓', '😩', '😫',
  '🥱', '😤', '😡', '😠', '🤬', '😈', '👿', '💀', '🤡', '👻',
  '💩', '🤖', '🙌', '👏', '👍', '👎', '👌', '🤌', '✌️', '🤞',
  '🤟', '🤘', '👋', '🤝', '🙏', '💪', '👉', '👈', '☝️', '✋',
  '🖖', '❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍', '💔',
  '❤️‍🔥', '💕', '💞', '💓', '💗', '💖', '💘', '💝', '💯', '💢',
  '💥', '💫', '💦', '💨', '✨', '⭐', '🌟', '⚡', '🔥', '🌈',
  '☀️', '🌙', '❄️', '🎉', '🎊', '🎁', '🎂', '🍰', '🍀', '🌹',
  '🌸', '💐', '🍎', '🍉', '🍓', '🍺', '🍵', '☕', '🧧', '💰',
  '🛒', '📦', '🚗', '✈️', '🏠', '⏰', '📱', '💻', '🐶', '🐱',
  '🐭', '🐰', '🦊', '🐻', '🐼', '🐮', '🐷', '🐸', '🐵', '🐔',
  '🦆', '🐴', '🐟', '🦋', '🙏',
]

/** 去重 + 去空，保证网格不出现重复格子 */
const UNIQUE_EMOJIS = Array.from(new Set(EMOJIS.filter(Boolean)))

export function EmojiPicker({ onSelect }: { onSelect: (emoji: string) => void }) {
  const [open, setOpen] = useState(false)
  const [recent, setRecent] = useState<string[]>([])
  const rootRef = useRef<HTMLDivElement>(null)

  // 每次打开面板时读一次最近使用（可能在别的标签页改过）
  useEffect(() => {
    if (!open) return
    try {
      const raw = localStorage.getItem(RECENT_KEY)
      const list = raw ? JSON.parse(raw) : []
      if (Array.isArray(list)) {
        setRecent(list.filter((e) => typeof e === 'string' && e).slice(0, RECENT_MAX))
      }
    } catch {
      // localStorage 不可用（隐私模式等）时没有最近使用，不影响主功能
    }
  }, [open])

  // 点面板外或按 Esc 关闭
  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const pick = (emoji: string) => {
    const next = [emoji, ...recent.filter((e) => e !== emoji)].slice(0, RECENT_MAX)
    setRecent(next)
    try {
      localStorage.setItem(RECENT_KEY, JSON.stringify(next))
    } catch {
      // 存不进去就算了，本次会话内仍然生效
    }
    onSelect(emoji)
    setOpen(false)
  }

  const cellClass = 'h-8 w-8 flex items-center justify-center text-xl leading-none rounded-md hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors'

  return (
    <div ref={rootRef} className="relative flex-shrink-0">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        title="表情"
        className={cn(
          'flex items-center justify-center w-9 h-9 border rounded-lg transition-colors',
          'disabled:opacity-50 disabled:cursor-not-allowed',
          open
            ? 'text-blue-500 bg-blue-50 dark:bg-blue-900/30 border-blue-300 dark:border-blue-700'
            : 'text-gray-500 dark:text-gray-300 border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700',
        )}
      >
        <Smile className="w-4 h-4" />
      </button>

      {open && (
        <div className="absolute bottom-full left-0 mb-2 z-50 w-[340px] max-w-[calc(100vw-2rem)] bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-gray-200 dark:border-gray-700 p-3">
          {recent.length > 0 && (
            <>
              <p className="text-xs text-gray-400 mb-1.5">最近使用</p>
              <div className="grid grid-cols-10 gap-0.5 mb-3">
                {recent.map((emoji) => (
                  <button key={`recent-${emoji}`} type="button" title={emoji}
                    onClick={() => pick(emoji)} className={cellClass}>
                    {emoji}
                  </button>
                ))}
              </div>
            </>
          )}
          <p className="text-xs text-gray-400 mb-1.5">全部表情</p>
          <div className="grid grid-cols-10 gap-0.5 max-h-56 overflow-y-auto">
            {UNIQUE_EMOJIS.map((emoji) => (
              <button key={`all-${emoji}`} type="button" title={emoji}
                onClick={() => pick(emoji)} className={cellClass}>
                {emoji}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
