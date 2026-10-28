/**
 * 在线聊天工作台 - 右侧快捷短语面板
 *
 * 展示与管理可复用的快捷短语：点击即发送，支持新增/编辑/删除。
 * 仅负责渲染与回调透传，数据与业务逻辑由父组件 ChatNew 维护。
 */
import { useEffect, useRef } from 'react'
import { Loader2, Pencil, Plus, Save, Trash2 } from 'lucide-react'
import type { QuickPhrase } from '@/api/chatNew'

interface QuickPhrasesPanelProps {
  /** 快捷短语列表 */
  phrases: QuickPhrase[]
  /** 当前选中的会话ID，为空时禁止发送 */
  activeCid: string
  /** 消息发送中（禁止重复发送） */
  sending: boolean
  /** 正在编辑的短语ID，null 表示新增 */
  editingPhraseId: number | null
  /** 表单：标题 */
  phraseTitle: string
  /** 表单：内容 */
  phraseContent: string
  /** 保存中 */
  savingPhrase: boolean
  /** 点击短语发送内容 */
  onSend: (content: string) => void
  /** 进入编辑态 */
  onEdit: (phrase: QuickPhrase) => void
  /** 删除短语 */
  onDelete: (id: number) => void
  /** 重置表单（新增态） */
  onReset: () => void
  /** 标题输入变化 */
  onTitleChange: (value: string) => void
  /** 内容输入变化 */
  onContentChange: (value: string) => void
  /** 保存（新增或更新） */
  onSave: () => void
}

export function QuickPhrasesPanel({
  phrases,
  activeCid,
  sending,
  editingPhraseId,
  phraseTitle,
  phraseContent,
  savingPhrase,
  onSend,
  onEdit,
  onDelete,
  onReset,
  onTitleChange,
  onContentChange,
  onSave,
}: QuickPhrasesPanelProps) {
  // 点「+」新增时把光标落到名称输入框：此前 onReset 只清空表单，而表单本就
  // 为空（非编辑态）时点击没有任何可见变化，用户以为按钮坏了（2026-10-28 反馈）
  const titleInputRef = useRef<HTMLInputElement>(null)
  const contentInputRef = useRef<HTMLTextAreaElement>(null)
  // 点「编辑」时把光标落到内容框：名称通常不用改，改的是话术正文
  useEffect(() => {
    if (editingPhraseId === null) return
    const el = contentInputRef.current
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  }, [editingPhraseId])
  return (
    <div className="basis-2/5 min-h-0 bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 flex flex-col">
      <div className="p-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <span className="font-medium text-sm text-gray-700 dark:text-gray-300">快捷短语</span>
        <button onClick={() => { onReset(); titleInputRef.current?.focus() }} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded" title="新增短语（光标落到名称输入框）"><Plus className="w-4 h-4 text-blue-500" /></button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-2 space-y-1.5">
        {phrases.length === 0 ? (
          <p className="text-center text-sm text-gray-400 py-4">先在下方添加常用回复</p>
        ) : phrases.map((phrase) => (
          <div key={phrase.id} className="flex items-center gap-1 rounded-lg border border-gray-200 dark:border-gray-700 hover:border-blue-300">
            <button onClick={() => onSend(phrase.content)} disabled={!activeCid || sending} className="flex-1 min-w-0 text-left px-2.5 py-2 disabled:opacity-40" title={`点击发送：${phrase.content}`}>
              <span className="block text-xs font-medium text-gray-700 dark:text-gray-200 truncate">{phrase.title}</span>
              {/* 话术正文支持换行，预览要照原样折行（truncate 会把 \n 压成空格），
                  再用 max-h-12 限高避免长话术把列表顶开；全文在 title 悬浮里看 */}
              <span className="block text-xs text-gray-400 whitespace-pre-wrap break-words max-h-12 overflow-hidden">{phrase.content}</span>
            </button>
            <button onClick={() => onEdit(phrase)} className="p-1 text-gray-400 hover:text-blue-500" title="编辑"><Pencil className="w-3.5 h-3.5" /></button>
            <button onClick={() => onDelete(phrase.id)} className="p-1 mr-1 text-gray-400 hover:text-red-500" title="删除"><Trash2 className="w-3.5 h-3.5" /></button>
          </div>
        ))}
      </div>
      <div className="p-2 border-t border-gray-200 dark:border-gray-700 space-y-1.5">
        <input ref={titleInputRef} value={phraseTitle} onChange={(e) => onTitleChange(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') onSave() }} placeholder="短语名称，例如：催付款" className="w-full px-2 py-1.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700" />
        <div className="flex gap-1.5 items-end">
          {/* 话术正文用多行输入框：下单须知这类话术本身是分段的，单行 input
              会把换行吃掉（2026-10-29 反馈）。Enter 直接换行，保存改用
              ⌘/Ctrl + Enter，避免想换行时误保存。 */}
          <textarea ref={contentInputRef} rows={4} value={phraseContent} onChange={(e) => onContentChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); onSave() } }}
            placeholder="输入发送内容（可直接换行）"
            className="flex-1 min-w-0 px-2 py-1.5 text-xs leading-relaxed border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 resize-y" />
          <button onClick={onSave} disabled={!phraseTitle.trim() || !phraseContent.trim() || savingPhrase} className="px-2.5 py-1.5 rounded bg-blue-500 text-white disabled:opacity-40" title={editingPhraseId ? '保存修改' : '添加短语'}>
            {savingPhrase ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          </button>
        </div>
        <p className="text-[10px] text-gray-400 dark:text-gray-500">Enter 换行 · ⌘/Ctrl + Enter 保存 · 点短语即发送（保留换行）</p>
      </div>
    </div>
  )
}
