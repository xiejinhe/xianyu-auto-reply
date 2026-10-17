/**
 * 闲鱼表情代码的解析与渲染
 *
 * 闲鱼聊天里的黄脸表情，线上传输的是 `[中文名]` 文本代码（真实买家消息里
 * 可证：原样收到过 "你应该还没下课[捂脸哭]"），动效由客户端本地资源渲染。
 * 所以本系统收到/发出的消息文本里都可能是 `[捂脸哭]` 这种代码，直接显示
 * 会是一串难读的文字 —— 这里把它翻译成语义最接近的 Unicode 表情再渲染，
 * 鼠标悬停仍能看到原始代码，方便对照。
 *
 * 注意：**只翻译白名单里收录的代码**。未收录的（以及 [图片]/[语音] 这类
 * 占位文本）一律原样输出，绝不瞎猜，避免把普通文字吃掉。
 */
import type { ReactNode } from 'react'

/** 闲鱼/淘宝系表情名 → 语义最接近的 Unicode 表情 */
const XIANYU_EMOJI: Record<string, string> = {
  // ===== 脸部：正向 =====
  微笑: '🙂', 嘻嘻: '😁', 哈哈: '😄', 嘿咻: '😄', 嘿嘿: '😁', 大笑: '😄',
  憨笑: '😄', 甜蜜: '🥰', 笑眼: '😊', 眨眼: '😉', 咧嘴: '😁', 调皮: '😜',
  吐舌: '😛', 坏笑: '😏', 偷笑: '😏', 得意: '😎', 酷: '😎', 色: '😍',
  花痴: '🤩', 亲亲: '😘', 飞吻: '😘', 爱你: '😍', 害羞: '😊', 可爱: '🥰',
  睡: '😴', 困: '😪', 饥饿: '🤤', 馋: '😋', 好吃: '😋', 发呆: '😶',
  // ===== 脸部：负面 / 夸张 =====
  撇嘴: '😕', 委屈: '😢', 哭: '😭', 大哭: '😭', 流泪: '😢', 滴泪: '😢',
  捂脸哭: '😭', 泪: '😢', 难过: '😔', 失望: '😞', 尴尬: '😅', 汗: '😓',
  擦汗: '😓', 冷汗: '😰', 狂汗: '😰', 流汗: '😓', 可怜: '🥺', 可怜巴巴: '🥺',
  晕: '😵', 疯了: '🤯', 惊恐: '😱', 惊讶: '😲', 疑问: '🤔', 疑惑: '🤔',
  白眼: '🙄', 鄙视: '😒', 抠鼻: '🤨', 挖鼻: '🤨', 闭嘴: '🤐', 嘘: '🤫',
  抓狂: '😫', 生气: '😠', 发怒: '😡', 咒骂: '🤬', 吐: '🤮', 恶心: '🤢',
  骷髅: '💀', 便便: '💩', 猪头: '🐷', 衰: '😞', 衰样: '😞', 晕菜: '😵‍💫',
  // ===== 手势 / 动作 =====
  鼓掌: '👏', 强: '👍', 赞: '👍', 弱: '👎', 差劲: '👎', 握手: '🤝',
  胜利: '✌️', 耶: '✌️', 抱拳: '🙏', 祈祷: '🙏', 拜托: '🙏', 加油: '💪',
  奋斗: '💪', 拳头: '👊', 敲打: '🔨', 勾引: '👉', 抱抱: '🤗', 拥抱: '🤗',
  再见: '👋', 挥手: '👋', OK: '👌', 好的: '👌', 不: '🙅', NO: '🙅',
  // ===== 心 / 物品 / 自然 =====
  爱心: '❤️', 心: '❤️', 心碎: '💔', 玫瑰: '🌹', 凋谢: '🥀', 礼物: '🎁',
  蛋糕: '🎂', 咖啡: '☕', 啤酒: '🍺', 干杯: '🍻', 红包: '🧧', 钱: '💰',
  太阳: '☀️', 月亮: '🌙', 星星: '⭐', 火: '🔥', 雨: '🌧️', 雪花: '❄️',
  炸弹: '💣', 刀: '🔪', 足球: '⚽', 篮球: '🏀',
}

/** 方括号代码：`[捂脸哭]`。名字里不含方括号，最长 10 字 */
const EMOJI_CODE_RE = /\[([^[\]]{1,10})\]/g

export interface ChatTextSegment {
  type: 'text' | 'emoji'
  /** 文本内容，或翻译后的表情字符 */
  value: string
  /** 表情段才有：原始代码，如 `[捂脸哭]`，用于悬停提示 */
  code?: string
}

/** 把消息文本切成「普通文本 / 表情」片段，供渲染使用 */
export function parseChatText(text: string): ChatTextSegment[] {
  if (!text || !text.includes('[')) return [{ type: 'text', value: text }]
  const segments: ChatTextSegment[] = []
  let last = 0
  for (const match of text.matchAll(EMOJI_CODE_RE)) {
    const start = match.index ?? 0
    const emoji = XIANYU_EMOJI[match[1]]
    if (!emoji) continue // 白名单外的代码保持原样
    if (start > last) segments.push({ type: 'text', value: text.slice(last, start) })
    segments.push({ type: 'emoji', value: emoji, code: match[0] })
    last = start + match[0].length
  }
  if (last < text.length) segments.push({ type: 'text', value: text.slice(last) })
  return segments.length > 0 ? segments : [{ type: 'text', value: text }]
}

/**
 * 渲染一段可能含表情代码的聊天文本。
 * 表情渲染为字符，悬停显示原始代码；普通文本原样输出。
 */
export function ChatText({
  text,
  emojiClassName,
}: {
  text: string
  /** 表情字符的额外样式（如放大一号） */
  emojiClassName?: string
}): ReactNode {
  const segments = parseChatText(text)
  return (
    <>
      {segments.map((segment, idx) =>
        segment.type === 'emoji' ? (
          <span key={idx} title={segment.code} className={emojiClassName}>
            {segment.value}
          </span>
        ) : (
          <span key={idx}>{segment.value}</span>
        ),
      )}
    </>
  )
}
