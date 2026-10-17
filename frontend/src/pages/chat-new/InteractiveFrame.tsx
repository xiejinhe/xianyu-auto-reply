/**
 * 内嵌跨域网页的交互容器
 *
 * 为什么需要它：跨域 iframe 一旦获得焦点（用户在里面点过/滚过），键盘事件
 * 就全被 iframe 的文档接收，父页面收不到 Esc，弹窗就关不上了——这是浏览器
 * 的安全限制，没有事件层面的解法。
 *
 * 所以默认在网页上盖一层遮罩，点击后才放行交互，并把规则明确告诉用户：
 * - 未激活：Esc 直接关闭弹窗；
 * - 激活后操作页面：Esc 暂时失效，点一下弹窗顶部（标题栏）即可恢复。
 */
import { useEffect, useState } from 'react'
import type { HTMLAttributeReferrerPolicy } from 'react'

interface InteractiveFrameProps {
  src: string
  title: string
  /** iframe 元素自身的 class（尺寸/圆角等由调用方决定） */
  frameClass?: string
  /** 外层容器 class（弹窗里一般给 h-full） */
  className?: string
  /** 变化时强制重载 iframe */
  reloadKey?: number
  referrerPolicy?: HTMLAttributeReferrerPolicy
}

export function InteractiveFrame({
  src,
  title,
  frameClass = 'w-full h-full border-0 bg-white',
  className = '',
  reloadKey = 0,
  referrerPolicy,
}: InteractiveFrameProps) {
  const [interactive, setInteractive] = useState(false)

  // 换了网页（重新打开弹窗/切换目标）时恢复拦截，保证 Esc 默认可用
  useEffect(() => {
    setInteractive(false)
  }, [src])

  return (
    <div className={`relative h-full ${className}`}>
      <iframe
        key={reloadKey}
        src={src}
        title={title}
        className={frameClass}
        referrerPolicy={referrerPolicy}
        style={{ pointerEvents: interactive ? 'auto' : 'none' }}
      />
      {!interactive && (
        <button
          type="button"
          onClick={() => setInteractive(true)}
          className="absolute inset-0 flex items-start justify-center pt-12 bg-black/5 hover:bg-black/10 dark:bg-black/25 transition-colors cursor-pointer"
        >
          <span className="px-4 py-2 rounded-full bg-white/95 dark:bg-slate-800/95 text-sm text-slate-700 dark:text-slate-200 shadow-lg">
            点击加载并操作页面 · 按 Esc 可随时关闭弹窗
          </span>
        </button>
      )}
    </div>
  )
}
