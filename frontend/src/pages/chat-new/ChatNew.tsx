/**
 * 在线聊天 主页面
 *
 * 三栏布局：左侧账号列表 | 中间会话列表 | 右侧聊天记录
 * 支持多账号切换，基于WebSocket API获取数据
 */
import { useEffect, useState, useRef, useCallback } from 'react'
import { Loader2, LogIn, LogOut, MessageCircle, RefreshCw, User, ChevronUp, X, Send, AlertCircle, Ban, ImagePlus, Copy, Package, ExternalLink, Mic, Square, FileText } from 'lucide-react'
import { useUIStore } from '@/store/uiStore'
import {
  getChatAccounts,
  getConnectedChatAccountIds,
  connectAccount,
  disconnectAccount,
  getConversations,
  getMessages,
  sendTextMessage,
  sendImageMessage,
  queryUserInfos,
  getAccountProfile,
  getCustomerOrders,
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
  type QuickPhrase,
  type XianyuUserProfile,
} from '@/api/chatNew'
import { cancelOrder, fetchXianyuOrders, getOrderDetail, manualDelivery, noLogisticsDelivery, type OrderDetail } from '@/api/orders'
import { useChatNewWs } from './useChatNewWs'
import { playNewMessageSound } from '@/utils/notificationSound'
import { appendMsg, mergeConversations, mergeMessages, toTimestampMs } from './chat_history_merge'
import { ChatHistoryRecovery, recoverConversations, recoverMessages,
  type ConversationCache, type MessageCache } from './chat_history_recovery'
import { ConfirmModal } from '@/components/common/ConfirmModal'
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
  const [loadingOrders, setLoadingOrders] = useState(false)
  const [deliveringOrderNo, setDeliveringOrderNo] = useState('')
  const [confirmingOrderNo, setConfirmingOrderNo] = useState('')
  const [cancellingOrderNo, setCancellingOrderNo] = useState('')
  const [orderDetail, setOrderDetail] = useState<OrderDetail | null>(null)
  const [loadingOrderDetail, setLoadingOrderDetail] = useState(false)
  const [blacklisting, setBlacklisting] = useState(false)
  const [isOfficiallyBlocked, setIsOfficiallyBlocked] = useState(false)
  const [recallingMessageId, setRecallingMessageId] = useState('')
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

  // 页面需要实时订阅的账号列表（手动连接成功后加入，刷新后自动恢复已连接账号）
  const [wsAccountIds, setWsAccountIds] = useState<string[]>([])

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
  /** 更新会话列表的通用逻辑（可作用于 state 数组或缓存数组） */
  const updateConvList = (convs: Conversation[], cid: string, summary: string, msg: ChatMessage, isViewing: boolean): Conversation[] => {
    const exists = convs.some((c) => c.cid === cid)
    if (exists) {
      const updated = convs.map((c) => {
        if (c.cid !== cid) return c
        return { ...c, lastMessageSummary: summary, lastMessageTime: msg.time, unreadCount: isViewing ? 0 : c.unreadCount + 1 }
      })
      const target = updated.find((c) => c.cid === cid)!
      return [target, ...updated.filter((c) => c.cid !== cid)]
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

  const handleWsNewMessage = useCallback((accountId: string, cid: string, msg: ChatMessage) => {
    const summary = msg.type === 'image' ? '[图片]' : (msg.text || '').slice(0, 50)
    const isActiveAccount = accountId === activeAccountIdRef.current
    // 新私聊提示音：只响买家发来的消息，自己发出去的不响
    if (!msg.isSelf) {
      playNewMessageSound()
    }
    // 非当前账号收到新消息：账号卡片累计未读角标（切到该账号时清零）
    if (!isActiveAccount && !msg.isSelf) {
      setAccountUnread((prev) => ({ ...prev, [accountId]: (prev[accountId] || 0) + 1 }))
    }

    const isViewingConv = isActiveAccount && cid === activeCidRef.current
    const conversationCache = convsCacheRef.current[accountId]
    if (conversationCache) {
      conversationCache.convs = updateConvList(conversationCache.convs, cid, summary, msg, isViewingConv)
    }
    let messageCache = msgsCacheRef.current[accountId]?.[cid]
    if (!messageCache && isViewingConv) {
      msgsCacheRef.current[accountId] ??= {}
      messageCache = { msgs: messagesRef.current,
        hasMore: historyPageRef.current.msgHasMore, cursor: msgCursorRef.current }
      msgsCacheRef.current[accountId][cid] = messageCache
    }
    // 当前账号的非当前会话也更新缓存；补偿边界保存在独立对象中。
    if (messageCache) messageCache.msgs = appendMsg(messageCache.msgs, msg)
    if (isActiveAccount) {
      const nextConversations = conversationCache?.convs ??
        updateConvList(conversationsRef.current, cid, summary, msg, isViewingConv)
      conversationsRef.current = nextConversations
      setConversations(nextConversations)
      if (isViewingConv && messageCache) {
        messagesRef.current = messageCache.msgs
        setMessages(messageCache.msgs)
        window.setTimeout(() => reloadOrdersRef.current(), 900)
      }
    }
  }, [])

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
  })

  // ==================== 加载账号列表（分页） ====================
  const loadAccounts = useCallback(async (page = 1) => {
    setLoadingAccounts(true)
    try {
      const res = await getChatAccounts(page)
      if (page === 1) {
        setAccounts(res.data)
      } else {
        setAccounts((prev) => [...prev, ...res.data])
      }
      setAccountPage(page)
      setAccountHasMore(res.hasMore)
    } catch (e: any) {
      addToast({ message: e.message || '获取账号列表失败', type: 'error' })
    } finally {
      setLoadingAccounts(false)
    }
  }, [addToast])

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
  const loadConversations = useCallback(async (accountId: string, append = false, merge = false) => {
    if (!accountId) return
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

    const fetchPage = async (cursor?: number) => {
      const page = await getConversations(accountId, cursor)
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
          otherUserName: !conversation.otherUserName || isPureDigits(conversation.otherUserName)
            ? info.nick || conversation.otherUserName : conversation.otherUserName }
      })
      const cached = convsCacheRef.current[accountId]
      const next: ConversationCache = {
        convs: append || compensating ? mergeConversations(latest, cached?.convs ?? []) : latest,
        hasMore: result.hasMore, cursor: result.cursor,
      }
      convsCacheRef.current[accountId] = next
      if (result.complete) recoveryRef.current.conversations.delete(accountId)
      else if (boundary) boundary.nextCursor = result.cursor
      if (isActive()) {
        conversationsRef.current = next.convs
        convCursorRef.current = next.cursor
        setConversations(next.convs)
        setConvHasMore(next.hasMore)
        setConvCursor(next.cursor)
      }
      if (!result.complete) {
        addToast({ message: `账号 ${accountId} 会话较多，请点击加载更多继续补齐`, type: 'warning' })
      }
      if (!append && isActive() && latest[0]?.cid) {
        getAccountProfile(accountId, latest[0].cid).then((profile) => {
          if (profile.nick) setAccounts((prev) => prev.map((account) => (
            account.account_id === accountId ? { ...account, display_name: profile.nick } : account
          )))
        }).catch((error: Error) => addToast({ message: error.message, type: 'error' }))
      }
    }).catch((error: Error) => {
      if (isCurrent()) addToast({ message: error.message, type: 'error' })
    }).finally(() => {
      if (requestId === convRequestIdRef.current[accountId] && isActive()) setLoadingConvs(false)
    })
  }, [addToast])

  // 用 ref 保持 accounts 最新引用，避免 effect 闭包取到旧值
  const accountsRef = useRef(accounts)
  useEffect(() => { accountsRef.current = accounts }, [accounts])

  // ==================== 加载用户信息（头像+昵称） ====================
  // 找出缺少头像或昵称的会话，构建 [{userId, cid}] 查询列表
  const missingInfoConvs = conversations.filter(
    (c) => {
      if (!c.otherUserId || !c.cid) return false
      // 昵称有效（非空且非纯数字）且头像存在时无需查询
      const hasValidName = !!c.otherUserName && !isPureDigits(c.otherUserName)
      if (c.otherUserAvatar && hasValidName) return false
      // 缓存中已有完整信息则跳过
      const cached = userInfoCacheRef.current[c.otherUserId]
      if (cached && cached.avatar && cached.nick) return false
      return true
    },
  )
  // 以 userId 去重后构建依赖 key
  const missingInfoKey = [...new Set(missingInfoConvs.map((c) => c.otherUserId))].sort().join(',')

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
          // 昵称为空或纯数字时用 API 返回的昵称覆盖
          if (info.nick && (!c.otherUserName || isPureDigits(c.otherUserName))) updates.otherUserName = info.nick
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
          if (!cancelled && infos && Object.keys(infos).length > 0) {
            applyInfos(infos)
          }
        } catch {
          // 单批失败不影响后续批次
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
      if (!result.complete) {
        addToast({ message: '断线期间消息较多，请点击加载更多继续补齐', type: 'warning' })
      }
    }).catch((error: Error) => {
      if (isCurrent()) addToast({ message: error.message, type: 'error' })
    }).finally(() => {
      if (requestId === msgRequestIdRef.current) setLoadingMsgs(false)
    })
  }, [addToast])

  const loadMessages = useCallback(async (accountId: string, cid: string, append = false) => {
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
    setLoadingMsgs(true)
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
      }).catch((error: Error) => {
        if (isCurrent()) addToast({ message: error.message, type: 'error' })
      }).finally(() => {
        if (requestId === msgRequestIdRef.current) setLoadingMsgs(false)
      })
  }, [addToast, catchUpActiveConversation])

  // 边界已在断线时冻结，等待会话补偿期间收到的实时消息不会推进消息补偿终点。
  const reloadCurrentChat = useCallback(async (accountId: string) => {
    await loadConversations(accountId, false, true)
    if (accountId !== activeAccountIdRef.current) return
    const cid = activeCidRef.current
    if (cid) await catchUpActiveConversation(accountId, cid)
  }, [catchUpActiveConversation, loadConversations])

  useEffect(() => {
    reloadCurrentChatRef.current = reloadCurrentChat
  }, [reloadCurrentChat])

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
    if (!cached || recoveryRef.current.conversations.has(accountId)) {
      void loadConversations(accountId, false, !!cached)
    }
    const cid = activeConvPerAccountRef.current[accountId] || ''
    selectCid(cid)
    restoreConversation(accountId, cid)
  }, [activeAccountId]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSelectConversation = (cid: string) => {
    selectCid(cid)
    setMobileTab('chat')
    setConversations((prev) => prev.map((conversation) =>
      conversation.cid === cid ? { ...conversation, unreadCount: 0 } : conversation,
    ))
    restoreConversation(activeAccountId, cid)
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
    } catch (e: any) {
      addToast({ message: e.message || '无物流发货失败', type: 'error' })
    } finally {
      setConfirmingOrderNo('')
    }
  }

  const handleCancelOrder = async (orderNo: string) => {
    if (!(await requestConfirm({ message: `确认取消客户订单 ${orderNo} 吗？取消后无法恢复。`, confirmText: '取消订单', type: 'danger' }))) return
    setCancellingOrderNo(orderNo)
    try {
      const res = await cancelOrder(orderNo)
      addToast({ message: res.message || (res.success ? '订单已取消' : '取消订单失败'), type: res.success ? 'success' : 'error' })
      await loadCustomerOrders(true)
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
              ? { ...c, lastMessageSummary: text.slice(0, 50), lastMessageTime: sentMsg.time }
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
              ? { ...c, lastMessageSummary: '[图片]', lastMessageTime: sentMsg.time }
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
              ? { ...c, lastMessageSummary: '[语音]', lastMessageTime: sentMsg.time }
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

      <div className="flex flex-1 min-h-0 flex-col md:flex-row gap-3">
      {/* 左侧：账号列表 */}
      <div className={`${mobileTab === 'accounts' ? 'flex' : 'hidden'} md:flex w-full md:w-56 flex-shrink-0 bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 flex-col min-h-0`}>
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

      {/* 中间：会话列表 */}
      <div className={`${mobileTab === 'convs' ? 'flex' : 'hidden'} md:flex w-full md:w-72 flex-shrink-0 bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 flex-col min-h-0`}>
        <div className="p-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <span className="font-medium text-sm text-gray-700 dark:text-gray-300">会话列表</span>
          <div className="flex items-center gap-1">
            <SoundPicker />
            {activeAccountId && (
              <button
                onClick={() => { setConvCursor(null); loadConversations(activeAccountId) }}
                className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                title="刷新会话"
              >
                <RefreshCw className={`w-4 h-4 text-gray-500 ${loadingConvs ? 'animate-spin' : ''}`} />
              </button>
            )}
          </div>
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
          ) : (
            <>
              {conversations.map((conv, idx) => (
                <div
                  key={conv.cid || `conv-${idx}`}
                  className={`px-3 py-2.5 cursor-pointer border-b border-gray-100 dark:border-gray-700/50 transition-colors ${
                    activeCid === conv.cid
                      ? 'bg-blue-50 dark:bg-blue-900/20'
                      : 'hover:bg-gray-50 dark:hover:bg-gray-700/30'
                  }`}
                  onClick={() => handleSelectConversation(conv.cid)}
                >
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
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">
                          {conv.otherUserName || conv.otherUserId || '未知用户'}
                        </span>
                        <span className="text-xs text-gray-400 flex-shrink-0 ml-2">
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
                          {conv.lastMessageSummary || '暂无消息'}
                        </span>
                        {conv.unreadCount > 0 && (
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
          </div>
          {activeCid && (
            <button onClick={handleBlacklistCustomer} disabled={blacklisting} className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded border border-red-200 text-red-500 hover:bg-red-50 disabled:opacity-40" title={isOfficiallyBlocked ? '解除闲鱼官方黑名单' : '加入闲鱼官方黑名单'}>
              {blacklisting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Ban className="w-3.5 h-3.5" />}
              {isOfficiallyBlocked ? '解除黑名单' : '加入黑名单'}
            </button>
          )}
        </div>

        {/* 名片 + 会话商品：桌面端左右并排省一行高度，窄屏自动回退上下堆叠 */}
        <div className="flex flex-col md:flex-row items-stretch border-b border-gray-200 dark:border-gray-700">
          {/* 对方名片条：闲鱼主页信息直接摆在会话里，点开可看完整主页 */}
          {activeConversation?.otherUserId && peerProfile && (
            <button
              onClick={() => openXianyuProfile(
                activeConversation.otherUserId,
                peerProfile.nick || activeConversation.otherUserName,
                peerProfile.avatar || activeConversation.otherUserAvatar,
              )}
              title="点击查看闲鱼个人主页"
              className="flex-1 min-w-0 flex items-center gap-3 px-3 py-2 md:border-r border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors text-left"
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
                    {peerProfile.nick || activeConversation.otherUserName || '未知用户'}
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
                <div className="flex items-center gap-1.5 mt-0.5 text-[11px] text-gray-400 min-w-0">
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
              title="点击查看闲鱼商品页"
              className={cn(
                'flex-shrink-0 md:w-64 flex items-center gap-2 px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/40 transition-colors text-left',
                !(activeConversation?.otherUserId && peerProfile) && 'flex-1 md:w-auto',
              )}
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
                    {/* 发送者名称 */}
                    <div
                      className={`text-xs text-gray-400 mb-1 ${
                        msg.isSelf ? 'text-right' : 'text-left'
                      }`}
                    >
                      {msg.senderName}
                      <span className="ml-2">{formatTime(msg.time)}</span>
                    </div>
                    {/* 消息气泡（失败的本地消息在气泡前显示红色感叹号，点击查看原因） */}
                    <div className={`flex items-center gap-1.5 ${msg.isSelf ? 'flex-row-reverse' : ''}`}>
                      <div
                        className={`rounded-lg px-3 py-2 text-sm break-words ${
                          msg.isSelf
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
                        ) : (
                          <span className="whitespace-pre-wrap">{msg.text}</span>
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
              <button
                type="button"
                onClick={isRecording ? stopRecording : startRecording}
                disabled={sending && !isRecording}
                title={isRecording ? '点击停止录音' : '点击开始录音，再点一次结束，然后选择转文字或发送语音'}
                className={cn(
                  'flex-shrink-0 flex items-center justify-center w-9 h-9 border rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed',
                  isRecording
                    ? 'text-white bg-red-500 border-red-500 hover:bg-red-600'
                    : 'text-gray-500 dark:text-gray-300 border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700'
                )}
              >
                {isRecording ? <Square className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
              </button>
            </div>
          </div>
        )}
      </div>
      {/* 图片预览弹窗 */}
      {/* 右侧工作区：客户订单 + 快捷短语 */}
      <div className={`${mobileTab === 'tools' ? 'flex' : 'hidden'} md:flex w-full md:w-80 flex-shrink-0 min-h-0 flex-col gap-3 overflow-y-auto md:overflow-visible`}>
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
