/**
 * 新消息提示音设置
 *
 * 铃铛按钮控制总开关，点开面板可选择并试听音效。
 * 开关与选中的音效都持久化在 localStorage，刷新后保持。
 */
import { useEffect, useRef, useState } from 'react'
import { Bell, BellOff, Check } from 'lucide-react'
import {
  SOUND_OPTIONS,
  getSelectedSoundId,
  isSoundEnabled,
  previewSound,
  setSelectedSoundId,
  setSoundEnabled,
} from '@/utils/notificationSound'

export function SoundPicker() {
  const [open, setOpen] = useState(false)
  const [enabled, setEnabled] = useState(() => isSoundEnabled())
  const [selected, setSelected] = useState(() => getSelectedSoundId())
  const rootRef = useRef<HTMLDivElement>(null)

  // 点击面板外部或按 Esc 时关闭
  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
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

  const toggleEnabled = () => {
    const next = !enabled
    setEnabled(next)
    setSoundEnabled(next)
    // 开启时试听一声，方便确认音量是否合适
    if (next) previewSound(selected)
  }

  const pick = (id: string) => {
    setSelected(id)
    setSelectedSoundId(id)
    previewSound(id)
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
        title="新消息提示音设置"
      >
        {enabled ? (
          <Bell className="w-4 h-4 text-blue-500" />
        ) : (
          <BellOff className="w-4 h-4 text-gray-400" />
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-1 w-60 rounded-lg border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-800">
          <button
            onClick={toggleEnabled}
            className="flex w-full items-center justify-between px-3 py-2 text-sm text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-gray-700 rounded-t-lg"
          >
            <span>新消息提示音</span>
            <span className={enabled ? 'text-blue-500' : 'text-gray-400'}>
              {enabled ? '已开启' : '已关闭'}
            </span>
          </button>

          <div className="border-t border-gray-200 dark:border-gray-700" />

          <div className="py-1">
            {SOUND_OPTIONS.map((o) => {
              const active = o.id === selected
              return (
                <button
                  key={o.id}
                  onClick={() => pick(o.id)}
                  className={`flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-50 dark:hover:bg-gray-700 ${
                    active ? 'bg-blue-50 dark:bg-blue-900/30' : ''
                  }`}
                >
                  <span className="flex-1 min-w-0">
                    <span
                      className={`block text-sm ${
                        active ? 'text-blue-600 dark:text-blue-400' : 'text-gray-700 dark:text-gray-200'
                      }`}
                    >
                      {o.name}
                    </span>
                    <span className="block text-xs text-gray-400 truncate">{o.desc}</span>
                  </span>
                  {active && <Check className="w-4 h-4 flex-shrink-0 text-blue-500" />}
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
