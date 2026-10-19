/**
 * 在线聊天 主页面
 *
 * 三栏布局：左侧账号列表 | 中间会话列表 | 右侧聊天记录
 * 支持多账号切换，基于WebSocket API获取数据
 */
import { useEffect, useState, useRef, useCallback, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react'
import { createPortal } from 'react-dom'
import { Loader2, LogIn, LogOut, MessageCircle, RefreshCw, User, ChevronUp, X, Send, AlertCircle, Ban, ImagePlus, Copy, Package, ExternalLink, Mic, Square, FileText, Trash2, Search, Info, Ticket, Truck, CreditCard, RotateCcw, CheckCircle, Calendar, Tag } from 'lucide-react'

// 事件卡片图标：按后端 eventKind 取对应图标，缺省用 Info
const EVENT_ICONS: Record<string, typeof Info> = {
  performance: Ticket, // 演出信息
  refund: RotateCcw, // 退款 / 交易关闭
  shipping: Truck, // 发货 / 物流 / 收货 / 凭证
  payment: CreditCard, // 付款 / 拍下 / 专拍
  done: CheckCircle, // 交易成功
  generic: Info, // 其它系统事件
}
import { useUIStore } from '@/store/uiStore'
import {
  getChatAccounts,
  getConnectedChatAccountIds,
  connectAccount,
  disconnectAccount,
  getConversations,
  hideConversation,
  getMessages,
  sendTextMessage,
  sendImageMessage,
  queryUserInfos,
  getAccountProfile,
  getCustomerOrders,
  getCustomerOrderMarks,
  getQuickPhrases,
  createQuickPhrase,
  updateQuickPhrase,
  deleteQuickPhrase,
  recallMessage,
  sendAudioMessage,
  transcribeAudio,
  transcribeAudioUrl,
  getOfficialBlacklistStatus,
  changeOfficialBlacklist,
  copyImageToClipboard,
  getItemInfo,
  getXianyuUserProfile,
  type ChatAccount,
  type Conversation,
  type ChatItemInfo,
  type ChatMessage,
  type CustomerOrder,
  type CustomerOrderMark,
  type OrderCardData,
  type QuickPhrase,
  type XianyuUserProfile,
} from '@/api/chatNew'
import { cancelOrder, fetchXianyuOrders, getOrderDetail, manualDelivery, noLogisticsDelivery, adjustOrderPrice, type OrderDetail } from '@/api/orders'
import { getOrderStatusMeta, SHIPPABLE_STATUSES } from './orderStatus'
import { useChatNewWs } from './useChatNewWs'

// ==================== 订单卡片富气泡 ====================
/** 与闲鱼 APP 一致的订单卡片：标题/描述/演出信息/订单号 + 动作按钮。
 *  数据来自后端 order_card_parser 解析的卡片富载荷（msg.card）。
 *  按钮分流：
 *  - to_ship（待发货，如门票-提醒发货-卖家）走系统已有发货/无物流发货接口，
 *    订单号缺失时从会话「待发货」订单兜底（该卡原始载荷不带订单号）；
 *  - https 深链（发货凭证 H5）直接新窗口打开；
 *  - 其余写侧动作（买家付款/订单改价/演出信息填写）系统未接，
 *    点击弹 toast 引导去闲鱼 APP 完成。 */
interface OrderCardBubbleProps {
  card: OrderCardData
  /** 会话当前订单列表：卡片没带订单号时，待发货卡从这里取「待发货」订单 */
  sessionOrders: CustomerOrder[]
  deliveringOrderNo: string
  confirmingOrderNo: string
  adjustingOrderNo: string
  onDeliver: (orderNo: string) => void
  onNoLogisticsDeliver: (orderNo: string) => void
  onAdjustPrice: (orderNo: string, currentPrice?: string) => void
  /** 展示闲鱼 APP 深链弹窗（fleamarket:// 原生页面，电脑上打不开，需复制到手机唤起） */
  onDeepLink: (url: string, title: string) => void
  onToast: (toast: { message: string; type: 'success' | 'error' | 'warning' | 'info' }) => void
}

/** 卡片类型 -> 状态小标：卖家一眼看出这张卡该干什么 */
const ORDER_CARD_KIND_HINT: Record<string, string> = {
  placed: '待付款',
  price_modified: '待付款',
  to_fill_show: '待填写演出信息',
  show_submitted: '待确认演出信息',
  show_confirmed: '演出信息已确认',
  to_ship: '待发货',
  shipped: '已发货',
  shipped_notice: '已发货',
}

function OrderCardBubble({
  card,
  sessionOrders,
  deliveringOrderNo,
  confirmingOrderNo,
  adjustingOrderNo,
  onDeliver,
  onNoLogisticsDeliver,
  onAdjustPrice,
  onDeepLink,
  onToast,
}: OrderCardBubbleProps) {
  // 待发货卡（门票-提醒发货-卖家）原始载荷不带订单号，从会话「待发货」订单兜底
  const fallbackOrderNo =
    card.kind === 'to_ship'
      ? sessionOrders.find((o) => SHIPPABLE_STATUSES.includes(o.status))?.order_no || ''
      : ''
  const orderNo = card.order_id || fallbackOrderNo
  const busy = !!orderNo && (deliveringOrderNo === orderNo || confirmingOrderNo === orderNo)
  const kindHint = ORDER_CARD_KIND_HINT[card.kind] || ''
  // 会话订单里匹配的那笔（用于取当前价/判断是否仍待付款）
  const matchedOrder = orderNo ? sessionOrders.find((o) => o.order_no === orderNo) : undefined
  // 「已拍下」卡（待付款）才给修改价格入口：订单同步里有该单时要求仍未付款，
  // 同步里没拉到（matchedOrder 为空）时默认展示，由接口侧最终校验
  const canAdjustPrice =
    card.kind === 'placed' && !!orderNo &&
    (!matchedOrder || matchedOrder.status === 'pending_payment' || matchedOrder.status === '待付款')

  /** https 链接直接新窗口打开，返回是否打开成功 */
  const openUrl = (url: string) => {
    if (url && /^https?:\/\//.test(url)) {
      window.open(url, '_blank', 'noopener,noreferrer')
      return true
    }
    return false
  }

  const handleCardButton = () => {
    const btn = card.button
    if (!btn) return
    // https 链接（发货凭证 H5 等）直接打开
    if (openUrl(btn.url)) return
    if (btn.action === 'goto_pay') {
      onToast({ message: '买家付款动作，请在闲鱼 APP 里完成', type: 'info' })
    } else if (btn.action === 'adjust_price') {
      // 订单改价已实现：有订单号直接开改价弹窗（与「已拍下」卡的修改价格按钮同一条路）
      if (orderNo) {
        onAdjustPrice(orderNo, matchedOrder?.amount)
      } else {
        onToast({ message: '未取到订单号，请在闲鱼 APP 里修改价格', type: 'warning' })
      }
    } else if (btn.action === 'smart_form') {
      // 演出信息表单是闲鱼 APP 原生页面（fleamarket://smart_form），网页端无法代填，
      // 但深链本身有效：弹窗给出链接，复制到手机上即可唤起 APP 直接打开该订单的表单页
      if (btn.url && /^fleamarket:\/\//.test(btn.url)) {
        onDeepLink(btn.url, card.title || '填写演出信息')
      } else {
        onToast({ message: '未取到表单深链，请在闲鱼 APP 里完成', type: 'warning' })
      }
    } else if (btn.action === 'order_detail' && openUrl(card.target_url)) {
      // https 订单详情已直接打开
    } else {
      onToast({ message: '请前往闲鱼 APP 完成该操作', type: 'info' })
    }
  }

  // 演出信息行：值形如「演出名称：北京张杰」，按「：」拆成标签 + 内容
  const showInfoRows: Array<{ key: string; icon: typeof Info; value: string }> = []
  if (card.show_info) {
    if (card.show_info.concertName) showInfoRows.push({ key: 'concertName', icon: Mic, value: card.show_info.concertName })
    if (card.show_info.ticketCount) showInfoRows.push({ key: 'ticketCount', icon: Ticket, value: card.show_info.ticketCount })
    if (card.show_info.date) showInfoRows.push({ key: 'date', icon: Calendar, value: card.show_info.date })
  }

  return (
    <div className="min-w-[240px] max-w-full">
      {/* 头部：图标（用卡片自带底色）+ 标题 + 状态小标 */}
      <div className="flex items-start gap-2">
        <div
          className="flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center"
          style={{ backgroundColor: card.bg_color || '#FFE60F' }}
        >
          <Ticket className="w-4 h-4 text-gray-800" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-sm font-semibold leading-5">{card.title || '订单卡片'}</span>
            {kindHint ? (
              <span className="flex-shrink-0 rounded bg-black/10 dark:bg-white/20 px-1.5 py-0.5 text-[10px] leading-none">
                {kindHint}
              </span>
            ) : null}
          </div>
          {card.desc ? <div className="text-xs opacity-80 leading-4 mt-0.5">{card.desc}</div> : null}
        </div>
      </div>

      {/* 演出信息（门票类卡片） */}
      {showInfoRows.length > 0 ? (
        <div className="mt-2 rounded-md bg-black/5 dark:bg-white/10 px-2.5 py-2 space-y-1">
          {showInfoRows.map(({ key, icon: Icon, value }) => {
            const idx = value.indexOf('：')
            const label = idx > 0 ? value.slice(0, idx + 1) : value
            const content = idx > 0 ? value.slice(idx + 1) : ''
            return (
              <div key={key} className="flex items-start gap-1.5 text-xs leading-4">
                <Icon className="w-3 h-3 flex-shrink-0 opacity-70 mt-0.5" />
                <span className="opacity-70 flex-shrink-0">{label}</span>
                {content ? <span className="font-medium">{content}</span> : null}
              </div>
            )
          })}
        </div>
      ) : null}

      {/* 订单号 */}
      {orderNo ? <div className="text-[11px] opacity-60 mt-1.5">订单号 {orderNo}</div> : null}

      {/* 按钮区 */}
      <div className="flex flex-wrap gap-2 mt-2.5">
        {/* 待发货卡：走系统已有发货接口（与客户订单面板一致） */}
        {card.kind === 'to_ship' && orderNo ? (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => onDeliver(orderNo)}
              className="inline-flex items-center gap-1 rounded-md bg-orange-500 hover:bg-orange-600 disabled:opacity-50 px-3 py-1.5 text-xs font-medium text-white transition-colors"
            >
              <Truck className="w-3.5 h-3.5" />
              {deliveringOrderNo === orderNo ? '发货中…' : '去发货'}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onNoLogisticsDeliver(orderNo)}
              className="inline-flex items-center gap-1 rounded-md border border-current opacity-70 hover:opacity-100 disabled:opacity-50 px-3 py-1.5 text-xs font-medium transition-opacity"
            >
              <Package className="w-3.5 h-3.5" />
              {confirmingOrderNo === orderNo ? '处理中…' : '无物流发货'}
            </button>
          </>
        ) : null}

        {/* 待付款卡（已拍下）：订单改价，议价后改订单总价（买家会收到改价通知） */}
        {canAdjustPrice ? (
          <button
            type="button"
            disabled={adjustingOrderNo === orderNo}
            onClick={() => onAdjustPrice(orderNo, matchedOrder?.amount)}
            className="inline-flex items-center gap-1 rounded-md bg-orange-500 hover:bg-orange-600 disabled:opacity-50 px-3 py-1.5 text-xs font-medium text-white transition-colors"
          >
            <Tag className="w-3.5 h-3.5" />
            {adjustingOrderNo === orderNo ? '改价中…' : '修改价格'}
          </button>
        ) : null}

        {/* 卡片自带按钮（去付款/去填写/查看发货凭证等；adjust_price 由上方修改价格按钮代替） */}
        {card.button && card.button.action !== 'adjust_price' ? (
          <button
            type="button"
            onClick={handleCardButton}
            className="inline-flex items-center gap-1 rounded-md bg-orange-500 hover:bg-orange-600 px-3 py-1.5 text-xs font-medium text-white transition-colors"
          >
            <ExternalLink className="w-3.5 h-3.5" />
            {card.button.text}
          </button>
        ) : null}

        {/* 无自带按钮、但 APP 里整卡可点的确认类卡片：给一个引导按钮 */}
        {!card.button && card.kind === 'show_submitted' ? (
          <button
            type="button"
            onClick={() => onToast({ message: '请在闲鱼 APP 会话中点击该卡片，确认演出信息', type: 'info' })}
            className="inline-flex items-center gap-1 rounded-md border border-current opacity-70 hover:opacity-100 px-3 py-1.5 text-xs font-medium transition-opacity"
          >
            <CheckCircle className="w-3.5 h-3.5" />
            去确认
          </button>
        ) : null}

        {/* 待发货但没拿到订单号（卡片载荷不带 + 订单同步失败）：引导去 APP */}
        {card.kind === 'to_ship' && !orderNo ? (
          <span className="text-xs opacity-60 self-center">未同步到订单号，请在闲鱼 APP 发货</span>
        ) : null}
      </div>
    </div>
  )
}

/** cid 统一比较：推送与会话列表两条来源的 cid 偶有 @goofish 后缀差异，去掉再比。
 *  不做归一化时，正打开的会话会被推送误判成「非当前会话」——列表红点 +1、
 *  消息却不进聊天窗口，只能等 30 秒兜底轮询补上（用户实测延迟约 20 秒）。 */
const normCid = (cid: string) => (cid || '').split('@')[0].trim()
/** 响铃水位/去重用的键：cid 一律归一化，推送侧的 cid 偶尔带 @goofish，
 *  不归一化会导致「同一个会话两个 key」，水位对不上就会重复响铃。 */
const rungKey = (accountId: string, cid: string) => `${accountId}:${normCid(cid)}`
/** 对账容差：服务端会话的 modifyTime 与消息 createAt 不是同一时刻，
 *  通常晚几百毫秒到几秒。严格比较会让同一条消息「推送响一次、对账再响一次」。 */
const RING_TOLERANCE_MS = 3000
/** 「对方正在输入」：收到停止帧后的宽限期 / 无更新时的最长显示时间 */
const TYPING_HIDE_MS = 2200
const TYPING_MAX_MS = 6000

/** 已读水位持久化：不落盘的话刷新页面水位就丢，服务端旧未读又会把红点亮一次 */
const READ_MARKS_KEY = 'chat_conv_read_marks'
const loadReadMarks = (): Record<string, number> => {
  try {
    const raw = localStorage.getItem(READ_MARKS_KEY)
    const parsed = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, number>) : {}
  } catch {
    return {}
  }
}
const saveReadMarks = (marks: Record<string, number>) => {
  try {
    const keys = Object.keys(marks)
    // 控制体积：超过 800 个会话时丢弃最旧的水位
    if (keys.length > 800) {
      keys.sort((a, b) => (marks[a] ?? 0) - (marks[b] ?? 0))
      for (const k of keys.slice(0, keys.length - 800)) delete marks[k]
    }
    localStorage.setItem(READ_MARKS_KEY, JSON.stringify(marks))
  } catch {
    // 存不了（隐私模式等）就算了，水位退化为仅本次页面会话有效
  }
}

// ==================== 桌面端面板拖拽调宽 ====================
/** 三栏可拖拽宽度（px）：账号列表 / 会话列表 / 右侧工作区，localStorage 持久化 */
const PANEL_WIDTHS_KEY = 'chat_new_panel_widths'
const PANEL_DEFAULTS = { accounts: 224, convs: 288, tools: 320 }
type PanelKey = keyof typeof PANEL_DEFAULTS
/** 拖拽方向：右侧工作区在最右边，往左拖才是变宽 */
const PANEL_DRAG_DIR: Record<PanelKey, 1 | -1> = { accounts: 1, convs: 1, tools: -1 }
const PANEL_LIMITS: Record<PanelKey, [number, number]> = {
  accounts: [180, 400],
  convs: [220, 520],
  tools: [240, 560],
}

const loadPanelWidths = (): typeof PANEL_DEFAULTS => {
  const merged = { ...PANEL_DEFAULTS }
  try {
    const raw = localStorage.getItem(PANEL_WIDTHS_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Record<string, unknown>
      for (const k of Object.keys(PANEL_DEFAULTS) as PanelKey[]) {
        const v = Number(parsed[k])
        const [min, max] = PANEL_LIMITS[k]
        if (Number.isFinite(v) && v >= min && v <= max) merged[k] = v
      }
    }
  } catch { /* 坏数据就用默认值 */ }
  return merged
}

/** 会话列表的最后一条摘要：发送者前缀按人着色，
 *  自己发的「你：」用翠绿色、对方发的「他：」用琥珀色，一眼分清谁说的。
 *  注意别用蓝色——摘要里的商品标题就是蓝色，会撞色（2026-10-17 用户反馈）。
 *  系统消息没有前缀，按普通灰色文本渲染。 */
function ConversationSummary({ text }: { text: string }) {
  const raw = text || '暂无消息'
  const matched = /^(你|他)：/.exec(raw)
  const prefix = matched ? matched[1] : ''
  const body = matched ? raw.slice(matched[0].length) : raw
  const prefixClass = prefix === '你'
    ? 'text-emerald-600 dark:text-emerald-400'
    : 'text-amber-600 dark:text-amber-400'
  return (
    <>
      {prefix && <span className={`font-medium ${prefixClass}`}>{prefix}：</span>}
      {body.includes('[') ? <ChatText text={body} /> : body}
    </>
  )
}
import { playNewMessageSound } from '@/utils/notificationSound'
import { appendMsg, mergeConversations, mergeMessages, toTimestampMs } from './chat_history_merge'
import { ChatHistoryRecovery, recoverConversations, recoverMessages,
  type ConversationCache, type MessageCache } from './chat_history_recovery'
import { ConfirmModal } from '@/components/common/ConfirmModal'
import { EmojiPicker } from './EmojiPicker'
import { ChatText } from './ChatText'
import { CustomerOrdersPanel } from './CustomerOrdersPanel'
import { QuickPhrasesPanel } from './QuickPhrasesPanel'
import { OrderDetailModal } from './OrderDetailModal'
import { SoundPicker } from './SoundPicker'
import { XianyuProfileModal } from './XianyuProfileModal'
import { XianyuItemModal } from './XianyuItemModal'
import { blobToWav } from '@/utils/audio'
import { cn } from '@/utils/cn'

/** 检查昵称是否为纯数字（如用户ID），纯数字视为无效昵称 */
const isPureDigits = (name: string) => /^\d+$/.test(name)

/** 闲鱼对部分接口的昵称打了码（如 樱***1）：会话列表、用户主页、账号信息
 *  返回的都是打码名，只有消息体里的 reminderTitle（前端 senderName）是完整的。
 *  取两个昵称里更适合展示的那个：完整 > 打码（含 *） > 空/纯数字。 */
const betterNick = (cur: string, incoming: string): string => {
  const c = (cur || '').trim()
  const n = (incoming || '').trim()
  if (!n || isPureDigits(n)) return c
  if (!c || isPureDigits(c)) return n
  if (c.includes('*') && !n.includes('*')) return n
  return c
}

/** 会话列表本地缓存（localStorage）：闲鱼会话列表接口被限流时（400600001 要
 *  很久才恢复），刷新页面也能先显示上次拿到的列表，不至于空白一片。 */
const CONVS_LS_PREFIX = 'chat_new_convs_cache:'
const readConvCache = (accountId: string): Conversation[] => {
  try {
    const raw = localStorage.getItem(`${CONVS_LS_PREFIX}${accountId}`)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed?.conversations) ? parsed.conversations : []
  } catch {
    return []
  }
}
const writeConvCache = (accountId: string, convs: Conversation[]) => {
  try {
    localStorage.setItem(`${CONVS_LS_PREFIX}${accountId}`,
      JSON.stringify({ savedAt: Date.now(), conversations: convs.slice(0, 50) }))
  } catch {
    // 隐私模式 / 存储配额满：忽略即可
  }
}

const canRecallMessage = (message: ChatMessage) =>
  message.isSelf &&
  !!message.messageId &&
  message.type !== 'system' &&
  Date.now() - toTimestampMs(message.time) >= 0 &&
  Date.now() - toTimestampMs(message.time) <= 120_000

export function ChatNew() {
  const { addToast } = useUIStore()

  // 账号相关（分页加载）
  const [accounts, setAccounts] = useState<ChatAccount[]>([])
  const [activeAccountId, setActiveAccountId] = useState('')
  const [loadingAccounts, setLoadingAccounts] = useState(false)
  const [connectingId, setConnectingId] = useState('')
  const [accountPage, setAccountPage] = useState(1)
  const [accountHasMore, setAccountHasMore] = useState(false)
  // 非当前账号的来消息计数：显示在账号卡片角标上，切到该账号时清零
  const [accountUnread, setAccountUnread] = useState<Record<string, number>>({})
  const accountListRef = useRef<HTMLDivElement>(null)

  // 会话相关
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [loadingConvs, setLoadingConvs] = useState(false)
  const [activeCid, setActiveCid] = useState('')
  const [convHasMore, setConvHasMore] = useState(false)
  const [convCursor, setConvCursor] = useState<number | null>(null)

  // 消息相关
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const [msgHasMore, setMsgHasMore] = useState(false)
  const [msgCursor, setMsgCursor] = useState<number | null>(null)
  const msgContainerRef = useRef<HTMLDivElement>(null)

  // 图片预览
  const [previewImage, setPreviewImage] = useState('')

  // 图片右键菜单：右键聊天图片/预览大图时弹出，菜单里提供「复制图片」
  const [imageMenu, setImageMenu] = useState<{ x: number; y: number; url: string } | null>(null)
  const [copyingImageUrl, setCopyingImageUrl] = useState('')

  // 用户信息缓存（otherUserId -> {avatar, nick}）
  const userInfoCacheRef = useRef<Record<string, { avatar: string; nick: string }>>({})
  // 头像/昵称查询失败记忆（otherUserId -> 失败时间戳）：
  // 后端主页接口被风控时返回空，若不做记忆，列表每次刷新都会重新发起
  // 一轮全量查询，把风控越养越死。5 分钟内不再重试同一用户。
  const userInfoFailAtRef = useRef<Record<string, number>>({})

  // 当前用户（卖家）头像缓存，accountId -> 头像URL，用于消息气泡右侧头像
  const [selfAvatars, setSelfAvatars] = useState<Record<string, string>>({})

  // 闲鱼个人主页弹窗
  const [profileModal, setProfileModal] = useState<{ userId: string; name: string; avatar: string } | null>(null)

  // 语音录制
  const [isRecording, setIsRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const mediaRecRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const recTimerRef = useRef<number | null>(null)
  const recStartTsRef = useRef(0)
  // 本次会话内是否已确认闲鱼网页端不支持发语音（避免每次点发送都白等一次上传）
  const voiceUnsupportedRef = useRef(false)
  const [voiceUnsupported, setVoiceUnsupported] = useState(false)
  // 录完待处理的语音：可以「发送语音」或「转成文字」
  const [pendingAudio, setPendingAudio] = useState<{ blob: Blob; url: string; durationMs: number } | null>(null)
  // 语音转文字中
  const [transcribing, setTranscribing] = useState(false)
  // 对方语音消息转写结果缓存：messageId -> 文本
  const [audioTextMap, setAudioTextMap] = useState<Record<string, string>>({})
  const [transcribingMsgId, setTranscribingMsgId] = useState('')

  // 当前会话关联的商品：图/标题/itemId 来自会话列表，价格需要额外查详情接口
  const [itemInfo, setItemInfo] = useState<ChatItemInfo | null>(null)

  // 对方的闲鱼主页信息（会话顶部的"买家名片"）
  const [peerProfile, setPeerProfile] = useState<XianyuUserProfile | null>(null)
  const [itemModalOpen, setItemModalOpen] = useState(false)

  // 发送消息
  const [inputText, setInputText] = useState('')
  const [sending, setSending] = useState(false)
  // 输入框引用：发送结束后把光标还回去（发送中禁用输入框会让浏览器丢焦点）
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const refocusInput = useCallback(() => {
    const input = inputRef.current
    if (!input) return
    requestAnimationFrame(() => {
      input.focus()
      const end = input.value.length
      input.setSelectionRange(end, end)
    })
  }, [])
  // 发送图片：隐藏的文件选择框引用
  const imageInputRef = useRef<HTMLInputElement>(null)
  const [pendingImage, setPendingImage] = useState<{ file: File; previewUrl: string } | null>(null)
  const pendingImageRef = useRef<{ file: File; previewUrl: string } | null>(null)

  // 当前客户订单与快捷短语
  const [customerOrders, setCustomerOrders] = useState<CustomerOrder[]>([])
  /** 会话列表的订单标记：buyerId -> { count, status }，只算进行中的订单 */
  const [orderMarks, setOrderMarks] = useState<Record<string, CustomerOrderMark>>({})
  const [loadingOrders, setLoadingOrders] = useState(false)
  const [deliveringOrderNo, setDeliveringOrderNo] = useState('')
  const [confirmingOrderNo, setConfirmingOrderNo] = useState('')
  // 订单改价弹窗状态：adjustingOrderNo 非空即打开，adjustPriceInput 是新价格（元）
  const [adjustingOrderNo, setAdjustingOrderNo] = useState('')
  const [adjustPriceInput, setAdjustPriceInput] = useState('')
  const [adjustingPrice, setAdjustingPrice] = useState(false)
  // 闲鱼 APP 深链弹窗：fleamarket:// 原生页面（演出信息表单等）电脑上打不开，
  // 弹出链接让复制到手机上唤起 APP
  const [deepLink, setDeepLink] = useState<{ url: string; title: string } | null>(null)
  const [deepLinkCopied, setDeepLinkCopied] = useState(false)
  const [cancellingOrderNo, setCancellingOrderNo] = useState('')
  const [orderDetail, setOrderDetail] = useState<OrderDetail | null>(null)
  const [loadingOrderDetail, setLoadingOrderDetail] = useState(false)
  const [blacklisting, setBlacklisting] = useState(false)
  const [isOfficiallyBlocked, setIsOfficiallyBlocked] = useState(false)
  const [recallingMessageId, setRecallingMessageId] = useState('')
  const [deletingConvCid, setDeletingConvCid] = useState('')
  const [confirmDialog, setConfirmDialog] = useState<{
    title: string
    message: string
    confirmText: string
    type: 'warning' | 'danger' | 'info'
  } | null>(null)
  const confirmResolverRef = useRef<((confirmed: boolean) => void) | null>(null)
  const [quickPhrases, setQuickPhrases] = useState<QuickPhrase[]>([])
  const [editingPhraseId, setEditingPhraseId] = useState<number | null>(null)
  const [phraseTitle, setPhraseTitle] = useState('')
  const [phraseContent, setPhraseContent] = useState('')
  const [savingPhrase, setSavingPhrase] = useState(false)

  // 页面需要实时订阅的账号列表（已连接 或 已掉线但可恢复的账号都订阅，实现"断线也响"）
  const [wsAccountIds, setWsAccountIds] = useState<string[]>([])
  // 会话列表搜索关键词（本地过滤已加载会话：用户名/用户ID/最后一条消息/商品标题）
  const [convSearch, setConvSearch] = useState('')

  // 断线兜底对账：记录已响铃的会话最新消息时间（accountId:cid -> 时间戳），
  // 避免实时推送与对账轮询对同一消息重复响铃；首轮仅校准水位不响旧未读。
  const rungConvRef = useRef<Record<string, number>>({})
  // 闲鱼流控冷却（账号 -> 解禁时间戳）：会话列表接口被流控后，对账轮询、
  // 读状态刷新都不再去请求该账号，避免把限流窗口一直撞着不恢复。
  // 后端也有一道同样的冷却，这里是为了不产生无意义的本地请求。
  const rateLimitUntilRef = useRef<Record<string, number>>({})
  // 限流兜底提示的去重节流（账号 -> 上次提示时间戳）：后端返回兜底缓存时
  // rateLimited=true，前端给用户说明一次「为什么刷新没有新数据」即可，
  // 同账号 5 分钟内不重复弹，避免后台轮询每 30s 弹一次刷屏。
  const rateLimitedNoticeAtRef = useRef<Record<string, number>>({})
  // 响铃去重：同一条物理消息可能被两条通道各推一次（App 通道 messageId 是 32 位
  // hex、网页 IM 是 xxx.PNM，两套体系没法按 ID 对上），只能按「账号+会话+发送者+
  // 内容」比对，15 秒内同样内容只响一次。
  const recentRingsRef = useRef<Array<{ key: string; at: number }>>([])
  // 每个会话最近一次真正响铃的时刻：对账轮询只是兜底通道，刚为这个会话响过就不许再响。
  const lastRingAtRef = useRef<Record<string, number>>({})
  // 全局基线标记：首轮对账（已连接账号）完成后置 true。首轮只校准水位（历史未读
  // 不响），之后任何在线账号——含断开后才被看门狗恢复连接的账号——漏掉的新未读
  // 都照常补响。用全局标记而非按账号标记，否则"页面打开时断开、之后才恢复"的账号
  // 永远不会被置位，导致断开期间的新消息永远不响（正是用户描述的场景）。
  const baselineDoneRef = useRef(false)

  // 手机端 Tab 切换（桌面端 md+ 仍为四栏并排，本 state 不影响桌面布局）
  type MobileTab = 'accounts' | 'convs' | 'chat' | 'tools'
  const [mobileTab, setMobileTab] = useState<MobileTab>('accounts')

  const requestConfirm = useCallback((options: {
    title?: string
    message: string
    confirmText?: string
    type?: 'warning' | 'danger' | 'info'
  }) => new Promise<boolean>((resolve) => {
    confirmResolverRef.current?.(false)
    confirmResolverRef.current = resolve
    setConfirmDialog({
      title: options.title || '确认操作',
      message: options.message,
      confirmText: options.confirmText || '确定',
      type: options.type || 'warning',
    })
  }), [])

  const closeConfirm = (confirmed: boolean) => {
    confirmResolverRef.current?.(confirmed)
    confirmResolverRef.current = null
    setConfirmDialog(null)
  }

  // 用 ref 保存当前选中的账号和会话，供 WebSocket 回调使用（避免闭包问题）
  const activeAccountIdRef = useRef(activeAccountId)
  useEffect(() => { activeAccountIdRef.current = activeAccountId }, [activeAccountId])
  const activeCidRef = useRef(activeCid)
  useEffect(() => { activeCidRef.current = activeCid }, [activeCid])
  const reloadOrdersRef = useRef<() => void>(() => {})
  const convCursorRef = useRef<number | null>(null)
  useEffect(() => { convCursorRef.current = convCursor }, [convCursor])
  const msgCursorRef = useRef<number | null>(null)
  useEffect(() => { msgCursorRef.current = msgCursor }, [msgCursor])
  const messagesRef = useRef<ChatMessage[]>([])
  useEffect(() => { messagesRef.current = messages }, [messages])
  const conversationsRef = useRef<Conversation[]>([])
  useEffect(() => { conversationsRef.current = conversations }, [conversations])
  const historyPageRef = useRef({ convHasMore, msgHasMore })
  useEffect(() => { historyPageRef.current = { convHasMore, msgHasMore } }, [convHasMore, msgHasMore])
  const convRequestIdRef = useRef<Record<string, number>>({})
  // 会话列表「拉首页」的在途计数（账号 → 在途请求数）：计数 >0 时同一账号的
  // 新首页请求直接复用，不再重复打到闲鱼。开发模式 React.StrictMode 会把 effect
  // 执行两次，页面加载时「选中账号 effect」和「WS 连上补偿」还会各拉一次，
  // 实测一次刷新发 2~4 条请求，直接叠加闲鱼 IM 流控风险。
  // append（加载更多）不参与去重；force（用户手动点刷新）总会真发。
  const convInflightRef = useRef<Map<string, number>>(new Map())
  const msgRequestIdRef = useRef(0)

  /** 同步更新账号 ref，避免账号切换后旧请求把数据写入新账号窗口 */
  const selectAccount = useCallback((accountId: string) => {
    activeAccountIdRef.current = accountId
    setActiveAccountId(accountId)
  }, [])

  /** 同步更新会话 ref，避免会话切换后旧请求把消息写入新会话窗口 */
  const selectCid = useCallback((cid: string) => {
    activeCidRef.current = cid
    setActiveCid(cid)
    // 切换会话时清掉「发语音不可用」的标记：用户可能刚在设置页配好了公网地址
    voiceUnsupportedRef.current = false
    setVoiceUnsupported(false)
  }, [])

  // ==================== 按账号缓存：切换账号时保留数据 ====================
  /** 每个账号的会话列表缓存 */
  const convsCacheRef = useRef<Record<string, ConversationCache>>({})
  /** 每个账号每个会话的消息缓存 */
  const msgsCacheRef = useRef<Record<string, Record<string, MessageCache>>>({})
  const recoveryRef = useRef(new ChatHistoryRecovery())
  /** 记住每个账号上次选中的会话 */
  const activeConvPerAccountRef = useRef<Record<string, string>>({})

  /** 冻结断线前范围；重复断线和后续实时推送不能提前推进补偿边界。 */
  const prepareAccountRecovery = useCallback((accountId: string, disconnected = false) => {
    let conversationCache = convsCacheRef.current[accountId]
    const messageCaches = { ...msgsCacheRef.current[accountId] }
    if (accountId === activeAccountIdRef.current) {
      conversationCache = { convs: conversationsRef.current,
        hasMore: historyPageRef.current.convHasMore, cursor: convCursorRef.current }
      if (activeCidRef.current) {
        messageCaches[activeCidRef.current] = { msgs: messagesRef.current,
          hasMore: historyPageRef.current.msgHasMore, cursor: msgCursorRef.current }
      }
    }
    recoveryRef.current.prepare(accountId, conversationCache, messageCaches, disconnected)
  }, [])

  /**
   * 同步当前会话列表到缓存
   * 注意：切换账号时 activeAccountId 已变但 conversations 还是旧账号的，
   * 必须跳过这次同步，否则会把旧数据写到新账号的缓存中
   */
  const convSyncAccountRef = useRef('')
  useEffect(() => {
    if (activeAccountId !== convSyncAccountRef.current) {
      convSyncAccountRef.current = activeAccountId
      return // activeAccountId 刚切换，conversations 还是旧的，跳过
    }
    if (activeAccountId && conversations.length > 0) {
      convsCacheRef.current[activeAccountId] = { convs: conversations, hasMore: convHasMore, cursor: convCursor }
    }
  }, [conversations, convHasMore, convCursor, activeAccountId])
  /** 同步当前消息列表到缓存（同理，切换账号或会话时跳过） */
  const msgSyncKeyRef = useRef('')
  useEffect(() => {
    const key = `${activeAccountId}:${activeCid}`
    if (key !== msgSyncKeyRef.current) {
      msgSyncKeyRef.current = key
      return
    }
    if (activeAccountId && activeCid && messages.length > 0) {
      if (!msgsCacheRef.current[activeAccountId]) msgsCacheRef.current[activeAccountId] = {}
      msgsCacheRef.current[activeAccountId][activeCid] = { msgs: messages, hasMore: msgHasMore, cursor: msgCursor }
    }
  }, [messages, msgHasMore, msgCursor, activeAccountId, activeCid])
  /** 记住每个账号的当前会话（同理跳过） */
  const cidSyncAccountRef = useRef('')
  useEffect(() => {
    if (activeAccountId !== cidSyncAccountRef.current) {
      cidSyncAccountRef.current = activeAccountId
      return
    }
    if (activeAccountId) activeConvPerAccountRef.current[activeAccountId] = activeCid
  }, [activeCid, activeAccountId])

  // ==================== WebSocket 实时推送（多账号） ====================

  // 已读水位：打开/正在查看会话时记录其 lastMessageTime。闲鱼服务端的会话未读
  // (redPoint) 没有「标记已读」接口可调（系统从未清过它），重新拉会话列表会按
  // 服务端旧未读把已经看过的红点复活——这是小红点不准的根源。合并服务端数据时
  // 按水位把已看过的会话红点归零；新消息时间大于水位时红点照常显示，不影响真未读。
  const readMarkRef = useRef<Record<string, number>>(loadReadMarks())

  const markConvReadByKey = useCallback((accountId: string, cid: string, time?: number) => {
    if (!accountId || !cid) return
    const key = `${accountId}:${normCid(cid)}`
    const list = convsCacheRef.current[accountId]?.convs ?? []
    const conv = list.find((c) => normCid(c.cid) === normCid(cid))
    const t = time ?? conv?.lastMessageTime ?? Date.now()
    if (t > (readMarkRef.current[key] ?? 0)) {
      readMarkRef.current[key] = t
      saveReadMarks(readMarkRef.current)
    }
  }, [])

  // 合并服务端会话数据时套用已读水位
  const applyReadMarks = useCallback((accountId: string, convs: Conversation[]): Conversation[] => {
    if (!accountId) return convs
    return convs.map((c) => {
      const mark = readMarkRef.current[`${accountId}:${normCid(c.cid)}`] ?? 0
      return c.unreadCount > 0 && mark >= (c.lastMessageTime || 0)
        ? { ...c, unreadCount: 0 }
        : c
    })
  }, [])

  // 清掉某个会话的红点（正在看的会话 = 已读），同步缓存与界面
  const clearConvUnread = useCallback((accountId: string, cid: string) => {
    if (!accountId || !cid) return
    markConvReadByKey(accountId, cid)
    const key = normCid(cid)
    const zero = (c: Conversation) => (normCid(c.cid) === key ? { ...c, unreadCount: 0 } : c)
    const cache = convsCacheRef.current[accountId]
    if (cache) cache.convs = cache.convs.map(zero)
    if (accountId === activeAccountIdRef.current) {
      conversationsRef.current = conversationsRef.current.map(zero)
      setConversations(conversationsRef.current)
    }
  }, [markConvReadByKey])

  // 消息体里的 senderName 是完整昵称（会话列表/主页接口都被闲鱼打了码），
  // 拉到消息后把【当前会话】的展示名升级成完整版：会话列表、顶部标题、名片随之变全。
  // 只动匹配 cid 的那一个会话，别把别人的打码名也换成这个人的昵称。
  const upgradeConvNickFromMsgs = useCallback((accountId: string, cid: string, msgs: ChatMessage[]) => {
    const sender = msgs.find((m) => !m.isSelf && m.senderName)
    if (!sender?.senderName) return
    const full = sender.senderName
    const key = normCid(cid)
    const upgrade = (c: Conversation): Conversation => {
      if (normCid(c.cid) !== key) return c
      const next = betterNick(c.otherUserName, full)
      return next === c.otherUserName ? c : { ...c, otherUserName: next }
    }
    const cache = convsCacheRef.current[accountId]
    if (cache) cache.convs = cache.convs.map(upgrade)
    // 顺手把用户信息缓存里的打码昵称也升级掉，后续重拉列表时用它补名
    if (sender.senderId) {
      const info = userInfoCacheRef.current[sender.senderId]
      if (info) userInfoCacheRef.current[sender.senderId] = { ...info, nick: betterNick(info.nick, full) }
    }
    if (accountId === activeAccountIdRef.current) {
      conversationsRef.current = conversationsRef.current.map(upgrade)
      setConversations(conversationsRef.current)
    }
  }, [])

  /** 更新会话列表的通用逻辑（可作用于 state 数组或缓存数组） */
  const updateConvList = (convs: Conversation[], cid: string, summary: string, msg: ChatMessage, isViewing: boolean): Conversation[] => {
    const key = normCid(cid)
    const sameConv = (c: Conversation) => normCid(c.cid) === key
    const exists = convs.some(sameConv)
    if (exists) {
      const updated = convs.map((c) => {
        if (!sameConv(c)) return c
        return { ...c, lastMessageSummary: summary, lastMessageTime: msg.time,
          unreadCount: isViewing ? 0 : c.unreadCount + 1,
          // 消息体里的 senderName 是完整昵称，会话列表里的往往被打码（樱***1），顺手升级
          otherUserName: msg.isSelf ? c.otherUserName : betterNick(c.otherUserName, msg.senderName || '') }
      })
      const target = updated.find(sameConv)!
      return [target, ...updated.filter((c) => !sameConv(c))]
    }
    // 新会话
    const newConv: Conversation = {
      cid, rawCid: cid,
      otherUserId: msg.isSelf ? '' : msg.senderId,
      otherUserName: msg.isSelf ? '' : (msg.senderName || ''),
      otherUserAvatar: '', itemTitle: '',
      lastMessageSummary: summary, lastMessageTime: msg.time,
      unreadCount: isViewing ? 0 : 1,
    }
    return [newConv, ...convs]
  }

  // 用刚拉到的**整段消息**校正会话列表条目（摘要 + 时间，必要时置顶）。
  // 会话列表接口被闲鱼限流时列表会停更，但消息接口是另一条通道、仍然通畅，
  // 不回写就会出现「聊天窗口显示 23:42、列表还停在 12:56」的对不上。
  // 与 updateConvList 的区别：那个是「来了一条新消息」（累加未读、强制置顶），
  // 这个是「按已有消息校正」——不累加未读，只在时间确实更新时才置顶，
  // 且字段没变时原样返回，避免 15 秒轮询白白触发重渲染。
  const syncConvFromMessages = (accountId: string, cid: string, msgs: ChatMessage[]): void => {
    const latest = msgs[msgs.length - 1]
    if (!latest) return
    const body = latest.type === 'image' ? '[图片]' : (latest.text || '').slice(0, 50)
    const summary = `${latest.isSelf ? '你' : '他'}：${body}`
    const key = normCid(cid)
    const isViewing = accountId === activeAccountIdRef.current && key === normCid(activeCidRef.current)
    const applyList = (convs: Conversation[]): Conversation[] => {
      const idx = convs.findIndex((c) => normCid(c.cid) === key)
      if (idx < 0) return convs
      const cur = convs[idx]
      const nextTime = Math.max(cur.lastMessageTime || 0, latest.time)
      const nextName = latest.isSelf ? cur.otherUserName : betterNick(cur.otherUserName, latest.senderName || '')
      const nextUnread = isViewing ? 0 : cur.unreadCount
      // 字段完全没变就原样返回，别为轮询制造新数组
      if (cur.lastMessageSummary === summary && (cur.lastMessageTime || 0) === nextTime
        && cur.otherUserName === nextName && cur.unreadCount === nextUnread) {
        return convs
      }
      const patched: Conversation = {
        ...cur,
        lastMessageSummary: summary,
        lastMessageTime: nextTime,
        unreadCount: nextUnread,
        otherUserName: nextName,
      }
      // 时间没前进就不置顶，只就地校正，保持列表顺序稳定
      if (nextTime === (cur.lastMessageTime || 0)) {
        const next = [...convs]
        next[idx] = patched
        return next
      }
      return [patched, ...convs.filter((_, i) => i !== idx)]
    }
    const cache = convsCacheRef.current[accountId]
    if (cache) cache.convs = applyList(cache.convs)
    if (accountId === activeAccountIdRef.current) {
      const next = cache?.convs ?? applyList(conversationsRef.current)
      // 引用没变说明字段毫无变化：不 setState、不落盘，别为 15 秒轮询白忙
      if (next !== conversationsRef.current) {
        conversationsRef.current = next
        setConversations(next)
        // 顺手落一份本地缓存：刷新页面时列表先拿它顶上，不至于回到停更的旧数据
        writeConvCache(accountId, next)
      }
    }
  }

  // 断线兜底：记录已响铃会话的最新消息时间，避免对账轮询对同一消息重复响
  const markRung = useCallback((accountId: string, cid: string, time: number) => {
    const key = rungKey(accountId, cid)
    const prev = rungConvRef.current[key] ?? 0
    if (time > prev) rungConvRef.current[key] = time
  }, [])

  // 实时推送通道的响铃：同一条消息被两条通道重复投递时只响一次。
  // 判定键不含时间戳（两侧时间可能差几毫秒），只看「谁发的 + 说了什么」，15 秒窗口。
  const ringOnce = useCallback((accountId: string, cid: string, msg: ChatMessage) => {
    const now = Date.now()
    const key = [
      accountId, normCid(cid), msg.senderId, msg.type,
      (msg.text || '').slice(0, 80), (msg.images || [])[0] || '',
    ].join('|')
    recentRingsRef.current = recentRingsRef.current.filter((r) => now - r.at < 15000)
    if (recentRingsRef.current.some((r) => r.key === key)) return false
    recentRingsRef.current.push({ key, at: now })
    lastRingAtRef.current[rungKey(accountId, cid)] = now
    playNewMessageSound()
    return true
  }, [])

  // 断线兜底对账：账号恢复连接后，把漏掉的新消息补响 + 未读角标。
  // unreadCount 只统计对方发来的未读，自己发出的消息不会触发响铃。
  const ringMissedIfAny = useCallback((accountId: string, cid: string, lastMessageTime: number, unreadCount: number) => {
    const key = rungKey(accountId, cid)
    const prev = rungConvRef.current[key] ?? 0
    if (lastMessageTime > prev) rungConvRef.current[key] = lastMessageTime
    // 首轮（baselineDone 未置位）仅校准水位，不响历史未读，避免打开页面时把旧未读全响一遍
    if (!baselineDoneRef.current) return
    // 同一条消息：modifyTime 比推送里的 createAt 晚几百毫秒到几秒，用容差吃掉这段差值
    if (unreadCount <= 0 || lastMessageTime <= prev + RING_TOLERANCE_MS) return
    // 该会话刚响过（实时推送通道），兜底通道 20 秒内不再重复响
    const now = Date.now()
    if (now - (lastRingAtRef.current[key] ?? 0) < 20000) return
    lastRingAtRef.current[key] = now
    playNewMessageSound()
    if (accountId !== activeAccountIdRef.current) {
      setAccountUnread((p) => ({ ...p, [accountId]: (p[accountId] || 0) + 1 }))
    }
  }, [])

  // 对账轮询发现某会话有新动静时，把服务端最新会话数据合并进列表状态。
  // 此前对账只响铃不回写，推送缺失时（闲鱼不给网页 IM 稳定推 APP 来源消息）
  // 列表摘要和红点要等用户手动刷新才出现——这是"铃声响但列表不动"的根因。
  const syncConvFromReconcile = useCallback((accountId: string, conv: Conversation) => {
    const key = normCid(conv.cid)
    const isViewingConv = accountId === activeAccountIdRef.current &&
      key === normCid(activeCidRef.current)
    // 正在看的会话 = 已读：套水位归零红点，防止服务端旧未读把红点亮回来
    const serverConv: Conversation = isViewingConv ? { ...conv, unreadCount: 0 } : conv
    if (isViewingConv) markConvReadByKey(accountId, conv.cid, conv.lastMessageTime)
    const cache = convsCacheRef.current[accountId]
    if (cache) {
      const exists = cache.convs.some((c) => normCid(c.cid) === key)
      cache.convs = exists
        ? cache.convs.map((c) => (normCid(c.cid) === key ? { ...c, ...serverConv } : c))
        : [serverConv, ...cache.convs]
    }
    if (accountId === activeAccountIdRef.current) {
      conversationsRef.current = cache?.convs ?? [serverConv, ...conversationsRef.current]
      setConversations(conversationsRef.current)
    }
  }, [markConvReadByKey])

  // ==================== 对方正在输入 ====================
  // 闲鱼是「边打字边推」：实测 1（正在输入）/0（停止）大约 1~2 秒交替一次，
  // 收到 0 就立刻清空的话提示会闪断，用户看到的就是"闪一下就没"。
  // 所以收到 0 只挂一个宽限计时器，期间只要有新的 1 到达就取消；
  // 1 则挂一个较长的兜底超时（对方停手时偶尔漏发 0 帧）。
  const [typingConv, setTypingConv] = useState<{ accountId: string; cid: string } | null>(null)
  const typingTimerRef = useRef<number | null>(null)
  const handleWsTyping = useCallback((accountId: string, cid: string, typing: boolean) => {
    if (typingTimerRef.current) {
      window.clearTimeout(typingTimerRef.current)
      typingTimerRef.current = null
    }
    if (!typing) {
      typingTimerRef.current = window.setTimeout(() => {
        setTypingConv(null)
        typingTimerRef.current = null
      }, TYPING_HIDE_MS)
      return
    }
    setTypingConv({ accountId, cid: normCid(cid) })
    typingTimerRef.current = window.setTimeout(() => {
      setTypingConv(null)
      typingTimerRef.current = null
    }, TYPING_MAX_MS)
  }, [])

  const handleWsNewMessage = useCallback((accountId: string, cid: string, msg: ChatMessage) => {
    // 会话列表摘要带发送者前缀：自己的「你：」，对方的「他：」（性别拿不到，统一「他」）
    const body = msg.type === 'image' ? '[图片]' : (msg.text || '').slice(0, 50)
    const summary = `${msg.isSelf ? '你' : '他'}：${body}`
    const isActiveAccount = accountId === activeAccountIdRef.current
    // 新私聊提示音：只响买家发来的消息，自己发出去的不响。
    // 不限账号：两个账号都托管在同一个界面里，任何一个账号的买家消息漏响不得。
    if (!msg.isSelf) {
      ringOnce(accountId, cid, msg)
      // 同步响铃水位，避免断线兜底对账轮询对同一消息重复响
      markRung(accountId, cid, msg.time)
    }
    // 非当前账号收到新消息：账号卡片累计未读角标（切到该账号时清零）
    if (!isActiveAccount && !msg.isSelf) {
      setAccountUnread((prev) => ({ ...prev, [accountId]: (prev[accountId] || 0) + 1 }))
    }

    // 闲鱼的会话 cid 可能跨账号共享（同一买家与多个卖家账号共用同一个 cid，
    // 实测两个账号请求同一 cid 的消息都返回 200）：「是不是正在看的会话」
    // 因此只按 cid 判断、不看消息所属账号。此前要求 accountId 等于当前账号，
    // 导致手机 APP 登录另一个账号发的消息界面完全静默（isSelf 连角标都不加），
    // 用户只能手动刷新会话列表才看到（2026-10-19 实测复现）。
    const isViewingConv = normCid(cid) === normCid(activeCidRef.current)
    if (isViewingConv) {
      markConvReadByKey(activeAccountIdRef.current, cid, msg.time)
      // 消息已经到达，输入提示立即撤掉（等下一个 0 帧太慢）；
      // 顺带清掉待执行的宽限计时器，否则它会把之后新一轮的「正在输入」也关掉
      if (typingTimerRef.current) {
        window.clearTimeout(typingTimerRef.current)
        typingTimerRef.current = null
      }
      setTypingConv(null)
    }
    // 消息所属账号自己的会话缓存照常更新（切到该账号时摘要是新的）；
    // 屏幕账号不是它时不算"正在看"，未读照常 +1
    const conversationCache = convsCacheRef.current[accountId]
    if (conversationCache) {
      conversationCache.convs = updateConvList(conversationCache.convs, cid, summary, msg, isViewingConv && isActiveAccount)
    }
    if (isViewingConv) {
      // 正在看的会话：无论这条推送属于哪个账号，都投影到当前窗口。
      // 屏幕上下文是当前活跃账号的——消息缓存、消息列表、会话列表摘要
      // 全部按活跃账号写，订单角标也一起刷。
      const screenAccount = activeAccountIdRef.current
      msgsCacheRef.current[screenAccount] ??= {}
      let messageCache = msgsCacheRef.current[screenAccount][cid]
      if (!messageCache) {
        messageCache = { msgs: messagesRef.current,
          hasMore: historyPageRef.current.msgHasMore, cursor: msgCursorRef.current }
        msgsCacheRef.current[screenAccount][cid] = messageCache
      }
      messageCache.msgs = appendMsg(messageCache.msgs, msg)
      const screenCache = convsCacheRef.current[screenAccount]
      const nextConversations = screenCache?.convs
        ? updateConvList(screenCache.convs, cid, summary, msg, true)
        : updateConvList(conversationsRef.current, cid, summary, msg, true)
      if (screenCache) screenCache.convs = nextConversations
      conversationsRef.current = nextConversations
      setConversations(nextConversations)
      messagesRef.current = messageCache.msgs
      setMessages(messageCache.msgs)
      window.setTimeout(() => reloadOrdersRef.current(), 900)
    } else if (isActiveAccount) {
      // 当前账号的非当前会话：只更新屏幕上的列表摘要
      const nextConversations = conversationCache?.convs ??
        updateConvList(conversationsRef.current, cid, summary, msg, false)
      conversationsRef.current = nextConversations
      setConversations(nextConversations)
    }
  }, [markConvReadByKey])

  // WebSocket 断连时刷新账号状态，保留订阅以等待 IM 临时离线后恢复
  // （节流：最多每 5 秒刷一次，避免网络抖动时请求风暴）
  const lastDisconnectRefreshRef = useRef(0)
  const handleWsDisconnect = useCallback((accountId: string) => {
    prepareAccountRecovery(accountId, true)
    const now = Date.now()
    if (now - lastDisconnectRefreshRef.current < 5000) return
    lastDisconnectRefreshRef.current = now
    getChatAccounts(1)
      .then((res) => {
        setAccounts(res.data)
        setAccountPage(1)
        setAccountHasMore(res.hasMore)
      })
      .catch((error: Error) => addToast({ message: error.message, type: 'error' }))
  }, [addToast, prepareAccountRecovery])

  // WebSocket 首次连接或重连成功后，补偿会话缓存，并刷新当前窗口
  const reloadCurrentChatRef = useRef<(accountId: string) => void>(() => {})
  // 供对账轮询调用：正在看的会话有新动静时立即补拉消息，不等 15 秒窗口轮询
  const catchUpConvRef = useRef<(accountId: string, cid: string) => Promise<void>>(async () => {})
  const handleWsConnected = useCallback((accountId: string, isReconnect: boolean) => {
    setAccounts((prev) => prev.map((account) => (
      account.account_id === accountId ? { ...account, connected: true } : account
    )))
    // 首次建连只补当前窗口，避免页面刷新时对全部在线账号并发拉取；
    // 断线重连仍需补偿所有账号，确保断线期间的新会话不会丢失。
    if (isReconnect || accountId === activeAccountIdRef.current) {
      prepareAccountRecovery(accountId)
      void reloadCurrentChatRef.current(accountId)
    }
  }, [prepareAccountRecovery])

  // 后端 IM 长连接掉线后自愈：掉线期间的消息服务端不补推，这里主动补拉一次
  const handleWsReconnected = useCallback((accountId: string) => {
    setAccounts((prev) => prev.map((account) => (
      account.account_id === accountId ? { ...account, connected: true } : account
    )))
    prepareAccountRecovery(accountId)
    void reloadCurrentChatRef.current(accountId)
  }, [prepareAccountRecovery])

  // 为当前页面需要实时接收消息的账号建立独立 WebSocket
  useChatNewWs({
    accountIds: wsAccountIds,
    onNewMessage: handleWsNewMessage,
    onConnected: handleWsConnected,
    onDisconnect: handleWsDisconnect,
    onReconnected: handleWsReconnected,
    onTyping: handleWsTyping,
  })

  // ==================== 加载账号列表（分页） ====================
  const loadAccounts = useCallback(async (page = 1) => {
    setLoadingAccounts(true)
    try {
      const res = await getChatAccounts(page)
      if (page === 1) {
        setAccounts(res.data)
        // 刷新页面后默认选中第一个已连接账号，会话列表/聊天窗口不至于一片空白。
        // 只在当前没有选中账号时触发；只选「已连接」的（未连接的不自动 connect，
        // 避免打开页面就触发连接动作），一个连接的账号都没有则保持原样。
        if (!activeAccountIdRef.current) {
          const firstConnected = res.data.find((a) => a.connected)
          if (firstConnected) selectAccount(firstConnected.account_id)
        }
      } else {
        setAccounts((prev) => [...prev, ...res.data])
      }
      setAccountPage(page)
      setAccountHasMore(res.hasMore)
      // 订阅范围：已连接 或 已掉线但可自动恢复（has_client）的账号都订阅实时通道，
      // 这样断线期间前端通道不丢，账号恢复后由 reconnected 事件补拉并响铃。
      // 完全断开（无客户端）的账号不订阅，避免前端空转重连。
      const subscribeIds = res.data
        .filter((a) => a.connected || a.has_client)
        .map((a) => a.account_id)
      setWsAccountIds((prev) => {
        const merged = [...prev]
        for (const id of subscribeIds) {
          if (!merged.includes(id)) merged.push(id)
        }
        return merged
      })
    } catch (e: any) {
      addToast({ message: e.message || '获取账号列表失败', type: 'error' })
    } finally {
      setLoadingAccounts(false)
    }
  }, [addToast, selectAccount])

  /** 加载更多账号 */
  const loadMoreAccounts = useCallback(() => {
    if (!loadingAccounts && accountHasMore) {
      loadAccounts(accountPage + 1)
    }
  }, [loadingAccounts, accountHasMore, accountPage, loadAccounts])

  useEffect(() => {
    loadAccounts()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // 页面刷新后，自动恢复所有已连接账号的实时消息订阅
  useEffect(() => {
    let cancelled = false
    getConnectedChatAccountIds()
      .then((accountIds) => {
        if (cancelled || accountIds.length === 0) return
        setWsAccountIds((prev) => {
          const next = [...prev]
          for (const accountId of accountIds) {
            if (!next.includes(accountId)) next.push(accountId)
          }
          return next.length === prev.length ? prev : next
        })
      })
      .catch((e: any) => {
        if (!cancelled) {
          addToast({ message: e.message || '获取已连接账号失败', type: 'error' })
        }
      })
    return () => {
      cancelled = true
    }
  }, [addToast])

  // ==================== 连接/断开 ====================
  const handleConnect = async (accountId: string) => {
    setConnectingId(accountId)
    try {
      const res = await connectAccount(accountId)
      if (res.success) {
        addToast({ message: '连接成功', type: 'success' })
        await loadAccounts()
        selectAccount(accountId)
        setAccountUnread((prev) => (prev[accountId] ? { ...prev, [accountId]: 0 } : prev))
        setWsAccountIds((prev) => prev.includes(accountId) ? prev : [...prev, accountId])
        // 手机端：连接成功后自动切到"会话"Tab（桌面端不受影响）
        setMobileTab('convs')
      } else {
        addToast({ message: res.message || '连接失败', type: 'error' })
      }
    } catch (e: any) {
      addToast({ message: e.message || '连接失败', type: 'error' })
    } finally {
      setConnectingId('')
    }
  }

  const handleDisconnect = async (accountId: string) => {
    prepareAccountRecovery(accountId, true)
    try {
      await disconnectAccount(accountId)
      addToast({ message: '已断开连接', type: 'success' })
      setWsAccountIds((prev) => prev.filter((id) => id !== accountId))
      if (activeAccountId === accountId) {
        selectAccount('')
        setConversations([])
        selectCid('')
        setMessages([])
        // 手机端：当前账号被断开后回到"账号"Tab
        setMobileTab('accounts')
      }
      await loadAccounts()
    } catch (e: any) {
      addToast({ message: e.message || '断开失败', type: 'error' })
    }
  }

  /** 点击账号卡片：已连接则选中，未连接则自动连接 */
  const handleSelectAccount = async (acc: ChatAccount) => {
    if (acc.connected) {
      selectAccount(acc.account_id)
      setAccountUnread((prev) => (prev[acc.account_id] ? { ...prev, [acc.account_id]: 0 } : prev))
      // 选中已连接账号时也建立 WebSocket（页面刷新后首次选中时触发）
      setWsAccountIds((prev) => prev.includes(acc.account_id) ? prev : [...prev, acc.account_id])
      // 手机端：选中已连接账号后切到"会话"Tab
      setMobileTab('convs')
    } else {
      await handleConnect(acc.account_id)
    }
  }

  // ==================== 加载会话列表 ====================
  /** 批量拉会话列表的订单标记：只查本地库（xy_orders），不碰闲鱼接口、无风控风险。
   *  角标是辅助信息，失败一律静默。 */
  const loadOrderMarks = useCallback(async (accountId: string) => {
    const convs = convsCacheRef.current[accountId]?.convs ?? []
    const ids = Array.from(new Set(convs.map((c) => c.otherUserId).filter(Boolean)))
    if (!ids.length) {
      setOrderMarks({})
      return
    }
    try {
      const marks = await getCustomerOrderMarks(accountId, ids)
      if (accountId === activeAccountIdRef.current) setOrderMarks(marks)
    } catch { /* 静默：角标拉不到就先不显示 */ }
  }, [])

  const loadConversations = useCallback(async (accountId: string, append = false, merge = false, force = false) => {
    if (!accountId) return
    // 在途去重：同一账号的首页请求已在飞就复用，避免重复请求打到闲鱼
    if (!append) {
      if (!force && (convInflightRef.current.get(accountId) ?? 0) > 0) return
      convInflightRef.current.set(accountId, (convInflightRef.current.get(accountId) ?? 0) + 1)
    }
    const requestId = (convRequestIdRef.current[accountId] ?? 0) + 1
    convRequestIdRef.current[accountId] = requestId
    const generation = recoveryRef.current.generation(accountId)
    const isCurrent = () => requestId === convRequestIdRef.current[accountId] &&
      generation === recoveryRef.current.generation(accountId)
    const isActive = () => accountId === activeAccountIdRef.current
    if (isActive()) setLoadingConvs(true)
    const existing = convsCacheRef.current[accountId] ?? { convs: [], hasMore: false, cursor: null }
    let boundary = recoveryRef.current.conversations.get(accountId)
    const compensating = merge || (append && !!boundary)
    if (compensating) boundary ??= recoveryRef.current.ensureConversations(accountId, existing)

    // 本地缓存兜底：刚打开页面 / 切账号时先显示上次的列表，
    // 服务端数据到了再整体替换（限流时这就是唯一能看到的数据）
    if (!append && !compensating && existing.convs.length === 0) {
      const local = readConvCache(accountId)
      if (local.length) {
        convsCacheRef.current[accountId] = { convs: local, hasMore: existing.hasMore, cursor: existing.cursor }
        if (isActive()) {
          conversationsRef.current = local
          setConversations(local)
          // 缓存顶上的也要有订单角标（角标查本地库，零风控风险）
          void loadOrderMarks(accountId)
        }
      }
    }

    // 后端在账号被闲鱼限流时会回退 Redis 兜底缓存并带 rateLimited=true。
    // recoverConversations 只透传 items/hasMore/cursor，用闭包变量记录，
    // 补偿链路（多页）里只要有一页是兜底就算数。
    let sawRateLimited = false
    const fetchPage = async (cursor?: number) => {
      const page = await getConversations(accountId, cursor)
      if (page.rateLimited) sawRateLimited = true
      return { items: page.conversations, hasMore: page.hasMore, nextCursor: page.nextCursor }
    }
    const request = compensating && boundary
      ? recoverConversations(fetchPage, boundary, isCurrent)
      : fetchPage(append ? existing.cursor ?? undefined : undefined).then((page) => ({
        items: page.items, hasMore: page.hasMore, cursor: page.nextCursor, complete: true,
      }))
    await request.then((result) => {
      if (!result || !isCurrent()) return
      const latest = result.items.map((conversation) => {
        const info = userInfoCacheRef.current[conversation.otherUserId]
        if (!info) return conversation
        return { ...conversation,
          otherUserAvatar: conversation.otherUserAvatar || info.avatar,
          otherUserName: betterNick(conversation.otherUserName, info.nick || '') }
      })
      const cached = convsCacheRef.current[accountId]
      const next: ConversationCache = {
        convs: append || compensating ? mergeConversations(latest, cached?.convs ?? []) : latest,
        hasMore: result.hasMore, cursor: result.cursor,
      }
      // 套用已读水位：服务端 redPoint 从未被清过，不套水位的话重拉一次列表，
      // 已经看过、红点已消的会话会按服务端旧未读重新亮起来
      next.convs = applyReadMarks(accountId, next.convs)
      convsCacheRef.current[accountId] = next
      if (result.complete) recoveryRef.current.conversations.delete(accountId)
      else if (boundary) boundary.nextCursor = result.cursor
      if (isActive()) {
        conversationsRef.current = next.convs
        convCursorRef.current = next.cursor
        setConversations(next.convs)
        setConvHasMore(next.hasMore)
        setConvCursor(next.cursor)
        // 本地留一份：闲鱼限流 / 刷新页面时先拿它顶上，不至于白屏
        if (!append) writeConvCache(accountId, next.convs)
        // 会话列表的「有订单」角标：随列表一起刷新（本地库查询，开销极小）
        void loadOrderMarks(accountId)
      }
      if (!result.complete) {
        addToast({ message: `账号 ${accountId} 会话较多，请点击加载更多继续补齐`, type: 'warning' })
      }
      if (sawRateLimited) {
        // 账号正被闲鱼限流：本地也记 5 分钟冷却，停掉对账轮询 / 读状态刷新对
        // 这个账号的请求（后端冷却期内只会回缓存，不会真的打闲鱼，但没必要空转）
        rateLimitUntilRef.current[accountId] = Date.now() + 300_000
        // 给用户说明一次「为什么刷新没有新数据」：同账号 5 分钟内不重复弹，
        // 避免切账号 / 补偿轮询每轮都弹。文案点明是闲鱼侧限流、非系统故障。
        const now = Date.now()
        if (isActive() && (rateLimitedNoticeAtRef.current[accountId] ?? 0) + 300_000 < now) {
          rateLimitedNoticeAtRef.current[accountId] = now
          addToast({
            message: `账号 ${accountId} 正被闲鱼限流，列表显示的是上次的数据，稍后会自动恢复`,
            type: 'info',
          })
        }
      } else {
        // 拉取成功：提前解除本地冷却，不用干等 5 分钟整
        delete rateLimitUntilRef.current[accountId]
      }
      if (!append && isActive() && latest[0]?.cid) {
        getAccountProfile(accountId, latest[0].cid).then((profile) => {
          if (profile.nick) setAccounts((prev) => prev.map((account) => (
            account.account_id === accountId ? { ...account, display_name: profile.nick } : account
          )))
        }).catch((error: Error) => addToast({ message: error.message, type: 'error' }))
      }
    }).catch((error: Error) => {
      // 闲鱼流控：记 5 分钟冷却（对账轮询/读状态刷新都会跳过这个账号），
      // 手头一个会话都没有时用本地缓存顶上，别让界面空白
      if (String(error?.message || '').includes('请求过于频繁')) {
        rateLimitUntilRef.current[accountId] = Date.now() + 300_000
        if (isActive() && conversationsRef.current.length === 0) {
          const local = readConvCache(accountId)
          if (local.length) {
            conversationsRef.current = local
            setConversations(local)
            // 限流兜底的列表同样要有订单角标（本地库查询，不碰闲鱼接口）
            void loadOrderMarks(accountId)
          }
        }
      }
      // 已经有列表可显示（本账号内存缓存，或当前账号屏幕上的数据）时不再弹红色
      // 错误：用户视角没有任何异常，只是这份数据稍旧，后端稍后会自动补拉；
      // 真的一屏空白才提示。避免限流期间反复弹红错吓人——后端 Redis 兜底缓存
      // 也可能刚好过期（TTL 只对新写入生效），那时前端本地缓存是唯一可见数据。
      const hasDataToShow =
        (convsCacheRef.current[accountId]?.convs?.length ?? 0) > 0 ||
        (isActive() && conversationsRef.current.length > 0)
      if (isCurrent() && !hasDataToShow) {
        addToast({ message: error.message, type: 'error' })
      }
    }).finally(() => {
      if (!append) {
        const left = (convInflightRef.current.get(accountId) ?? 1) - 1
        if (left > 0) convInflightRef.current.set(accountId, left)
        else convInflightRef.current.delete(accountId)
      }
      if (requestId === convRequestIdRef.current[accountId] && isActive()) setLoadingConvs(false)
    })
  }, [addToast, applyReadMarks, loadOrderMarks])

  // 用 ref 保持 accounts 最新引用，避免 effect 闭包取到旧值
  const accountsRef = useRef(accounts)
  useEffect(() => { accountsRef.current = accounts }, [accounts])

  // ==================== 加载用户信息（头像+昵称） ====================
  // 找出缺少头像或昵称的会话，构建 [{userId, cid}] 查询列表
  const missingInfoConvs = conversations.filter(
    (c) => {
      if (!c.otherUserId || !c.cid) return false
      // 昵称有效（非空且非纯数字）且头像存在时无需查询。
      // 注意：这里**不要**把打码名（樱***1）也当成无效——那会让每个会话都去
      // 触发批量头像查询，一屏十几个会话就是十几个接口请求，很容易把账号
      // 顶进闲鱼风控（实测会话列表接口会被连带流控）。昵称的完整化改由
      // 「打开会话时用消息里的完整昵称升级」，不额外发请求。
      const hasValidName = !!c.otherUserName && !isPureDigits(c.otherUserName)
      if (c.otherUserAvatar && hasValidName) return false
      // 缓存中已有完整信息则跳过
      const cached = userInfoCacheRef.current[c.otherUserId]
      if (cached && cached.avatar && cached.nick) return false
      // 最近查询失败过（多半是接口被风控），冷却期内不重试
      const failedAt = userInfoFailAtRef.current[c.otherUserId] || 0
      if (Date.now() - failedAt < 5 * 60 * 1000) return false
      return true
    },
  )
  // 以 userId 去重后构建依赖 key
  const missingInfoKey = [...new Set(missingInfoConvs.map((c) => c.otherUserId))].sort().join(',')

  // 会话列表搜索：按用户名 / 用户ID / 最后一条消息 / 商品标题本地过滤（不区分大小写）
  const keyword = convSearch.trim().toLowerCase()
  const visibleConvs = keyword
    ? conversations.filter((c) => {
        const haystacks = [c.otherUserName, c.otherUserId, c.lastMessageSummary, c.itemTitle]
        return haystacks.some((s) => (s || '').toLowerCase().includes(keyword))
      })
    : conversations

  useEffect(() => {
    if (!activeAccountId || !missingInfoKey) return

    // 以 userId 去重后构建查询参数
    const seen = new Set<string>()
    const queries: { userId: string; cid: string }[] = []
    for (const c of missingInfoConvs) {
      if (!seen.has(c.otherUserId)) {
        seen.add(c.otherUserId)
        queries.push({ userId: c.otherUserId, cid: c.cid })
      }
    }
    if (queries.length === 0) return

    // 分批查询（每批3个），每批完成立即更新UI
    const BATCH_SIZE = 3
    let cancelled = false

    const applyInfos = (infos: Record<string, { avatar: string; nick: string }>) => {
      for (const [uid, info] of Object.entries(infos)) {
        // 同时缓存 avatar 和 nick
        const prev = userInfoCacheRef.current[uid] || { avatar: '', nick: '' }
        userInfoCacheRef.current[uid] = {
          avatar: info.avatar || prev.avatar,
          nick: info.nick || prev.nick,
        }
      }
      setConversations((prev) =>
        prev.map((c) => {
          const info = infos[c.otherUserId]
          if (!info) return c
          const updates: Partial<Conversation> = {}
          if (info.avatar && !c.otherUserAvatar) updates.otherUserAvatar = info.avatar
          // 昵称为空/纯数字/打码（樱***1）时用 API 返回的昵称覆盖
          const nextNick = betterNick(c.otherUserName, info.nick || '')
          if (nextNick && nextNick !== c.otherUserName) updates.otherUserName = nextNick
          return Object.keys(updates).length > 0 ? { ...c, ...updates } : c
        }),
      )
    }

    ;(async () => {
      for (let i = 0; i < queries.length; i += BATCH_SIZE) {
        if (cancelled) break
        const batch = queries.slice(i, i + BATCH_SIZE)
        try {
          const infos = await queryUserInfos(activeAccountId, batch)
          // 记录失败记忆：批次里没拿到信息的用户进入 5 分钟冷却期；
          // 拿到了则清掉旧记忆，允许以后刷新
          const now = Date.now()
          for (const q of batch) {
            if (infos && infos[q.userId]) delete userInfoFailAtRef.current[q.userId]
            else userInfoFailAtRef.current[q.userId] = now
          }
          if (!cancelled && infos && Object.keys(infos).length > 0) {
            applyInfos(infos)
          }
        } catch {
          // 单批失败不影响后续批次；本批用户也记入冷却期，避免立刻重试
          const now = Date.now()
          for (const q of batch) userInfoFailAtRef.current[q.userId] = now
        }
      }
    })()

    return () => { cancelled = true }
  }, [activeAccountId, missingInfoKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // ==================== 加载聊天记录 ====================
  const catchUpActiveConversation = useCallback(async (accountId: string, cid: string) => {
    if (accountId !== activeAccountIdRef.current || cid !== activeCidRef.current) return
    const requestId = ++msgRequestIdRef.current
    const generation = recoveryRef.current.generation(accountId)
    const isCurrent = () => requestId === msgRequestIdRef.current &&
      generation === recoveryRef.current.generation(accountId) &&
      accountId === activeAccountIdRef.current && cid === activeCidRef.current
    const cached = msgsCacheRef.current[accountId]?.[cid] ?? {
      msgs: messagesRef.current, hasMore: historyPageRef.current.msgHasMore, cursor: msgCursorRef.current,
    }
    const boundary = recoveryRef.current.ensureMessage(accountId, cid, cached)
    setLoadingMsgs(true)
    await recoverMessages(async (cursor, limit) => {
      const page = await getMessages(accountId, cid, cursor, limit)
      return { items: page.messages, hasMore: page.hasMore, nextCursor: page.nextCursor }
    }, boundary, isCurrent).then((result) => {
      if (!result || !isCurrent()) return
      const current = msgsCacheRef.current[accountId]?.[cid]?.msgs ?? messagesRef.current
      const next: MessageCache = { msgs: mergeMessages(result.items, current),
        hasMore: result.hasMore, cursor: result.cursor }
      msgsCacheRef.current[accountId] ??= {}
      msgsCacheRef.current[accountId][cid] = next
      if (result.complete) recoveryRef.current.messages.get(accountId)?.delete(cid)
      else boundary.nextCursor = result.cursor
      messagesRef.current = next.msgs
      msgCursorRef.current = next.cursor
      setMessages(next.msgs)
      setMsgHasMore(next.hasMore)
      setMsgCursor(next.cursor)
      // 消息里的 senderName 是完整昵称，列表/顶部的打码名（樱***1）顺手升级
      upgradeConvNickFromMsgs(accountId, cid, next.msgs)
      if (!result.complete) {
        addToast({ message: '断线期间消息较多，请点击加载更多继续补齐', type: 'warning' })
      }
    }).catch((error: Error) => {
      if (isCurrent()) addToast({ message: error.message, type: 'error' })
    }).finally(() => {
      if (requestId === msgRequestIdRef.current) setLoadingMsgs(false)
    })
  }, [addToast, upgradeConvNickFromMsgs])

  const loadMessages = useCallback(async (accountId: string, cid: string, append = false, silent = false) => {
    if (!accountId || !cid) return
    if (recoveryRef.current.message(accountId, cid)) {
      await catchUpActiveConversation(accountId, cid)
      return
    }
    const requestId = ++msgRequestIdRef.current
    const generation = recoveryRef.current.generation(accountId)
    const isCurrent = () => requestId === msgRequestIdRef.current &&
      generation === recoveryRef.current.generation(accountId) &&
      accountId === activeAccountIdRef.current && cid === activeCidRef.current
    // silent 供定时兜底拉取使用：不闪 loading、失败不弹 toast
    if (!silent) setLoadingMsgs(true)
    await getMessages(accountId, cid, append ? msgCursorRef.current ?? undefined : undefined)
      .then((result) => {
        if (!isCurrent()) return
        const current = msgsCacheRef.current[accountId]?.[cid]?.msgs ?? messagesRef.current
        const next = { msgs: mergeMessages(result.messages, current),
          hasMore: result.hasMore, cursor: result.nextCursor }
        msgsCacheRef.current[accountId] ??= {}
        msgsCacheRef.current[accountId][cid] = next
        messagesRef.current = next.msgs
        msgCursorRef.current = next.cursor
        setMessages(next.msgs)
        setMsgHasMore(next.hasMore)
        setMsgCursor(next.cursor)
        // 消息里的 senderName 是完整昵称，列表/顶部的打码名（樱***1）顺手升级
        upgradeConvNickFromMsgs(accountId, cid, next.msgs)
        // 用刚拉到的消息回写会话列表条目：会话列表接口被闲鱼限流时会停更，
        // 而消息接口是另一条通道仍然通畅，不回写就会「窗口 23:42、列表 12:56」
        syncConvFromMessages(accountId, cid, next.msgs)
        // 正在看的消息拉取成功 = 已读：顺手清掉该会话红点，
        // 同时兜住「推送误标的红点」和「服务端旧未读」两类残留
        if (accountId === activeAccountIdRef.current && normCid(cid) === normCid(activeCidRef.current)) {
          clearConvUnread(accountId, cid)
        }
      }).catch((error: Error) => {
        if (!silent && isCurrent()) addToast({ message: error.message, type: 'error' })
      }).finally(() => {
        if (!silent && requestId === msgRequestIdRef.current) setLoadingMsgs(false)
      })
  }, [addToast, catchUpActiveConversation, clearConvUnread, upgradeConvNickFromMsgs])

  // 边界已在断线时冻结，等待会话补偿期间收到的实时消息不会推进消息补偿终点。
  const reloadCurrentChat = useCallback(async (accountId: string) => {
    await loadConversations(accountId, false, true)
    if (accountId !== activeAccountIdRef.current) return
    const cid = activeCidRef.current
    if (cid) await catchUpActiveConversation(accountId, cid)
  }, [catchUpActiveConversation, loadConversations])

  useEffect(() => {
    reloadCurrentChatRef.current = reloadCurrentChat
    catchUpConvRef.current = catchUpActiveConversation
  }, [reloadCurrentChat, catchUpActiveConversation])

  // 推送缺失兜底：闲鱼不给网页 IM 稳定推送对方消息（服务端行为，实测长期如此，
  // 见提交 67e6348 的排查）。推送和断线补偿任何一环没接到，聊天窗口就会一直
  // 停在旧消息上。这里给当前打开的会话定时增量拉最新一页兜底，消息按 ID 去重，
  // 重复拉取无害。静默模式：不闪 loading、失败不弹 toast。
  useEffect(() => {
    if (!activeAccountId || !activeCid) return
    const timer = setInterval(() => {
      // 页面切到后台时没必要拉，回来后下一轮自然补上
      if (document.hidden) return
      void loadMessages(activeAccountId, activeCid, false, true)
    }, 15000)
    return () => clearInterval(timer)
  }, [activeAccountId, activeCid, loadMessages])

  /** 账号和会话切换共用恢复入口，后台缓存待补偿时立即拉取缺失消息。 */
  const restoreConversation = useCallback((accountId: string, cid: string) => {
    msgRequestIdRef.current += 1
    const cached = msgsCacheRef.current[accountId]?.[cid]
    const restored = cached ?? { msgs: [], hasMore: false, cursor: null }
    messagesRef.current = restored.msgs
    msgCursorRef.current = restored.cursor
    setMessages(restored.msgs)
    setMsgHasMore(restored.hasMore)
    setMsgCursor(restored.cursor)
    setLoadingMsgs(false)
    if (cid && (!cached || recoveryRef.current.message(accountId, cid))) {
      void loadMessages(accountId, cid)
    }
  }, [loadMessages])

  // 选中账号时恢复缓存；离线期间产生的缺口通过同一补偿入口补齐。
  useEffect(() => {
    const accountId = activeAccountId
    const account = accountsRef.current.find((item) => item.account_id === accountId)
    if (!accountId || !account?.connected) {
      setConversations([])
      selectCid('')
      restoreConversation(accountId, '')
      return
    }
    const cached = convsCacheRef.current[accountId]
    conversationsRef.current = cached?.convs ?? []
    convCursorRef.current = cached?.cursor ?? null
    setConversations(cached?.convs ?? [])
    setConvHasMore(cached?.hasMore ?? false)
    setConvCursor(cached?.cursor ?? null)
    // 换账号先清掉上一个账号的订单标记，防止串号残留
    setOrderMarks({})
    if (!cached || recoveryRef.current.conversations.has(accountId)) {
      void loadConversations(accountId, false, !!cached)
    } else {
      // 命中缓存不重拉列表时也要刷订单标记，否则角标停留在上一次的状态
      void loadOrderMarks(accountId)
    }
    const cid = activeConvPerAccountRef.current[accountId] || ''
    selectCid(cid)
    restoreConversation(accountId, cid)
    // 自动恢复打开的会话对用户可见 = 已读，记水位防服务端旧未读复活
    if (cid) markConvReadByKey(accountId, cid)
  }, [activeAccountId]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSelectConversation = (cid: string) => {
    selectCid(cid)
    setMobileTab('chat')
    clearConvUnread(activeAccountId, cid)
    restoreConversation(activeAccountId, cid)
  }

  // 删除会话：只从本系统的会话列表移除（后端记删除时刻），
  // 之后对方再来消息（lastMessageTime 晚于删除时刻）会自动重新出现。
  const handleDeleteConversation = async (conv: Conversation) => {
    if (!activeAccountId || deletingConvCid) return
    const name = conv.otherUserName || conv.otherUserId || '该用户'
    const confirmed = await requestConfirm({
      title: '删除会话',
      message: `删除与「${name}」的会话？只从列表移除，聊天记录还在闲鱼上；对方再来消息会重新出现。`,
      confirmText: '删除',
      type: 'danger',
    })
    if (!confirmed) return
    setDeletingConvCid(conv.cid)
    try {
      await hideConversation(activeAccountId, conv.cid)
      const drop = (list: Conversation[]) => list.filter((c) => c.cid !== conv.cid)
      const nextConvs = drop(conversationsRef.current)
      conversationsRef.current = nextConvs
      setConversations(nextConvs)
      // 同步清理各层缓存与补偿边界，避免下次合并时又冒出来
      const cache = convsCacheRef.current[activeAccountId]
      if (cache) cache.convs = drop(cache.convs)
      const boundary = recoveryRef.current.conversations.get(activeAccountId)
      if (boundary) boundary.cache.convs = drop(boundary.cache.convs)
      if (msgsCacheRef.current[activeAccountId]) {
        delete msgsCacheRef.current[activeAccountId][conv.cid]
      }
      recoveryRef.current.messages.get(activeAccountId)?.delete(conv.cid)
      // 删除的是当前打开的会话：清空右侧窗口
      if (activeCidRef.current === conv.cid) {
        activeConvPerAccountRef.current[activeAccountId] = ''
        selectCid('')
        messagesRef.current = []
        setMessages([])
        setMsgHasMore(false)
        setMsgCursor(null)
        msgCursorRef.current = null
        setMobileTab('convs')
      }
      addToast({ message: '会话已删除', type: 'success' })
    } catch (e: any) {
      addToast({ message: e?.message || '删除会话失败', type: 'error' })
    } finally {
      setDeletingConvCid('')
    }
  }

  const activeConversation = conversations.find((c) => c.cid === activeCid)

  // 查当前会话关联商品的价格（图/标题会话里就有，价格要额外查闲鱼详情接口，后端缓存10分钟）
  useEffect(() => {
    const itemId = activeConversation?.itemId
    if (!activeAccountId || !itemId) {
      setItemInfo(null)
      return
    }
    let cancelled = false
    getItemInfo(activeAccountId, itemId).then((info) => {
      if (!cancelled) setItemInfo(info)
    })
    return () => {
      cancelled = true
    }
  }, [activeAccountId, activeConversation?.itemId])

  // 对方闲鱼主页信息（会话窗口的"买家名片"）：等级/好评率/属地/粉丝/认证
  // 后端按用户ID缓存 30 分钟；查不到时静默不展示，不打扰聊天
  useEffect(() => {
    const peerId = activeConversation?.otherUserId
    if (!activeAccountId || !peerId) {
      setPeerProfile(null)
      return
    }
    let cancelled = false
    setPeerProfile(null)
    getXianyuUserProfile(activeAccountId, peerId).then((profile) => {
      if (!cancelled) setPeerProfile(profile)
    })
    return () => {
      cancelled = true
    }
  }, [activeAccountId, activeConversation?.otherUserId])

  // 打开闲鱼个人主页弹窗（昵称和头像直接用系统内已有的数据）
  const openXianyuProfile = useCallback((userId: string, name: string, avatar: string) => {
    if (!userId) return
    setProfileModal({ userId, name, avatar })
  }, [])

  // 取一个会话ID，用于查询当前账号（卖家）自己的头像
  const firstConvCid = conversations.find((c) => c.cid)?.cid || ''

  // 获取当前账号自己的头像（消息气泡右侧显示），拿到后不再重复请求
  useEffect(() => {
    if (!activeAccountId || !firstConvCid) return
    if (selfAvatars[activeAccountId]) return
    let cancelled = false
    getAccountProfile(activeAccountId, firstConvCid)
      .then((profile) => {
        if (cancelled) return
        if (profile.avatar) {
          setSelfAvatars((prev) => ({ ...prev, [activeAccountId]: profile.avatar }))
        }
        if (profile.nick) {
          setAccounts((prev) => prev.map((account) => (
            account.account_id === activeAccountId ? { ...account, display_name: profile.nick } : account
          )))
        }
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [activeAccountId, firstConvCid, selfAvatars])

  useEffect(() => {
    let cancelled = false
    setIsOfficiallyBlocked(false)
    if (!activeAccountId || !activeCid) return
    getOfficialBlacklistStatus(activeAccountId, activeCid)
      .then((blocked) => { if (!cancelled) setIsOfficiallyBlocked(blocked) })
      .catch(() => { if (!cancelled) setIsOfficiallyBlocked(false) })
    return () => { cancelled = true }
  }, [activeAccountId, activeCid])

  const loadCustomerOrders = useCallback(async (silent = false) => {
    if (!activeAccountId || !activeConversation?.otherUserId) {
      setCustomerOrders([])
      return
    }
    if (!silent) setLoadingOrders(true)
    try {
      const data = await getCustomerOrders(activeAccountId, activeConversation.otherUserId, activeCid)
      setCustomerOrders(data)
    } catch (e: any) {
      addToast({ message: e.message || '获取客户订单失败', type: 'error' })
    } finally {
      if (!silent) setLoadingOrders(false)
    }
  }, [activeAccountId, activeCid, activeConversation?.otherUserId, addToast])

  useEffect(() => {
    loadCustomerOrders()
  }, [loadCustomerOrders])

  useEffect(() => {
    reloadOrdersRef.current = () => { void loadCustomerOrders(true) }
  }, [loadCustomerOrders])

  useEffect(() => {
    if (!activeCid) return
    const timer = window.setInterval(() => { void loadCustomerOrders(true) }, 15000)
    return () => window.clearInterval(timer)
  }, [activeCid, loadCustomerOrders])

  const loadQuickPhrases = useCallback(async () => {
    try {
      setQuickPhrases(await getQuickPhrases())
    } catch (e: any) {
      addToast({ message: e.message || '获取快捷短语失败', type: 'error' })
    }
  }, [addToast])

  useEffect(() => {
    loadQuickPhrases()
  }, [loadQuickPhrases])

  const resetPhraseForm = () => {
    setEditingPhraseId(null)
    setPhraseTitle('')
    setPhraseContent('')
  }

  const handleSavePhrase = async () => {
    if (!phraseTitle.trim() || !phraseContent.trim() || savingPhrase) return
    setSavingPhrase(true)
    try {
      const payload = { title: phraseTitle.trim(), content: phraseContent.trim(), sort_order: 0 }
      if (editingPhraseId) {
        await updateQuickPhrase(editingPhraseId, payload)
      } else {
        await createQuickPhrase(payload)
      }
      resetPhraseForm()
      await loadQuickPhrases()
      addToast({ message: editingPhraseId ? '快捷短语已更新' : '快捷短语已添加', type: 'success' })
    } catch (e: any) {
      addToast({ message: e.message || '保存快捷短语失败', type: 'error' })
    } finally {
      setSavingPhrase(false)
    }
  }

  const handleDeletePhrase = async (id: number) => {
    if (!(await requestConfirm({ message: '确认删除这条快捷短语吗？', confirmText: '删除', type: 'danger' }))) return
    try {
      await deleteQuickPhrase(id)
      if (editingPhraseId === id) resetPhraseForm()
      await loadQuickPhrases()
    } catch (e: any) {
      addToast({ message: e.message || '删除快捷短语失败', type: 'error' })
    }
  }

  const handleSyncOrders = async () => {
    if (!activeAccountId || loadingOrders) return
    setLoadingOrders(true)
    try {
      const res = await fetchXianyuOrders(activeAccountId)
      addToast({ message: res.message || '订单同步完成', type: res.success ? 'success' : 'error' })
      await loadCustomerOrders()
    } catch (e: any) {
      addToast({ message: e.message || '同步订单失败', type: 'error' })
    } finally {
      setLoadingOrders(false)
    }
  }

  const handleDeliverOrder = async (orderNo: string) => {
    if (!(await requestConfirm({ message: `确认立即发货订单 ${orderNo} 吗？`, confirmText: '发货' }))) return
    setDeliveringOrderNo(orderNo)
    try {
      const res = await manualDelivery(orderNo)
      addToast({ message: res.message || (res.success ? '发货成功' : '发货失败'), type: res.success ? 'success' : 'error' })
      await loadCustomerOrders()
      // 订单状态变了，会话列表角标跟着刷一次
      if (activeAccountId) void loadOrderMarks(activeAccountId)
    } catch (e: any) {
      addToast({ message: e.message || '发货失败', type: 'error' })
    } finally {
      setDeliveringOrderNo('')
    }
  }


  // 消息变化时自动滚动到底部
  const handleNoLogisticsDelivery = async (orderNo: string) => {
    if (!(await requestConfirm({ message: `确认将订单 ${orderNo} 标记为无物流发货吗？`, confirmText: '无物流发货' }))) return
    setConfirmingOrderNo(orderNo)
    try {
      const res = await noLogisticsDelivery(orderNo)
      addToast({ message: res.message || (res.success ? '无物流发货成功' : '无物流发货失败'), type: res.success ? 'success' : 'error' })
      await loadCustomerOrders(true)
      if (activeAccountId) void loadOrderMarks(activeAccountId)
    } catch (e: any) {
      addToast({ message: e.message || '无物流发货失败', type: 'error' })
    } finally {
      setConfirmingOrderNo('')
    }
  }

  // 打开订单改价弹窗（currentPrice 为会话订单里的当前总价，可能为空）
  const openAdjustPriceModal = (orderNo: string, currentPrice?: string) => {
    setAdjustingOrderNo(orderNo)
    setAdjustPriceInput(currentPrice || '')
  }

  // 打开闲鱼 APP 深链弹窗（演出信息表单等 fleamarket:// 原生页面）
  const openDeepLinkModal = (url: string, title: string) => {
    setDeepLinkCopied(false)
    setDeepLink({ url, title })
  }

  // 复制深链到剪贴板：Preferred clipboard API 在非安全上下文不可用（http 局域网访问），
  // 退化到 execCommand 选中文本框再复制，保证局域网/反代部署时也能用
  const copyDeepLink = async () => {
    if (!deepLink) return
    const text = deepLink.url
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const el = document.getElementById('deeplink-input') as HTMLInputElement | null
        if (el) {
          el.select()
          document.execCommand('copy')
        }
      }
      setDeepLinkCopied(true)
      addToast({ message: '链接已复制，粘贴到手机浏览器/微信里打开即可唤起闲鱼 APP', type: 'success' })
    } catch {
      addToast({ message: '复制失败，请手动选中链接复制', type: 'warning' })
    }
  }

  // 确认改价：走与发货/取消同一套 websocket 内部链路调闲鱼改价接口
  const handleAdjustOrderPrice = async (orderNo: string, priceText: string) => {
    const price = priceText.trim()
    if (!price || isNaN(Number(price)) || Number(price) <= 0) {
      addToast({ message: '请输入正确的价格', type: 'error' })
      return
    }
    if (!(await requestConfirm({
      message: `确认将订单 ${orderNo} 的价格修改为 ¥${price} 吗？改价后买家会收到通知。`,
      confirmText: '确认改价',
    }))) return
    setAdjustingPrice(true)
    try {
      const res = await adjustOrderPrice(orderNo, price)
      addToast({ message: res.message || (res.success ? '改价成功' : '改价失败'), type: res.success ? 'success' : 'error' })
      if (res.success) {
        setAdjustingOrderNo('')
        await loadCustomerOrders(true)
        // 订单金额变了，会话列表角标跟着刷一次
        if (activeAccountId) void loadOrderMarks(activeAccountId)
      }
    } catch (e: any) {
      addToast({ message: e.message || '改价失败', type: 'error' })
    } finally {
      setAdjustingPrice(false)
    }
  }

  const handleCancelOrder = async (orderNo: string) => {
    if (!(await requestConfirm({ message: `确认取消客户订单 ${orderNo} 吗？取消后无法恢复。`, confirmText: '取消订单', type: 'danger' }))) return
    setCancellingOrderNo(orderNo)
    try {
      const res = await cancelOrder(orderNo)
      addToast({ message: res.message || (res.success ? '订单已取消' : '取消订单失败'), type: res.success ? 'success' : 'error' })
      await loadCustomerOrders(true)
      // 取消后该买家可能没有进行中订单了，角标同步刷掉
      if (activeAccountId) void loadOrderMarks(activeAccountId)
    } catch (e: any) {
      addToast({ message: e.message || '取消订单失败', type: 'error' })
    } finally {
      setCancellingOrderNo('')
    }
  }

  const handleViewOrderDetail = async (orderNo: string) => {
    setLoadingOrderDetail(true)
    try {
      const res = await getOrderDetail(orderNo, true)
      setOrderDetail(res.data)
      await loadCustomerOrders(true)
    } catch (e: any) {
      addToast({ message: e.message || '获取订单详情失败', type: 'error' })
    } finally {
      setLoadingOrderDetail(false)
    }
  }

  const handleBlacklistCustomer = async () => {
    if (!activeConversation || !activeAccountId || blacklisting) return
    const action = isOfficiallyBlocked ? 'remove' : 'add'
    const label = isOfficiallyBlocked ? '解除黑名单' : '加入黑名单'
    if (!(await requestConfirm({
      title: `确认${label}`,
      message: `确认在闲鱼官方${label}客户 ${activeConversation.otherUserName || activeConversation.otherUserId} 吗？`,
      confirmText: label,
      type: isOfficiallyBlocked ? 'warning' : 'danger',
    }))) return
    setBlacklisting(true)
    try {
      const res = await changeOfficialBlacklist(activeAccountId, activeConversation.cid, action)
      if (!res.success) throw new Error(res.message || '黑名单操作失败')
      setIsOfficiallyBlocked(action === 'add')
      addToast({ message: res.message || `${label}成功`, type: 'success' })
    } catch (e: any) {
      addToast({ message: e.message || '加入黑名单失败', type: 'error' })
    } finally {
      setBlacklisting(false)
    }
  }

  /** 右键图片菜单里的「复制图片」：走后端代理拿同源数据后写入剪贴板 */
  const handleCopyImage = async (url: string) => {
    setImageMenu(null)
    if (!url || copyingImageUrl) return
    setCopyingImageUrl(url)
    try {
      const res = await copyImageToClipboard(url)
      addToast({ message: res.message, type: 'success' })
    } catch (e: any) {
      addToast({ message: e.message || '复制图片失败', type: 'error' })
    } finally {
      setCopyingImageUrl('')
    }
  }

  /** 图片右键菜单：点别处、滚动、按 Esc 或改窗口大小时关闭 */
  useEffect(() => {
    if (!imageMenu) return
    const close = () => setImageMenu(null)
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setImageMenu(null)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('scroll', close, true)
    document.addEventListener('keydown', onKeyDown)
    window.addEventListener('resize', close)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('scroll', close, true)
      document.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('resize', close)
    }
  }, [imageMenu])

  /** 图片预览大图：按 Esc 或点空白处关闭 */
  useEffect(() => {
    if (!previewImage) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setPreviewImage('')
        // 预览图上开的右键菜单跟着一起关，避免 Esc 后留个孤立菜单
        setImageMenu(null)
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [previewImage])

  const handleRecallMessage = async (msg: ChatMessage) => {
    if (!activeAccountId || !msg.messageId || recallingMessageId) return
    if (!canRecallMessage(msg)) {
      addToast({ message: '消息发送超过两分钟，无法撤回', type: 'error' })
      return
    }
    if (!(await requestConfirm({
      title: '撤回消息',
      message: '确认撤回这条消息吗？撤回仅支持发送后两分钟内操作。',
      confirmText: '撤回',
      type: 'danger',
    }))) return
    setRecallingMessageId(msg.messageId)
    try {
      const res = await recallMessage(activeAccountId, msg.messageId, msg.time)
      if (!res.success) throw new Error(res.message || '撤回失败')
      setMessages((prev) => prev.map((item) => item.messageId === msg.messageId
        ? { ...item, type: 'system', text: '你撤回了一条消息', images: [] }
        : item))
      addToast({ message: res.message || '消息已撤回', type: 'success' })
    } catch (e: any) {
      addToast({ message: e.message || '撤回失败', type: 'error' })
    } finally {
      setRecallingMessageId('')
    }
  }

  const prevMsgCountRef = useRef(0)
  useEffect(() => {
    if (!msgContainerRef.current) return
    const container = msgContainerRef.current
    // 只有新增消息时才滚动（而不是加载历史）
    if (messages.length > prevMsgCountRef.current || prevMsgCountRef.current === 0) {
      container.scrollTop = container.scrollHeight
    }
    prevMsgCountRef.current = messages.length
  }, [messages])

  // 每 20 秒静默刷新当前会话的读状态：只按 messageId 更新 readStatus，不重排 UI，
  // 让自己发出的消息「未读 → 已读」能及时变化（闲鱼不会主动推送读状态变更）。
  useEffect(() => {
    if (!activeAccountId || !activeCid) return
    let cancelled = false
    const refresh = async () => {
      // 账号处于流控冷却期：读状态刷新也停掉，别给已经限流的账号再加压
      if ((rateLimitUntilRef.current[activeAccountId] ?? 0) > Date.now()) return
      try {
        const res = await getMessages(activeAccountId, activeCid, undefined, 20)
        if (cancelled) return
        // getMessages 直接返回 { messages, hasMore, nextCursor }，不包 success/data
        const fresh = res.messages || []
        setMessages((prev) => {
          if (!prev.length) return prev
          const statusMap = new Map(fresh.map((m) => [m.messageId, m.readStatus]))
          return prev.map((m) => {
            const s = statusMap.get(m.messageId)
            return s === undefined || s === m.readStatus ? m : { ...m, readStatus: s }
          })
        })
      } catch {
        // 静默失败，等下一轮
      }
    }
    const timer = window.setInterval(refresh, 20000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [activeAccountId, activeCid])

  // 断线兜底对账轮询：每 30 秒对「当前在线」的账号拉一次会话列表。
  // 消息实时性由 App 通道推送保证，这里只是兜底（推送丢了/断线期间漏了），
  // 间隔从 15 秒放宽到 30 秒——15 秒 × 多账号会把闲鱼的会话列表接口顶进
  // 流控（400600001），而它一旦被限流要很久才恢复，代价远大于晚 15 秒。
  // 发现某会话有新动静时做两件事：① 把服务端最新会话数据回写列表（摘要+红点
  // ≤30s 自动出现，不再等手动刷新）；② 若正是当前打开的会话，立即补拉消息进窗口。
  // 用服务端未读数(unreadCount)比对，把断线期间漏掉的新消息补响 + 未读角标。
  // 断线期间拉不到（接口要求在线），等账号被自动恢复看门狗拉起来后的下一轮补响——
  // 这就是"断线也响"的兜底：可恢复的掉线最终都会响，只有 Cookie 真失效才不响。
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      if (cancelled) return
      const now = Date.now()
      const connectedIds = accountsRef.current.filter((a) => a.connected).map((a) => a.account_id)
      let fetchedAny = false
      for (const accountId of connectedIds) {
        if ((rateLimitUntilRef.current[accountId] ?? 0) > now) continue
        try {
          const { conversations, rateLimited } = await getConversations(accountId)
          if (cancelled) return
          fetchedAny = true
          // 后端限流冷却期内会回缓存并带 rateLimited：本地同步记冷却，
          // 后续轮次跳过该账号；成功一次就提前解除，不等整 5 分钟
          if (rateLimited) {
            rateLimitUntilRef.current[accountId] = Date.now() + 300_000
          } else {
            delete rateLimitUntilRef.current[accountId]
          }
          for (const conv of conversations) {
            // 先于响铃水位判断「有没有新动静」：有则把列表摘要/红点一并回写，
            // 正在看的会话立即补拉消息——推送缺失时不再只响铃不动界面
            const isNew = conv.lastMessageTime >
              (rungConvRef.current[rungKey(accountId, conv.cid)] ?? 0) + RING_TOLERANCE_MS
            if (isNew && baselineDoneRef.current) {
              const isViewingConv = accountId === activeAccountIdRef.current &&
                normCid(conv.cid) === normCid(activeCidRef.current)
              syncConvFromReconcile(accountId, conv)
              if (isViewingConv) void catchUpConvRef.current(accountId, conv.cid)
            }
            ringMissedIfAny(accountId, conv.cid, conv.lastMessageTime, conv.unreadCount)
          }
        } catch (e) {
          // 闲鱼流控：该账号冷却 5 分钟（与后端一致），期间不再请求
          if (String((e as Error)?.message || '').includes('请求过于频繁')) {
            rateLimitUntilRef.current[accountId] = Date.now() + 300_000
          }
          // 其余失败静默，等下一轮
        }
      }
      // 首轮整体对账完成：至少有一个账号成功拉取过（水位已校准），之后才允许响铃。
      // 仅当真正校准过水位才置位，避免首轮全失败时把旧未读误响；置位后断开后恢复
      // 连接的账号也能在后续轮次补响（覆盖用户所述"断开期间有人发消息"的场景）。
      if (fetchedAny) baselineDoneRef.current = true
    }
    const timer = window.setInterval(tick, 30000)
    void tick()
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  // ==================== 发送消息 ====================
  const sendMessageText = async (rawText: string, clearInput = false) => {
    if (!rawText.trim() || !activeAccountId || !activeCid || sending) return

    // 获取当前会话的对方用户ID
    const conv = conversations.find((c) => c.cid === activeCid)
    if (!conv) {
      addToast({ message: '未找到当前会话信息', type: 'error' })
      return
    }

    const text = rawText.trim()
    setSending(true)
    try {
      const res = await sendTextMessage(activeAccountId, activeCid, conv.otherUserId, text)
      // 无论成功失败，都把这条消息展示在聊天记录中；
      // 失败时标记 failed + failReason，气泡前显示红色感叹号，点击查看原因
      const sentMsg: ChatMessage = {
        messageId: res.data?.messageId || '',
        senderId: activeAccountId,
        senderName: '',
        isSelf: true,
        type: 'text',
        text,
        images: [],
        time: Date.now(),
        failed: !res.success,
        failReason: res.success ? undefined : (res.message || '发送失败'),
      }
      if (clearInput) setInputText('')
      setMessages((prev) => appendMsg(prev, sentMsg))
      if (res.success) {
        // 成功才更新会话列表摘要
        setConversations((prev) =>
          prev.map((c) =>
            c.cid === activeCid
              ? { ...c, lastMessageSummary: `你：${text.slice(0, 50)}`, lastMessageTime: sentMsg.time }
              : c,
          ),
        )
      } else {
        addToast({ message: res.message || '发送失败', type: 'error' })
      }
    } catch (e: any) {
      // 网络等异常：同样以失败态展示该条消息
      const failReason = e?.message || '发送失败'
      const sentMsg: ChatMessage = {
        messageId: '',
        senderId: activeAccountId,
        senderName: '',
        isSelf: true,
        type: 'text',
        text,
        images: [],
        time: Date.now(),
        failed: true,
        failReason,
      }
      if (clearInput) setInputText('')
      setMessages((prev) => appendMsg(prev, sentMsg))
      addToast({ message: failReason, type: 'error' })
    } finally {
      setSending(false)
      // 从输入框发出的（clearInput=true），发完把光标还回输入框
      if (clearInput) refocusInput()
    }
  }

  // ==================== 发送图片 ====================
  const clearPendingImage = useCallback(() => {
    setPendingImage((prev) => {
      if (prev) URL.revokeObjectURL(prev.previewUrl)
      return null
    })
  }, [])

  useEffect(() => {
    pendingImageRef.current = pendingImage
  }, [pendingImage])

  useEffect(() => () => {
    if (pendingImageRef.current) URL.revokeObjectURL(pendingImageRef.current.previewUrl)
  }, [])

  useEffect(() => {
    clearPendingImage()
  }, [activeAccountId, activeCid, clearPendingImage])

  const getClipboardImageFileName = (type: string) => {
    const extMap: Record<string, string> = {
      'image/jpeg': 'jpg',
      'image/png': 'png',
      'image/webp': 'webp',
      'image/gif': 'gif',
    }
    return `clipboard-${Date.now()}.${extMap[type] || 'png'}`
  }

  const handlePickImage = () => {
    if (sending) return
    imageInputRef.current?.click()
  }

  const validateImageFile = (file: File) => {
    if (!file.type.startsWith('image/')) {
      addToast({ message: '请选择图片文件', type: 'error' })
      return false
    }
    if (file.size > 10 * 1024 * 1024) {
      addToast({ message: '图片大小不能超过10MB', type: 'error' })
      return false
    }
    return true
  }

  const sendImageFile = async (file: File) => {
    if (!file || !activeAccountId || !activeCid || sending) return false

    if (!validateImageFile(file)) return false

    const conv = conversations.find((c) => c.cid === activeCid)
    if (!conv) {
      addToast({ message: '未找到当前会话信息', type: 'error' })
      return false
    }

    setSending(true)
    try {
      const res = await sendImageMessage(activeAccountId, activeCid, conv.otherUserId, file)
      // 成功用CDN地址；失败则用本地预览地址，保证用户都能看到所发图片
      const displayUrl = res.success && res.data?.imageUrl ? res.data.imageUrl : URL.createObjectURL(file)
      // 无论成功失败，都把这条图片消息展示在聊天记录中
      const sentMsg: ChatMessage = {
        messageId: res.data?.messageId || '',
        senderId: activeAccountId,
        senderName: '',
        isSelf: true,
        type: 'image',
        text: '',
        images: [displayUrl],
        time: Date.now(),
        failed: !res.success,
        failReason: res.success ? undefined : (res.message || '发送失败'),
      }
      setMessages((prev) => appendMsg(prev, sentMsg))
      if (res.success) {
        setConversations((prev) =>
          prev.map((c) =>
            c.cid === activeCid
              ? { ...c, lastMessageSummary: '你：[图片]', lastMessageTime: sentMsg.time }
              : c,
          ),
        )
      } else {
        addToast({ message: res.message || '发送失败', type: 'error' })
      }
      return res.success
    } catch (e: any) {
      const failReason = e?.message || '发送失败'
      const displayUrl = URL.createObjectURL(file)
      const sentMsg: ChatMessage = {
        messageId: '',
        senderId: activeAccountId,
        senderName: '',
        isSelf: true,
        type: 'image',
        text: '',
        images: [displayUrl],
        time: Date.now(),
        failed: true,
        failReason,
      }
      setMessages((prev) => appendMsg(prev, sentMsg))
      addToast({ message: failReason, type: 'error' })
      return false
    } finally {
      setSending(false)
      refocusInput()
    }
  }

  // 发送语音：把录音 blob 交给后端（后端转 mp3 → 上传闲鱼CDN → 发 contentType=3 消息）
  const sendAudioBlob = async (blob: Blob, durationMs: number) => {
    if (!activeAccountId || !activeCid) {
      addToast({ message: '请先选中一个会话再发送', type: 'error' })
      return false
    }
    const conv = conversations.find((c) => c.cid === activeCid)
    if (!conv) {
      addToast({ message: '未找到当前会话信息', type: 'error' })
      return false
    }
    setSending(true)
    try {
      const res = await sendAudioMessage(
        activeAccountId,
        activeCid,
        conv.otherUserId,
        blob,
        durationMs,
      )
      // 闲鱼网页端上传通道只收图片，一旦确认不支持就记住，后续直接走转文字，不再白等
      if (!res.success && (res as any)?.data?.voiceUnsupported === true) {
        voiceUnsupportedRef.current = true
        setVoiceUnsupported(true)
      }
      const localUrl = URL.createObjectURL(blob)
      const sentMsg: ChatMessage = {
        messageId: res.data?.messageId || '',
        senderId: activeAccountId,
        senderName: '',
        isSelf: true,
        type: 'audio',
        text: '',
        images: [],
        audioUrl: (res.success && res.data?.audioUrl) ? res.data.audioUrl : localUrl,
        audioDuration: durationMs,
        time: Date.now(),
        failed: !res.success,
        failReason: res.success ? undefined : (res.message || '发送失败'),
      }
      setMessages((prev) => [...prev, sentMsg])
      if (res.success) {
        setConversations((prev) =>
          prev.map((c) =>
            c.cid === activeCid
              ? { ...c, lastMessageSummary: '你：[语音]', lastMessageTime: sentMsg.time }
              : c,
          ),
        )
      } else {
        addToast({ message: res.message || '发送失败', type: 'error' })
      }
      return res.success
    } catch (e: any) {
      const failReason = e?.message || '发送失败'
      const sentMsg: ChatMessage = {
        messageId: '',
        senderId: activeAccountId,
        senderName: '',
        isSelf: true,
        type: 'audio',
        text: '',
        images: [],
        audioUrl: URL.createObjectURL(blob),
        audioDuration: durationMs,
        time: Date.now(),
        failed: true,
        failReason,
      }
      setMessages((prev) => [...prev, sentMsg])
      addToast({ message: failReason, type: 'error' })
      return false
    } finally {
      setSending(false)
      refocusInput()
    }
  }

  const startRecording = async () => {
    if (sending || !activeCid) return
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      addToast({ message: '当前浏览器不支持录音（需 https 或 localhost）', type: 'error' })
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const pickMime = () => {
        for (const m of ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']) {
          if (MediaRecorder.isTypeSupported(m)) return m
        }
        return ''
      }
      const mime = pickMime()
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
      chunksRef.current = []
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data)
      }
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop())
        if (recTimerRef.current) {
          window.clearInterval(recTimerRef.current)
          recTimerRef.current = null
        }
        const durationMs = Date.now() - recStartTsRef.current
        setIsRecording(false)
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || mime || 'audio/webm' })
        if (durationMs < 800) {
          addToast({ message: '录音时间太短，请多说一会儿', type: 'error' })
          return
        }
        // 录完先停在「待发送」状态，由用户选择发送语音还是转成文字
        setPendingAudio((prev) => {
          if (prev) URL.revokeObjectURL(prev.url)
          return { blob, url: URL.createObjectURL(blob), durationMs }
        })
      }
      rec.start()
      mediaRecRef.current = rec
      recStartTsRef.current = Date.now()
      setIsRecording(true)
      setRecordSeconds(0)
      recTimerRef.current = window.setInterval(() => {
        setRecordSeconds(Math.floor((Date.now() - recStartTsRef.current) / 1000))
      }, 250)
    } catch (e: any) {
      addToast({ message: e?.message || '无法访问麦克风，请检查浏览器权限', type: 'error' })
    }
  }

  const stopRecording = () => {
    if (mediaRecRef.current && mediaRecRef.current.state !== 'inactive') {
      mediaRecRef.current.stop()
    }
  }

  const clearPendingAudio = () => {
    setPendingAudio((prev) => {
      if (prev) URL.revokeObjectURL(prev.url)
      return null
    })
  }

  /** 直接发送语音消息（走闲鱼语音上传通道） */
  const sendPendingAudio = async () => {
    if (!pendingAudio) return
    // 已经确认闲鱼网页端不支持发语音，直接转成文字，省掉一次必然失败的上传
    if (voiceUnsupportedRef.current) {
      await transcribePendingAudio({ fallback: true })
      return
    }
    const { blob, durationMs } = pendingAudio
    const ok = await sendAudioBlob(blob, durationMs)
    if (ok) {
      clearPendingAudio()
      return
    }
    // 闲鱼网页端的上传通道目前只收图片，发语音大概率被拒。
    // 失败后自动把这段录音转成文字填进输入框，保证一次点击一定发得出去。
    await transcribePendingAudio({ fallback: true })
  }

  /** 语音转文字：转写结果回填到输入框，用户确认后再当普通文本发出去 */
  const transcribePendingAudio = async (opts?: { fallback?: boolean }): Promise<boolean> => {
    if (!pendingAudio || !activeAccountId) return false
    setTranscribing(true)
    try {
      // 浏览器里先统一转成 16k/16bit 单声道 WAV，兼容各家 ASR 接口
      const wav = await blobToWav(pendingAudio.blob)
      const res = await transcribeAudio(activeAccountId, wav)
      if (!res.success) {
        addToast({ message: res.message || '转写失败', type: 'error' })
        return false
      }
      const text = (res.data?.text || '').trim()
      if (!text) {
        addToast({ message: '没听清，请再说一遍', type: 'error' })
        return false
      }
      setInputText((prev) => (prev.trim() ? `${prev.trim()} ${text}` : text))
      clearPendingAudio()
      refocusInput()
      addToast({
        message: opts?.fallback
          ? '闲鱼网页端暂不支持发语音，已自动转成文字，确认后发送'
          : '已转成文字，确认后点击发送',
        type: 'success',
      })
      return true
    } catch (e: any) {
      addToast({ message: e?.message || '转写失败', type: 'error' })
      return false
    } finally {
      setTranscribing(false)
    }
  }

  /** 把对方发来的语音消息转成文字 */
  const transcribeMessageAudio = async (msg: ChatMessage) => {
    if (!activeAccountId || !msg.audioUrl || !msg.messageId) return
    setTranscribingMsgId(msg.messageId)
    try {
      const res = await transcribeAudioUrl(activeAccountId, msg.audioUrl)
      if (!res.success) {
        addToast({ message: res.message || '转写失败', type: 'error' })
        return
      }
      const text = (res.data?.text || '').trim()
      if (!text) {
        addToast({ message: '没听清这条语音', type: 'error' })
        return
      }
      setAudioTextMap((prev) => ({ ...prev, [msg.messageId!]: text }))
    } catch (e: any) {
      addToast({ message: e?.message || '转写失败', type: 'error' })
    } finally {
      setTranscribingMsgId('')
    }
  }

  const handleImageSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    // 选完即清空 value，保证同一张图片可重复选择触发 onChange
    e.target.value = ''
    if (!file) return
    await sendImageFile(file)
  }

  const handlePasteImage = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = Array.from(e.clipboardData?.items || [])
    const imageItem = items.find((item) => item.kind === 'file' && item.type.startsWith('image/'))
    if (!imageItem) return

    e.preventDefault()
    const blob = imageItem.getAsFile()
    if (!blob) {
      addToast({ message: '读取剪贴板图片失败', type: 'error' })
      return
    }

    const file = new File([blob], getClipboardImageFileName(blob.type), {
      type: blob.type || 'image/png',
      lastModified: Date.now(),
    })
    if (!validateImageFile(file)) return

    setPendingImage((prev) => {
      if (prev) URL.revokeObjectURL(prev.previewUrl)
      return { file, previewUrl: URL.createObjectURL(file) }
    })
  }

  const handleSendMessage = async () => {
    if (pendingImage) {
      const file = pendingImage.file
      clearPendingImage()
      await sendImageFile(file)
      return
    }
    sendMessageText(inputText, true)
  }

  // 选表情：插到输入框光标处（无输入框引用时退化为追加到末尾）
  const handleEmojiSelect = (emoji: string) => {
    const el = inputRef.current
    if (!el) {
      setInputText((prev) => prev + emoji)
      return
    }
    const start = el.selectionStart ?? inputText.length
    const end = el.selectionEnd ?? start
    setInputText(inputText.slice(0, start) + emoji + inputText.slice(end))
    // 等 state 渲染完再把光标放回插入点之后
    requestAnimationFrame(() => {
      el.focus()
      const pos = start + emoji.length
      el.setSelectionRange(pos, pos)
    })
  }

  // ==================== 时间格式化 ====================
  const formatTime = (ts: number) => {
    if (!ts) return ''
    const d = new Date(ts)
    const now = new Date()
    const isToday =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    if (isToday) {
      return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    }
    return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) +
      ' ' +
      d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }

  // ==================== 面板拖拽调宽 ====================
  const [panelWidths, setPanelWidths] = useState(loadPanelWidths)
  const [draggingPanel, setDraggingPanel] = useState(false)
  const dragStateRef = useRef<{ key: PanelKey; startX: number; startWidth: number } | null>(null)

  const startPanelDrag = (key: PanelKey) => (e: ReactPointerEvent) => {
    e.preventDefault()
    dragStateRef.current = { key, startX: e.clientX, startWidth: panelWidths[key] }
    setDraggingPanel(true)
  }

  useEffect(() => {
    if (!draggingPanel) return
    const onMove = (e: PointerEvent) => {
      const st = dragStateRef.current
      if (!st) return
      const [min, max] = PANEL_LIMITS[st.key]
      const w = Math.min(max, Math.max(min, st.startWidth + PANEL_DRAG_DIR[st.key] * (e.clientX - st.startX)))
      setPanelWidths(prev => (prev[st.key] === w ? prev : { ...prev, [st.key]: w }))
    }
    const onUp = () => {
      dragStateRef.current = null
      setDraggingPanel(false)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [draggingPanel])

  // 拖拽结束（以及首次进入）时把宽度落盘
  useEffect(() => {
    if (draggingPanel) return
    try { localStorage.setItem(PANEL_WIDTHS_KEY, JSON.stringify(panelWidths)) } catch { /* ignore */ }
  }, [panelWidths, draggingPanel])

  /** 栏间拖拽手柄：桌面端才有（手机端是 Tab 切换布局） */
  const panelResizer = (key: PanelKey) => (
    <div
      onPointerDown={startPanelDrag(key)}
      className={`hidden md:block w-1.5 flex-shrink-0 self-stretch cursor-col-resize rounded-full transition-colors ${
        draggingPanel ? 'bg-blue-400/60' : 'bg-transparent hover:bg-blue-300/50 dark:hover:bg-blue-500/40'
      }`}
      title="拖拽调整宽度"
    />
  )

  // ==================== 渲染 ====================
  // 手机端各 Tab 对应未读 / 状态提示
  const totalUnread = conversations.reduce((sum, c) => sum + (c.unreadCount || 0), 0)
  const tabItems: Array<{ key: MobileTab; label: string; badge?: number; disabled?: boolean }> = [
    { key: 'accounts', label: '账号' },
    { key: 'convs', label: '会话', badge: totalUnread, disabled: !activeAccountId },
    { key: 'chat', label: '聊天', disabled: !activeCid },
    { key: 'tools', label: '工作区', disabled: !activeCid },
  ]

  return (
    <div className="flex flex-col h-[calc(100vh-120px)]">
      {/* 手机端顶部 Tab 切换栏（桌面端隐藏） */}
      <div className="md:hidden mb-2 flex bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        {tabItems.map((item) => {
          const active = mobileTab === item.key
          return (
            <button
              key={item.key}
              type="button"
              disabled={item.disabled}
              onClick={() => setMobileTab(item.key)}
              className={`flex-1 py-2 text-xs font-medium transition-colors relative ${
                active
                  ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-300'
                  : 'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40'
              } disabled:opacity-40 disabled:cursor-not-allowed`}
            >
              {item.label}
              {item.badge && item.badge > 0 ? (
                <span className="absolute top-1 right-1/2 translate-x-6 bg-red-500 text-white text-[10px] rounded-full px-1 min-w-[16px] text-center">
                  {item.badge > 99 ? '99+' : item.badge}
                </span>
              ) : null}
            </button>
          )
        })}
      </div>

      <div
        className={`flex flex-1 min-h-0 flex-col md:flex-row gap-3 ${draggingPanel ? 'select-none' : ''}`}
        style={{
          '--w-accounts': `${panelWidths.accounts}px`,
          '--w-convs': `${panelWidths.convs}px`,
          '--w-tools': `${panelWidths.tools}px`,
        } as CSSProperties}
      >
      {/* 左侧：账号列表 */}
      <div className={`${mobileTab === 'accounts' ? 'flex' : 'hidden'} md:flex w-full md:w-[var(--w-accounts)] flex-shrink-0 bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 flex-col min-h-0`}>
        <div className="p-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <span className="font-medium text-sm text-gray-700 dark:text-gray-300">账号列表</span>
          <button
            onClick={() => loadAccounts()}
            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
            title="刷新"
          >
            <RefreshCw className={`w-4 h-4 text-gray-500 ${loadingAccounts ? 'animate-spin' : ''}`} />
          </button>
        </div>
        <div
          ref={accountListRef}
          className="flex-1 overflow-y-auto p-2 space-y-1"
          onScroll={(e) => {
            const el = e.currentTarget
            if (el.scrollHeight - el.scrollTop - el.clientHeight < 40) {
              loadMoreAccounts()
            }
          }}
        >
          {loadingAccounts && accounts.length === 0 ? (
            <div className="flex justify-center py-8">
              <Loader2 className="w-5 h-5 animate-spin text-blue-500" />
            </div>
          ) : accounts.length === 0 ? (
            <p className="text-center text-sm text-gray-400 py-8">暂无可用账号</p>
          ) : (
            <>
              {accounts.map((acc) => (
                <div
                  key={acc.account_id}
                  className={`p-2 rounded-lg transition-colors text-sm cursor-pointer ${
                    activeAccountId === acc.account_id
                      ? 'bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-700'
                      : 'hover:bg-gray-50 dark:hover:bg-gray-700/50 border border-transparent'
                  }`}
                  onClick={() => handleSelectAccount(acc)}
                  title={`${acc.display_name || acc.remark || acc.account_id}\n(${acc.account_id})`}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      <User className="w-4 h-4 flex-shrink-0 text-gray-400" />
                      <div className="min-w-0 flex-1">
                        <span className="block truncate text-gray-700 dark:text-gray-300">
                          {acc.display_name || acc.remark || acc.account_id}
                        </span>
                        {(acc.display_name || acc.remark) && (
                          <span className="block truncate text-xs text-gray-400 dark:text-gray-500">
                            {acc.remark && acc.remark !== acc.display_name ? acc.remark : acc.account_id}
                          </span>
                        )}
                        {acc.owner && (
                          <span className="block truncate text-xs text-blue-400 dark:text-blue-500">
                            {acc.owner}
                          </span>
                        )}
                      </div>
                    </div>
                    {acc.connected ? (
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDisconnect(acc.account_id) }}
                        className="p-1 text-red-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/30 rounded"
                        title="断开"
                      >
                        <LogOut className="w-3.5 h-3.5" />
                      </button>
                    ) : (
                      <button
                        onClick={(e) => { e.stopPropagation(); handleConnect(acc.account_id) }}
                        disabled={!!connectingId}
                        className="p-1 text-green-500 hover:text-green-700 hover:bg-green-50 dark:hover:bg-green-900/30 rounded disabled:opacity-50"
                        title="连接"
                      >
                        {connectingId === acc.account_id ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                          <LogIn className="w-3.5 h-3.5" />
                        )}
                      </button>
                    )}
                  </div>
                  <div className="mt-1 flex items-center gap-1">
                    <span
                      className={`inline-block w-1.5 h-1.5 rounded-full ${
                        acc.connected ? 'bg-green-500' : acc.status !== 'active' ? 'bg-orange-400' : 'bg-gray-300'
                      }`}
                    />
                    <span className="text-xs text-gray-400">
                      {acc.connected ? '已连接' : acc.status !== 'active' ? '已禁用' : '未连接'}
                    </span>
                    {(accountUnread[acc.account_id] || 0) > 0 && (
                      <span
                        className="ml-auto inline-flex min-w-[18px] h-[18px] px-1 items-center justify-center rounded-full bg-red-500 text-white text-[10px] font-medium"
                        title={`${accountUnread[acc.account_id]} 条新消息`}
                      >
                        {accountUnread[acc.account_id] > 99 ? '99+' : accountUnread[acc.account_id]}
                      </span>
                    )}
                  </div>
                </div>
              ))}
              {accountHasMore && (
                <button
                  onClick={loadMoreAccounts}
                  disabled={loadingAccounts}
                  className="w-full py-2 text-xs text-blue-500 hover:bg-gray-50 dark:hover:bg-gray-700/30 disabled:opacity-50"
                >
                  {loadingAccounts ? '加载中...' : '加载更多'}
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {panelResizer('accounts')}
      {/* 中间：会话列表 */}
      <div className={`${mobileTab === 'convs' ? 'flex' : 'hidden'} md:flex w-full md:w-[var(--w-convs)] flex-shrink-0 bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 flex-col min-h-0`}>
        <div className="p-3 border-b border-gray-200 dark:border-gray-700 space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-medium text-sm text-gray-700 dark:text-gray-300">会话列表</span>
            <div className="flex items-center gap-1">
              <SoundPicker />
              {activeAccountId && (
                <button
                  onClick={() => { setConvCursor(null); loadConversations(activeAccountId, false, false, true) }}
                  className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                  title="刷新会话"
                >
                  <RefreshCw className={`w-4 h-4 text-gray-500 ${loadingConvs ? 'animate-spin' : ''}`} />
                </button>
              )}
            </div>
          </div>
          {activeAccountId && (
            <div className="relative">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400 pointer-events-none" />
              <input
                value={convSearch}
                onChange={(e) => setConvSearch(e.target.value)}
                placeholder="搜索用户 / 消息关键词"
                className="w-full pl-7 pr-7 py-1.5 text-xs rounded-md border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-700 dark:text-gray-200 placeholder:text-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
              {convSearch && (
                <button
                  onClick={() => setConvSearch('')}
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                  title="清除"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          )}
        </div>
        <div
          className="flex-1 overflow-y-auto"
          onScroll={(e) => {
            const el = e.currentTarget
            if (el.scrollHeight - el.scrollTop - el.clientHeight < 40 && convHasMore && !loadingConvs && activeAccountId) {
              loadConversations(activeAccountId, true)
            }
          }}
        >
          {!activeAccountId ? (
            <p className="text-center text-sm text-gray-400 py-12">请先选择账号</p>
          ) : loadingConvs && conversations.length === 0 ? (
            <div className="flex justify-center py-12">
              <Loader2 className="w-5 h-5 animate-spin text-blue-500" />
            </div>
          ) : conversations.length === 0 ? (
            <p className="text-center text-sm text-gray-400 py-12">
              {accounts.find((a) => a.account_id === activeAccountId)?.connected
                ? '暂无会话'
                : '请先连接账号'}
            </p>
          ) : visibleConvs.length === 0 ? (
            <p className="text-center text-sm text-gray-400 py-12">
              没有匹配「{convSearch.trim()}」的会话
            </p>
          ) : (
            <>
              {visibleConvs.map((conv, idx) => (
                <div
                  key={conv.cid || `conv-${idx}`}
                  className={`group relative px-3 py-2.5 cursor-pointer border-b border-gray-100 dark:border-gray-700/50 transition-colors ${
                    normCid(activeCid) === normCid(conv.cid)
                      ? 'bg-blue-100 dark:bg-blue-900/40 shadow-[inset_3px_0_0_0_#3b82f6,0_1px_3px_rgba(0,0,0,0.08)]'
                      : 'hover:bg-gray-50 dark:hover:bg-gray-700/30'
                  }`}
                  onClick={() => handleSelectConversation(conv.cid)}
                >
                  {/* 删除会话：桌面端悬停显示，手机端常显 */}
                  <button
                    type="button"
                    title="删除会话"
                    disabled={deletingConvCid === conv.cid}
                    onClick={(e) => {
                      e.stopPropagation()
                      handleDeleteConversation(conv)
                    }}
                    className="absolute top-1.5 right-1.5 z-10 p-1 rounded text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30 opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus:opacity-100 transition-opacity disabled:opacity-40"
                  >
                    {deletingConvCid === conv.cid
                      ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      : <Trash2 className="w-3.5 h-3.5" />}
                  </button>
                  <div className="flex items-center gap-2">
                    {conv.otherUserAvatar ? (
                      <img
                        src={conv.otherUserAvatar}
                        className="w-9 h-9 rounded-full flex-shrink-0 object-cover"
                        alt=""
                      />
                    ) : (
                      <div className="w-9 h-9 rounded-full bg-gray-200 dark:bg-gray-600 flex items-center justify-center flex-shrink-0">
                        <User className="w-5 h-5 text-gray-400" />
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      {/* 时间戳贴右对齐：桌面端不留 pr-8，悬停出删除按钮时把时间淡出让位；
                          手机端删除按钮常显，保留 pr-8 防重叠 */}
                      <div className="flex items-center justify-between pr-8 md:pr-0">
                        <span className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">
                          {conv.otherUserName || conv.otherUserId || '未知用户'}
                        </span>
                        {/* 有进行中订单的买家：昵称后挂状态胶囊，配色与右侧订单面板一致 */}
                        {(() => {
                          const mark = conv.otherUserId ? orderMarks[conv.otherUserId] : undefined
                          if (!mark) return null
                          const meta = getOrderStatusMeta(mark.status)
                          return (
                            <span
                              className={`flex-shrink-0 ml-1.5 text-[11px] leading-none px-1.5 py-0.5 rounded ${meta.className}`}
                              title={`与该买家有 ${mark.count} 笔进行中的订单`}
                            >
                              {meta.label}{mark.count > 1 ? ` ×${mark.count}` : ''}
                            </span>
                          )
                        })()}
                        <span className="text-xs text-gray-400 flex-shrink-0 ml-auto md:transition-opacity md:group-hover:opacity-0">
                          {formatTime(conv.lastMessageTime)}
                        </span>
                      </div>
                      {conv.itemTitle && (
                        <div className="text-xs text-blue-400 truncate mt-0.5">
                          {conv.itemTitle}
                        </div>
                      )}
                      <div className="flex items-center justify-between mt-0.5">
                        <span className="text-xs text-gray-400 truncate">
                          <ConversationSummary text={conv.lastMessageSummary} />
                        </span>
                        {/* 正在看的会话不显示红点（看见即已读），双保险 */}
                        {normCid(conv.cid) !== normCid(activeCid) && conv.unreadCount > 0 && (
                          <span className="ml-2 flex-shrink-0 bg-red-500 text-white text-xs rounded-full px-1.5 min-w-[18px] text-center">
                            {conv.unreadCount > 99 ? '99+' : conv.unreadCount}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
              {convHasMore && (
                <button
                  onClick={() => loadConversations(activeAccountId, true)}
                  disabled={loadingConvs}
                  className="w-full py-2 text-xs text-blue-500 hover:bg-gray-50 dark:hover:bg-gray-700/30 disabled:opacity-50"
                >
                  {loadingConvs ? '加载中...' : '加载更多'}
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {/* 右侧：聊天记录 */}
      {panelResizer('convs')}
      <div className={`${mobileTab === 'chat' ? 'flex' : 'hidden'} md:flex flex-1 min-w-0 min-h-0 bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 flex-col`}>
        {/* 聊天头部 */}
        <div className="p-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
          <MessageCircle className="w-4 h-4 text-gray-500" />
          <span className="font-medium text-sm text-gray-700 dark:text-gray-300">
            {activeCid
              ? conversations.find((c) => c.cid === activeCid)?.otherUserName || '聊天记录'
              : '聊天记录'}
          </span>
          {/* 对方正在输入：来自闲鱼输入状态帧，4 秒无更新自动隐藏 */}
          {typingConv && typingConv.accountId === activeAccountId &&
            typingConv.cid === normCid(activeCid) && (
            <span className="flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
              <span className="flex gap-0.5">
                <span className="w-1 h-1 rounded-full bg-emerald-500 animate-bounce [animation-delay:0ms]" />
                <span className="w-1 h-1 rounded-full bg-emerald-500 animate-bounce [animation-delay:150ms]" />
                <span className="w-1 h-1 rounded-full bg-emerald-500 animate-bounce [animation-delay:300ms]" />
              </span>
              对方正在输入
            </span>
          )}
          </div>
          {activeCid && (
            <button onClick={handleBlacklistCustomer} disabled={blacklisting} className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded border border-red-200 text-red-500 hover:bg-red-50 disabled:opacity-40" title={isOfficiallyBlocked ? '解除闲鱼官方黑名单' : '加入闲鱼官方黑名单'}>
              {blacklisting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Ban className="w-3.5 h-3.5" />}
              {isOfficiallyBlocked ? '解除黑名单' : '加入黑名单'}
            </button>
          )}
        </div>

        {/* 名片 + 会话商品：各占一整行（改回上下堆叠，信息能完整铺开）。
            悬停任一条可看完整资料 */}
        <div className="flex flex-col items-stretch border-b border-gray-200 dark:border-gray-700">
          {/* 对方名片条：闲鱼主页信息直接摆在会话里，点开可看完整主页 */}
          {activeConversation?.otherUserId && peerProfile && (
            <button
              onClick={() => openXianyuProfile(
                activeConversation.otherUserId,
                betterNick(peerProfile.nick || '', activeConversation.otherUserName || ''),
                peerProfile.avatar || activeConversation.otherUserAvatar,
              )}
              title={[
                betterNick(peerProfile.nick || '', activeConversation.otherUserName || ''),
                peerProfile.lastVisitTime,
                peerProfile.shopLevel && `店铺等级 ${peerProfile.shopLevel}`,
                typeof peerProfile.praiseRatio === 'number' && `好评率 ${peerProfile.praiseRatio}%`,
                peerProfile.replyRatio24h && `24h回复 ${peerProfile.replyRatio24h}`,
                peerProfile.replyInterval && `平均 ${peerProfile.replyInterval} 回复`,
                peerProfile.ipLocation,
                peerProfile.followers && `粉丝 ${peerProfile.followers}`,
                typeof peerProfile.itemCount === 'number' && `在售宝贝 ${peerProfile.itemCount}`,
                peerProfile.certifications.length > 0 && `认证：${peerProfile.certifications.join('、')}`,
                peerProfile.introduction,
                '——点击查看闲鱼个人主页',
              ].filter(Boolean).join('\n')}
              className="w-full flex items-center gap-3 px-3 py-2 border-b border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors text-left"
            >
              {peerProfile.avatar ? (
                <img
                  src={peerProfile.avatar}
                  alt=""
                  className="w-9 h-9 rounded-full object-cover flex-shrink-0 bg-gray-100 dark:bg-gray-700"
                />
              ) : (
                <div className="w-9 h-9 rounded-full bg-gray-100 dark:bg-gray-700 flex items-center justify-center flex-shrink-0">
                  <User className="w-4 h-4 text-gray-400" />
                </div>
              )}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5 min-w-0">
                  <span className="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">
                    {/* 主页接口的昵称是打码的（樱***1），有完整昵称时优先显示完整版 */}
                    {betterNick(peerProfile.nick || '', activeConversation.otherUserName || '') || '未知用户'}
                  </span>
                  {peerProfile.lastVisitTime && (
                    <span className="flex-shrink-0 px-1 rounded bg-emerald-100 dark:bg-emerald-900/40 text-[10px] text-emerald-700 dark:text-emerald-300">
                      {peerProfile.lastVisitTime}
                    </span>
                  )}
                  {peerProfile.shopLevel && (
                    <span className="flex-shrink-0 px-1 rounded bg-amber-100 dark:bg-amber-900/40 text-[10px] text-amber-700 dark:text-amber-300">
                      {peerProfile.shopLevel}
                    </span>
                  )}
                  {peerProfile.licensed && (
                    <span className="flex-shrink-0 px-1 rounded bg-blue-100 dark:bg-blue-900/40 text-[10px] text-blue-600 dark:text-blue-300">
                      持证
                    </span>
                  )}
                  {typeof peerProfile.praiseRatio === 'number' && (
                    <span className="flex-shrink-0 text-[10px] text-emerald-600 dark:text-emerald-400">
                      好评{peerProfile.praiseRatio}%
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1.5 mt-0.5 text-[11px] text-gray-400 min-w-0 overflow-hidden">
                  {peerProfile.replyRatio24h && (
                    <span className="flex-shrink-0">24h回复{peerProfile.replyRatio24h}</span>
                  )}
                  {peerProfile.replyInterval && (
                    <span className="flex-shrink-0">平均{peerProfile.replyInterval}回复</span>
                  )}
                  {peerProfile.ipLocation && <span className="flex-shrink-0">{peerProfile.ipLocation}</span>}
                  {peerProfile.followers && <span className="flex-shrink-0">粉丝{peerProfile.followers}</span>}
                  {typeof peerProfile.itemCount === 'number' && (
                    <span className="flex-shrink-0">宝贝{peerProfile.itemCount}</span>
                  )}
                  {peerProfile.certifications.slice(0, 2).map((cert) => (
                    <span key={cert} className="flex-shrink-0 px-1 rounded bg-gray-100 dark:bg-gray-700">
                      {cert}
                    </span>
                  ))}
                  {peerProfile.introduction && (
                    <span className="truncate">· {peerProfile.introduction}</span>
                  )}
                </div>
              </div>
              <ExternalLink className="w-4 h-4 text-gray-400 flex-shrink-0" />
            </button>
          )}

          {/* 会话关联的商品条：点开可看闲鱼商品页（图/标题来自会话，价格来自详情接口） */}
          {activeConversation?.itemId && (
            <button
              onClick={() => setItemModalOpen(true)}
              title={[
                activeConversation.itemTitle || itemInfo?.title || '商品',
                itemInfo?.price && `价格 ¥${itemInfo.price}`,
                `商品ID ${activeConversation.itemId}`,
                '——点击查看闲鱼商品页',
              ].filter(Boolean).join('\n')}
              className="w-full flex items-center gap-3 px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors text-left"
            >
              {activeConversation.itemMainPic ? (
                <img
                  src={activeConversation.itemMainPic}
                  alt=""
                  className="w-10 h-10 rounded object-cover flex-shrink-0 bg-gray-100 dark:bg-gray-700"
                />
              ) : (
                <div className="w-10 h-10 rounded bg-gray-100 dark:bg-gray-700 flex items-center justify-center flex-shrink-0">
                  <Package className="w-5 h-5 text-gray-400" />
                </div>
              )}
              <div className="flex-1 min-w-0">
                <div className="text-sm text-gray-700 dark:text-gray-200 truncate">
                  {activeConversation.itemTitle || itemInfo?.title || '商品'}
                </div>
                <div className="text-xs text-gray-400 truncate">
                  商品ID {activeConversation.itemId} · 点击查看商品
                </div>
              </div>
              {itemInfo?.price ? (
                <span className="text-sm font-semibold text-red-500 flex-shrink-0">
                  ¥{itemInfo.price}
                </span>
              ) : null}
              <ExternalLink className="w-4 h-4 text-gray-400 flex-shrink-0" />
            </button>
          )}
        </div>

        {/* 消息区域 */}
        <div ref={msgContainerRef} className="flex-1 overflow-y-auto p-4 space-y-3">
          {!activeCid ? (
            <p className="text-center text-sm text-gray-400 py-12">请选择一个会话查看聊天记录</p>
          ) : loadingMsgs && messages.length === 0 ? (
            <div className="flex justify-center py-12">
              <Loader2 className="w-5 h-5 animate-spin text-blue-500" />
            </div>
          ) : (
            <>
              {/* 加载更多历史 */}
              {msgHasMore && (
                <div className="text-center">
                  <button
                    onClick={() => loadMessages(activeAccountId, activeCid, true)}
                    disabled={loadingMsgs}
                    className="inline-flex items-center gap-1 text-xs text-blue-500 hover:underline disabled:opacity-50"
                  >
                    <ChevronUp className="w-3 h-3" />
                    {loadingMsgs ? '加载中...' : '加载更早的消息'}
                  </button>
                </div>
              )}
              {messages.map((msg, idx) => {
                const isSystem = msg.type === 'system'
                // 系统 / 交易事件消息：按发送者左右对齐，和普通消息一样待在对话流里，
                // 只是气泡换成事件卡样式（浅底 + 描边 + 图标），一眼能和打字消息区分开。
                const isEvent = !!msg.isEvent
                const EventIcon = EVENT_ICONS[msg.eventKind || 'generic'] || Info
                // 对方头像：优先用户信息缓存，退回当前会话的头像
                const otherAvatar =
                  userInfoCacheRef.current[msg.senderId]?.avatar || activeConversation?.otherUserAvatar || ''
                // 自己（卖家）头像：来自账号资料缓存
                const selfAvatar = (activeAccountId && selfAvatars[activeAccountId]) || ''
                return (
                <div
                  key={msg.messageId || idx}
                  className={`flex gap-2 ${msg.isSelf ? 'justify-end' : 'justify-start'}`}
                >
                  {/* 对方头像（系统消息不显示） */}
                  {!msg.isSelf && !isSystem && (
                    <button
                      type="button"
                      title="查看 TA 的闲鱼主页"
                      className="flex-shrink-0 mt-5 rounded-full hover:opacity-80 transition-opacity"
                      onClick={() => openXianyuProfile(msg.senderId, msg.senderName, otherAvatar)}
                    >
                      {otherAvatar ? (
                        <img
                          src={otherAvatar}
                          className="w-9 h-9 rounded-full object-cover"
                          alt=""
                        />
                      ) : (
                        <div className="w-9 h-9 rounded-full bg-gray-200 dark:bg-gray-600 flex items-center justify-center">
                          <User className="w-5 h-5 text-gray-400" />
                        </div>
                      )}
                    </button>
                  )}
                  <div className="max-w-[70%]">
                    {/* 发送者名称（系统事件消息常无昵称，此时只显示时间） */}
                    <div
                      className={`text-xs text-gray-400 mb-1 ${
                        msg.isSelf ? 'text-right' : 'text-left'
                      }`}
                    >
                      {msg.senderName ? (
                        <>
                          {msg.senderName}
                          <span className="ml-2">{formatTime(msg.time)}</span>
                        </>
                      ) : (
                        formatTime(msg.time)
                      )}
                    </div>
                    {/* 消息气泡（失败的本地消息在气泡前显示红色感叹号，点击查看原因） */}
                    <div className={`flex items-center gap-1.5 ${msg.isSelf ? 'flex-row-reverse' : ''}`}>
                      <div
                        className={`rounded-lg px-3 py-2 text-sm break-words ${
                          isEvent || msg.card
                            ? msg.isSelf
                              ? 'bg-blue-50 border border-blue-200 text-blue-700 dark:bg-blue-900/30 dark:border-blue-700/60 dark:text-blue-200'
                              : 'bg-gray-50 border border-gray-200 text-gray-600 dark:bg-gray-700/50 dark:border-gray-600 dark:text-gray-300'
                            : msg.isSelf
                            ? 'bg-blue-500 text-white'
                            : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200'
                        } ${msg.type === 'system' ? '!bg-gray-200 dark:!bg-gray-600 text-center text-gray-500 dark:text-gray-400 text-xs' : ''}`}
                      >
                        {msg.type === 'audio' && msg.audioUrl ? (
                          <div className="space-y-1 min-w-[140px]">
                            <div className="flex items-center gap-2">
                              <audio
                                controls
                                src={msg.audioUrl}
                                className="h-8 max-w-[220px]"
                                preload="metadata"
                              />
                              {msg.audioDuration ? (
                                <span className="text-xs text-gray-500 dark:text-gray-400 flex-shrink-0">
                                  {Math.max(1, Math.round(msg.audioDuration / 1000))}″
                                </span>
                              ) : null}
                            </div>
                            {msg.messageId ? (
                              audioTextMap[msg.messageId] ? (
                                <div className="text-xs leading-5 whitespace-pre-wrap opacity-90 pt-0.5">
                                  {audioTextMap[msg.messageId]}
                                </div>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => transcribeMessageAudio(msg)}
                                  disabled={transcribingMsgId === msg.messageId}
                                  className="text-xs underline opacity-80 hover:opacity-100 disabled:opacity-50 disabled:no-underline"
                                >
                                  {transcribingMsgId === msg.messageId ? '转写中…' : '转文字'}
                                </button>
                              )
                            ) : null}
                          </div>
                        ) : msg.type === 'image' && msg.images.length > 0 ? (
                          <div className="space-y-1">
                            {msg.images.map((url, i) => (
                              <img
                                key={i}
                                src={url}
                                className="max-w-full rounded max-h-48 object-contain cursor-pointer hover:opacity-80 transition-opacity"
                                alt="图片消息"
                                onClick={() => setPreviewImage(url)}
                                onContextMenu={(e) => {
                                  e.preventDefault()
                                  setImageMenu({ x: e.clientX, y: e.clientY, url })
                                }}
                              />
                            ))}
                          </div>
                        ) : msg.card ? (
                          // 订单卡片富气泡：与闲鱼 APP 一致的标题/描述/演出信息/订单号/按钮
                          <OrderCardBubble
                            card={msg.card}
                            sessionOrders={customerOrders}
                            deliveringOrderNo={deliveringOrderNo}
                            confirmingOrderNo={confirmingOrderNo}
                            adjustingOrderNo={adjustingOrderNo}
                            onDeliver={handleDeliverOrder}
                            onNoLogisticsDeliver={handleNoLogisticsDelivery}
                            onAdjustPrice={openAdjustPriceModal}
                            onDeepLink={openDeepLinkModal}
                            onToast={addToast}
                          />
                        ) : isEvent ? (
                          // 事件卡气泡：图标 + 文本，浅底描边已在外层 className 处理
                          <span className="inline-flex items-start gap-1.5">
                            <EventIcon className="w-3.5 h-3.5 flex-shrink-0 opacity-70 mt-[2px]" />
                            <span className="whitespace-pre-wrap">
                              <ChatText text={msg.text} emojiClassName="px-0.5 text-base align-middle" />
                            </span>
                          </span>
                        ) : (
                          // 闲鱼的黄脸表情在消息里是 [中文名] 代码，这里翻译渲染
                          <span className="whitespace-pre-wrap">
                            <ChatText text={msg.text} emojiClassName="px-0.5 text-base align-middle" />
                          </span>
                        )}
                      </div>
                      {msg.isSelf && msg.failed && (
                        <button
                          type="button"
                          title="发送失败，点击查看原因"
                          onClick={() =>
                            addToast({ message: msg.failReason || '发送失败', type: 'error' })
                          }
                          className="flex-shrink-0 text-red-500 hover:text-red-600 transition-colors"
                        >
                          <AlertCircle className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                    {/* 已读/未读：readStatus 来自 IM 原始数据（0=未读 2=已读），自己消息代表对方是否已读 */}
                    {msg.isSelf && msg.type !== 'system' && (msg.readStatus === 0 || msg.readStatus === 2) && (
                      <div
                        className={`text-xs mt-0.5 ${
                          msg.readStatus === 2
                            ? 'text-gray-400 dark:text-gray-500'
                            : 'text-amber-500 dark:text-amber-400'
                        } ${msg.isSelf ? 'text-right' : 'text-left'}`}
                      >
                        {msg.readStatus === 2 ? '已读' : '未读'}
                      </div>
                    )}
                    {canRecallMessage(msg) && (
                      <div className="mt-1 text-right">
                        <button
                          onClick={() => handleRecallMessage(msg)}
                          disabled={!!recallingMessageId}
                          className="text-xs text-gray-400 hover:text-red-500 disabled:opacity-40"
                        >
                          {recallingMessageId === msg.messageId ? '撤回中...' : '撤回'}
                        </button>
                      </div>
                    )}
                  </div>
                  {/* 自己头像 */}
                  {msg.isSelf && (
                    <button
                      type="button"
                      onClick={() => openXianyuProfile(msg.senderId, msg.senderName, selfAvatar)}
                      title="查看我的闲鱼主页"
                      className="flex-shrink-0 mt-5 rounded-full hover:opacity-80 transition-opacity"
                    >
                      {selfAvatar ? (
                        <img
                          src={selfAvatar}
                          className="w-9 h-9 rounded-full object-cover"
                          alt=""
                        />
                      ) : (
                        <div className="w-9 h-9 rounded-full bg-blue-100 dark:bg-blue-900 flex items-center justify-center">
                          <User className="w-5 h-5 text-blue-400" />
                        </div>
                      )}
                    </button>
                  )}
                </div>
                )
              })}
            </>
          )}
        </div>
        {/* 底部输入框 */}
        {activeCid && (
          <div className="p-3 border-t border-gray-200 dark:border-gray-700">
            <input
              ref={imageInputRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={handleImageSelected}
            />
            {pendingImage && (
              <div className="mb-2 inline-flex items-start gap-2 rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700/50 p-2">
                <img
                  src={pendingImage.previewUrl}
                  className="h-20 w-20 rounded object-contain bg-white dark:bg-gray-800 cursor-pointer"
                  alt="待发送图片"
                  onClick={() => setPreviewImage(pendingImage.previewUrl)}
                />
                <div className="min-w-0 max-w-44">
                  <div className="truncate text-xs text-gray-600 dark:text-gray-300">{pendingImage.file.name}</div>
                  <div className="mt-1 text-xs text-gray-400">点击发送按钮后发送图片</div>
                </div>
                <button
                  type="button"
                  onClick={clearPendingImage}
                  disabled={sending}
                  title="移除待发送图片"
                  className="rounded p-1 text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-600 dark:hover:text-gray-200 disabled:opacity-50"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            )}
            {isRecording && (
              <div className="mb-2 flex items-center gap-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800">
                <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
                <span className="text-sm text-red-600 dark:text-red-300">
                  录音中 {String(Math.floor(recordSeconds / 60)).padStart(2, '0')}:
                  {String(recordSeconds % 60).padStart(2, '0')}
                </span>
                <span className="text-xs text-red-400">再点一次麦克风结束录音</span>
              </div>
            )}
            {pendingAudio && (
              <div className="mb-2 flex flex-wrap items-center gap-2 px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-700/50 border border-gray-200 dark:border-gray-600">
                <audio
                  controls
                  src={pendingAudio.url}
                  className="h-8 max-w-[220px]"
                  preload="metadata"
                />
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {Math.max(1, Math.round(pendingAudio.durationMs / 1000))}″
                </span>
                <div className="flex items-center gap-2 ml-auto">
                  <button
                    type="button"
                    onClick={() => transcribePendingAudio()}
                    disabled={transcribing || sending}
                    title="把这段语音转成文字，填进输入框后发送（推荐，最稳定）"
                    className="inline-flex items-center gap-1 px-2.5 h-7 text-xs rounded-md bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {transcribing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileText className="w-3.5 h-3.5" />}
                    {transcribing ? '转写中…' : '转文字'}
                  </button>
                  <button
                    type="button"
                    onClick={sendPendingAudio}
                    disabled={transcribing || sending || voiceUnsupported}
                    title={
                      voiceUnsupported
                        ? '闲鱼网页端的上传通道只收图片，发不了真语音。想发真语音：系统设置 → 语音（转文字/发送）→ 填「语音消息公网地址」（需要公网服务器或内网穿透）'
                        : '直接发送语音消息。闲鱼网页端上传通道目前只收图片，失败会自动转成文字'
                    }
                    className={cn(
                      'inline-flex items-center gap-1 px-2.5 h-7 text-xs rounded-md border border-gray-300 dark:border-gray-500 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors',
                      voiceUnsupported && 'line-through decoration-gray-400',
                    )}
                  >
                    <Send className="w-3.5 h-3.5" />
                    发送语音
                  </button>
                  <button
                    type="button"
                    onClick={clearPendingAudio}
                    disabled={transcribing || sending}
                    title="丢弃这段录音"
                    className="rounded p-1 text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-600 dark:hover:text-gray-200 disabled:opacity-50"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              </div>
            )}
            <div className="flex items-end gap-2">
              {/* 左侧一列：录音在上、表情在下，两者都放最左 */}
              <div className="flex-shrink-0 flex flex-col gap-1.5">
                <button
                  type="button"
                  onClick={isRecording ? stopRecording : startRecording}
                  disabled={sending && !isRecording}
                  title={isRecording ? '点击停止录音' : '点击开始录音，再点一次结束，然后选择转文字或发送语音'}
                  className={cn(
                    'flex items-center justify-center w-9 h-9 border rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed',
                    isRecording
                      ? 'text-white bg-red-500 border-red-500 hover:bg-red-600'
                      : 'text-gray-500 dark:text-gray-300 border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700'
                  )}
                >
                  {isRecording ? <Square className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
                </button>
                <EmojiPicker onSelect={handleEmojiSelect} />
              </div>
              <button
                type="button"
                onClick={handlePickImage}
                disabled={sending}
                title="发送图片"
                className="flex-shrink-0 flex items-center justify-center w-9 h-9 text-gray-500 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                <ImagePlus className="w-4 h-4" />
              </button>
              <button
                onClick={handleSendMessage}
                disabled={sending || (!inputText.trim() && !pendingImage)}
                className="flex-shrink-0 flex items-center gap-1 px-4 h-9 text-sm font-medium text-white bg-blue-500 rounded-lg hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {sending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Send className="w-4 h-4" />
                )}
                发送
              </button>
              <textarea
                ref={inputRef}
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onPaste={handlePasteImage}
                onKeyDown={(e) => {
                  // Enter 发送；Shift+Enter / Ctrl+Enter 在输入框内换行
                  // 中文输入法选词阶段的回车（compositionend 前）不触发发送
                  const isComposing = e.nativeEvent.isComposing || (e as any).keyCode === 229
                  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !isComposing) {
                    e.preventDefault()
                    handleSendMessage()
                  }
                }}
                placeholder="输入消息...（Shift+Enter 换行，Enter 发送）"
                rows={3}
                className="flex-1 px-3 py-2 text-sm leading-5 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50 resize-none max-h-32 overflow-y-auto whitespace-pre-wrap"
              />
            </div>
          </div>
        )}
      </div>
      {/* 图片预览弹窗 */}
      {panelResizer('tools')}
      {/* 右侧工作区：客户订单 + 快捷短语 */}
      <div className={`${mobileTab === 'tools' ? 'flex' : 'hidden'} md:flex w-full md:w-[var(--w-tools)] flex-shrink-0 min-h-0 flex-col gap-3 overflow-y-auto md:overflow-visible`}>
        <CustomerOrdersPanel
          activeCid={activeCid}
          orders={customerOrders}
          loading={loadingOrders}
          deliveringOrderNo={deliveringOrderNo}
          confirmingOrderNo={confirmingOrderNo}
          cancellingOrderNo={cancellingOrderNo}
          loadingOrderDetail={loadingOrderDetail}
          onSync={handleSyncOrders}
          onViewDetail={handleViewOrderDetail}
          onCancel={handleCancelOrder}
          onNoLogistics={handleNoLogisticsDelivery}
          onDeliver={handleDeliverOrder}
        />
        <QuickPhrasesPanel
          phrases={quickPhrases}
          activeCid={activeCid}
          sending={sending}
          editingPhraseId={editingPhraseId}
          phraseTitle={phraseTitle}
          phraseContent={phraseContent}
          savingPhrase={savingPhrase}
          onSend={(content) => sendMessageText(content)}
          onEdit={(phrase) => { setEditingPhraseId(phrase.id); setPhraseTitle(phrase.title); setPhraseContent(phrase.content) }}
          onDelete={handleDeletePhrase}
          onReset={resetPhraseForm}
          onTitleChange={setPhraseTitle}
          onContentChange={setPhraseContent}
          onSave={handleSavePhrase}
        />
      </div>
      </div>
      <OrderDetailModal
        order={orderDetail}
        fallbackBuyerNick={activeConversation?.otherUserName}
        onClose={() => setOrderDetail(null)}
      />
      {previewImage && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
          onClick={() => setPreviewImage('')}
        >
          <button
            className="absolute top-4 right-4 text-white hover:text-gray-300 transition-colors"
            onClick={() => setPreviewImage('')}
          >
            <X className="w-8 h-8" />
          </button>
          <img
            src={previewImage}
            className="max-w-[90vw] max-h-[90vh] object-contain rounded-lg"
            alt="预览"
            onClick={(e) => e.stopPropagation()}
            onContextMenu={(e) => {
              e.preventDefault()
              setImageMenu({ x: e.clientX, y: e.clientY, url: previewImage })
            }}
          />
        </div>
      )}
      {/* 图片右键菜单：右键聊天图片或预览大图时弹出，用于把图片复制进剪贴板 */}
      {imageMenu && (
        <div
          className="fixed z-[300] min-w-[140px] rounded-lg border border-gray-200 bg-white py-1 shadow-lg dark:border-slate-600 dark:bg-slate-800"
          style={{
            left: Math.min(imageMenu.x, window.innerWidth - 160),
            top: Math.min(imageMenu.y, window.innerHeight - 60),
          }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          <button
            type="button"
            disabled={!!copyingImageUrl}
            onClick={() => handleCopyImage(imageMenu.url)}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-50 dark:text-gray-200 dark:hover:bg-slate-700"
          >
            <Copy className="w-4 h-4" />
            {copyingImageUrl === imageMenu.url ? '复制中...' : '复制图片'}
          </button>
        </div>
      )}
      <ConfirmModal
        isOpen={!!confirmDialog}
        title={confirmDialog?.title}
        message={confirmDialog?.message || ''}
        confirmText={confirmDialog?.confirmText}
        type={confirmDialog?.type}
        onConfirm={() => closeConfirm(true)}
        onCancel={() => closeConfirm(false)}
      />
      {/* 订单改价弹窗：「已拍下」待付款卡片点「修改价格」打开，改价后买家会收到通知 */}
      {adjustingOrderNo && createPortal(
        <div className="fixed inset-0 z-[1000] flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
            onClick={() => { if (!adjustingPrice) setAdjustingOrderNo('') }}
          />
          <div className="relative w-full max-w-xs mx-4 rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl dark:border-slate-700 dark:bg-slate-800">
            <h3 className="mb-1 text-lg font-semibold text-slate-900 dark:text-white">修改价格</h3>
            <p className="mb-4 text-xs text-slate-500 dark:text-slate-400">
              订单号 {adjustingOrderNo} · 改价后买家会收到通知
            </p>
            <label className="mb-1 block text-sm text-slate-600 dark:text-slate-300">改价后总价（元）</label>
            <input
              type="text"
              inputMode="decimal"
              autoFocus
              value={adjustPriceInput}
              onChange={(e) => setAdjustPriceInput(e.target.value.replace(/[^\d.]/g, ''))}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !adjustingPrice && adjustPriceInput) {
                  void handleAdjustOrderPrice(adjustingOrderNo, adjustPriceInput)
                }
              }}
              placeholder="0.00"
              className="mb-4 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none focus:ring-2 focus:ring-orange-500 dark:border-slate-600 dark:bg-slate-700 dark:text-white"
            />
            <div className="flex gap-3">
              <button
                type="button"
                disabled={adjustingPrice}
                onClick={() => setAdjustingOrderNo('')}
                className="flex-1 rounded-lg bg-slate-100 px-4 py-2.5 font-medium text-slate-700 transition-colors hover:bg-slate-200 disabled:opacity-50 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600"
              >
                取消
              </button>
              <button
                type="button"
                disabled={adjustingPrice || !adjustPriceInput}
                onClick={() => handleAdjustOrderPrice(adjustingOrderNo, adjustPriceInput)}
                className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-orange-500 px-4 py-2.5 font-medium text-white transition-colors hover:bg-orange-600 disabled:opacity-50"
              >
                {adjustingPrice && <Loader2 className="h-4 w-4 animate-spin" />}
                确认改价
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
      {/* 闲鱼 APP 深链弹窗：演出信息表单等 fleamarket:// 原生页面网页端无法代填，
          但深链本身有效，复制到手机上打开即可唤起 APP 直接到该订单的对应页面 */}
      {deepLink && createPortal(
        <div className="fixed inset-0 z-[1000] flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
            onClick={() => setDeepLink(null)}
          />
          <div className="relative w-full max-w-md mx-4 rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl dark:border-slate-700 dark:bg-slate-800">
            <div className="mb-1 flex items-start justify-between gap-2">
              <h3 className="text-lg font-semibold text-slate-900 dark:text-white">{deepLink.title || '打开闲鱼 APP'}</h3>
              <button
                type="button"
                onClick={() => setDeepLink(null)}
                className="rounded p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-700"
                title="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {deepLink.title && /演出信息/.test(deepLink.title) && (
              <div className="mb-3 rounded-xl bg-orange-50 p-3 text-[12px] leading-5 text-orange-700 dark:bg-orange-500/10 dark:text-orange-300">
                闲鱼演出票标准流程：① 卖家在 APP 填写演出信息 → ② 买家在 APP 确认演出信息 → ③ 买家确认后订单自动进入「待发货」，回到本页即可发货。
                <br />
                当前由卖家操作第 ① 步，点下方按钮复制链接，在手机闲鱼打开填写页。
              </div>
            )}

            <p className="mb-3 text-xs leading-5 text-slate-500 dark:text-slate-400">
              这是闲鱼 APP 原生页面专属地址，电脑浏览器打不开。复制后发到手机，点开即可直接唤起 APP 打开该订单的对应页面。
            </p>
            <textarea
              id="deeplink-input"
              readOnly
              rows={3}
              value={deepLink.url}
              onFocus={(e) => e.currentTarget.select()}
              className="mb-3 w-full resize-none rounded-lg border border-slate-300 bg-slate-50 px-3 py-2 font-mono text-[11px] leading-4 text-slate-700 outline-none focus:ring-2 focus:ring-orange-500 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
            />
            <ul className="mb-4 space-y-1 text-[11px] leading-4 text-slate-400 dark:text-slate-500">
              <li>1. 微信发给「文件传输助手」，在手机上点开即可唤起 APP</li>
              <li>2. 或粘贴到手机浏览器地址栏，同样会跳转到闲鱼</li>
              <li>3. 需已安装闲鱼 APP 并登录卖家账号</li>
            </ul>
            <button
              type="button"
              onClick={() => void copyDeepLink()}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-orange-500 px-4 py-2.5 font-medium text-white transition-colors hover:bg-orange-600"
            >
              {deepLinkCopied ? (
                <CheckCircle className="h-4 w-4" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
              {deepLinkCopied ? '已复制链接' : '复制链接'}
            </button>
          </div>
        </div>,
        document.body,
      )}
      <XianyuProfileModal
        isOpen={!!profileModal}
        userId={profileModal?.userId || ''}
        name={profileModal?.name || ''}
        avatar={profileModal?.avatar || ''}
        onClose={() => setProfileModal(null)}
      />
      <XianyuItemModal
        isOpen={itemModalOpen}
        itemId={activeConversation?.itemId || ''}
        title={activeConversation?.itemTitle || itemInfo?.title || ''}
        pic={activeConversation?.itemMainPic || ''}
        price={itemInfo?.price}
        originalPrice={itemInfo?.originalPrice}
        transportFee={itemInfo?.transportFee}
        status={itemInfo?.status}
        desc={itemInfo?.desc}
        images={itemInfo?.images}
        browseCnt={itemInfo?.browseCnt}
        collectCnt={itemInfo?.collectCnt}
        quantity={itemInfo?.quantity}
        sellerNick={itemInfo?.sellerNick}
        onClose={() => setItemModalOpen(false)}
      />
    </div>
  )
}

export default ChatNew
