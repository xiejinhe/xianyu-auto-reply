/**
 * 新消息提示音
 *
 * 音频文件放在 public/sounds/{id}.wav。Vite 构建时 public 目录会原样复制到 dist，
 * 因此本地开发与 Docker（nginx 提供静态资源）走的是同一个路径，无需额外配置。
 */
export interface SoundOption {
  /** 音效标识，对应 public/sounds/<id>.wav */
  id: string
  /** 界面显示名称 */
  name: string
  /** 一句话听感描述 */
  desc: string
}

/** 可选提示音列表（数组顺序即界面展示顺序） */
export const SOUND_OPTIONS: SoundOption[] = [
  { id: 'pop', name: '气泡音', desc: '短促现代，不打扰' },
  { id: 'soft2', name: '柔和双音', desc: '温和，像手机提示' },
  { id: 'marimba', name: '马林巴', desc: '木质温暖，像敲木琴' },
  { id: 'pluck', name: '竖琴拨弦', desc: '弦音质感，柔和' },
  { id: 'chime', name: '风铃', desc: '清透空灵，余韵长' },
  { id: 'musicbox', name: '音乐盒', desc: '高频清脆，像八音盒' },
]

const DEFAULT_SOUND_ID = 'pop'
/** 开关状态（'off' 为关闭，其余视为开启） */
const ENABLED_KEY = 'chat_new_message_sound'
/** 选中的音效 id */
const SELECTED_KEY = 'chat_new_message_sound_id'

/** 按音效 id 缓存 Audio 实例，切换音效互不干扰 */
const audioCache = new Map<string, HTMLAudioElement>()

/** 校正音效 id：不在可选列表内时回退到默认音效 */
function resolveSoundId(id: string): string {
  return SOUND_OPTIONS.some((o) => o.id === id) ? id : DEFAULT_SOUND_ID
}

function getAudio(id: string): HTMLAudioElement {
  const key = resolveSoundId(id)
  let a = audioCache.get(key)
  if (!a) {
    a = new Audio(`/sounds/${key}.wav`)
    a.volume = 0.75
    a.preload = 'auto'
    audioCache.set(key, a)
  }
  return a
}

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // localStorage 不可用时忽略，不影响主流程
  }
}

/** 是否开启新消息提示音（默认开启） */
export function isSoundEnabled(): boolean {
  return readStorage(ENABLED_KEY) !== 'off'
}

/** 设置新消息提示音开关 */
export function setSoundEnabled(enabled: boolean): void {
  writeStorage(ENABLED_KEY, enabled ? 'on' : 'off')
}

/** 当前选中的音效 id */
export function getSelectedSoundId(): string {
  return resolveSoundId(readStorage(SELECTED_KEY) || DEFAULT_SOUND_ID)
}

/** 选择音效（立即持久化） */
export function setSelectedSoundId(id: string): void {
  writeStorage(SELECTED_KEY, resolveSoundId(id))
}

/** 播放指定音效 */
function play(id: string): void {
  try {
    const a = getAudio(id)
    a.currentTime = 0
    const p = a.play()
    if (p && typeof p.catch === 'function') p.catch(() => {})
  } catch {
    // 忽略播放异常
  }
}

/**
 * 解锁浏览器音频限制
 *
 * 现代浏览器要求页面先发生用户交互才允许播放音频，否则 play() 会被拒绝。
 * 这里在首次交互时以静音方式播放一次来取得播放权限，之后便可随时播放。
 * 解锁是页面级的，切换音效后无需重新解锁。
 */
let unlocked = false
function unlockAudio(): void {
  if (unlocked) return
  unlocked = true
  const a = getAudio(getSelectedSoundId())
  const prevVolume = a.volume
  a.volume = 0
  const p = a.play()
  if (p && typeof p.then === 'function') {
    p.then(() => {
      a.pause()
      a.currentTime = 0
      a.volume = prevVolume
    }).catch(() => {
      a.volume = prevVolume
    })
  } else {
    a.volume = prevVolume
  }
}

if (typeof window !== 'undefined') {
  const handler = () => {
    unlockAudio()
    window.removeEventListener('pointerdown', handler)
    window.removeEventListener('keydown', handler)
  }
  window.addEventListener('pointerdown', handler)
  window.addEventListener('keydown', handler)
}

/**
 * 播放新消息提示音（受开关控制，使用当前选中的音效）
 *
 * 被浏览器策略拦截时会静默失败，不抛出、不打断消息处理流程。
 */
export function playNewMessageSound(): void {
  if (!isSoundEnabled()) return
  play(getSelectedSoundId())
}

/** 试听指定音效（不受开关控制，供界面选音效时确认听感） */
export function previewSound(id: string): void {
  play(resolveSoundId(id))
}
