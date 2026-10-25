/**
 * 在线聊天(新) API接口
 *
 * 提供账号连接管理、会话列表、聊天记录等接口，
 * 以及 WebSocket 实时消息推送连接
 */
import { del, get, post, put } from '@/utils/request'

const PREFIX = '/api/v1/chat-new'


// ==================== 类型定义 ====================

/** 可用账号 */
export interface ChatAccount {
  account_id: string
  display_name: string
  remark: string
  connected: boolean
  /** 是否持有 IM 客户端对象（已连接 或 已掉线但可自动恢复）。
   *  即使 connected=false，只要 has_client=true 前端也应订阅实时通道，
   *  以便账号自动重连后补拉历史并响铃（断线也响）。 */
  has_client?: boolean
  status: string
  /** 网页 Cookie 会话状态推断结果（normal/risk_control/expired/disabled） */
  cookie_status?: 'normal' | 'risk_control' | 'expired' | 'disabled'
  last_login_at?: string | null
  last_refresh_at?: string | null
  /** 所属用户（管理员查看时返回） */
  owner?: string
}

export interface CustomerOrder {
  order_no: string
  item_id: string
  item_title: string
  buyer_id: string
  quantity: number
  amount: string
  status: string
  delivery_method: string
  delivery_fail_reason: string
  card_only_delivered: boolean
  placed_at: string
}

export interface QuickPhrase {
  id: number
  title: string
  content: string
  sort_order: number
}

/** 会话 */
export interface Conversation {
  cid: string
  rawCid: string
  otherUserId: string
  otherUserName: string
  otherUserAvatar: string
  itemTitle: string
  /** 会话关联商品ID（IM extension 的 itemId），用于拼闲鱼商品页链接 */
  itemId?: string
  /** 商品主图（IM extension 的 itemMainPic） */
  itemMainPic?: string
  lastMessageSummary: string
  lastMessageTime: number
  unreadCount: number
}

/** 对方闲鱼主页信息（会话窗口的"买家名片"） */
export interface XianyuUserProfile {
  userId: string
  nick: string
  avatar: string
  /** IP属地，如"北京市" */
  ipLocation: string
  introduction: string
  /** 店铺等级，如 L5（买家没有） */
  shopLevel: string
  /** 好评率百分比，如 100 */
  praiseRatio: number | null
  /** 评价数 */
  reviewNum: number | null
  /** 信用分 */
  creditScore: number | null
  /** 是否持证经营 */
  licensed: boolean
  followers: string
  following: string
  /** 在售宝贝数 */
  itemCount: number | null
  /** 信用及评价数 */
  rateCount: number | null
  /** 认证标签：实名认证/真人认证/芝麻信用/闲鱼老用户 */
  certifications: string[]
  /** 最近活跃，如"3分钟前来过"（对方有在售商品时才有） */
  lastVisitTime?: string
  /** 24h 回复率，如"77%" */
  replyRatio24h?: string
  /** 平均回复间隔，如"1小时" */
  replyInterval?: string
}

/** 订单卡片富数据（后端从闲鱼卡片载荷解析） */
export interface OrderCardButton {
  /** 按钮文案，如"修改价格" / "去填写" / "查看发货凭证" */
  text: string
  /** 按钮动作 URL（fleamarket:// scheme 或 https H5） */
  url: string
  /** 动作类型：adjust_price / smart_form / shipping_proof / order_detail / goto_pay */
  action: string
}

export interface OrderCardShowInfo {
  /** 演出名称（含"演出名称："前缀） */
  concertName?: string
  /** 门票数量（含"门票数量："前缀） */
  ticketCount?: string
  /** 演出时间（含"演出时间："前缀） */
  date?: string
}

export interface OrderCardData {
  /** 卡片类型：placed 已拍下 / price_modified 改价待付款 / to_fill_show 待填演出信息 /
   * show_submitted 待确认演出信息 / show_confirmed 已确认演出信息 /
   * to_ship 待发货 / shipped 已发货带凭证 / shipped_notice 发货通知 */
  kind: string
  title: string
  desc: string
  /** 闲鱼订单号 */
  order_id: string
  /** 卡片按钮（无按钮为 null） */
  button: OrderCardButton | null
  /** 演出信息字段（门票类卡片有） */
  show_info: OrderCardShowInfo | null
  /** bizTag.taskName 原始分类标签 */
  task_name: string
  /** 卡片整体点击 URL */
  target_url: string
  bg_color: string
  template: string
}

/** 聊天消息 */
export interface ChatMessage {
  messageId: string
  senderId: string
  senderName: string
  isSelf: boolean
  /** 发送者是本系统托管的其它账号（自己另一个号，cid 跨账号共享推来的）：
   *  渲染上是「对方」（isSelf=false，左侧），但不响铃/不弹跨账号 toast */
  managedSender?: boolean
  type: 'text' | 'image' | 'system' | 'card' | 'audio'
  text: string
  images: string[]
  /** 语音消息：音频地址（type==='audio' 时有值） */
  audioUrl?: string
  /** 语音时长（毫秒） */
  audioDuration?: number
  /** 语音转文字结果（ASR 完成后回填） */
  audioText?: string
  /** 是否为系统 / 交易事件卡片（付款状态、演出信息、发货凭证等），
   * 前端据此渲染成居中的事件卡，而非普通聊天气泡 */
  isEvent?: boolean
  /** 事件类别（后端 classify_event 给出）：performance / refund / shipping /
   * payment / done / generic，用于事件卡图标 */
  eventKind?: string
  /** 订单卡片富数据（按钮/订单号/演出信息字段），非交易卡片为 undefined。
   * 有值时前端渲染富事件卡（与闲鱼 APP 一致的按钮 + 演出信息） */
  card?: OrderCardData
  /** 是否为「撤回」占位消息（对方/自己撤回了原消息，原位置只剩提示）。
   *  为 true 时前端渲染「对方撤回了一条消息」并带头像 */
  revoked?: boolean
  /** 撤回前原文线索（reminderContent 旧摘要，可能为空）：
   *  文本消息即原文，图片/语音为 [图片] 之类占位。用户点「显示」才展开 */
  revokedOriginal?: string
  /** 引用消息（回复）：被引用的原消息。来源两种——
   *  1. IM 协议自带引用字段时由后端解析填充（真引用，尚未落地）；
   *  2. 本端发出的伪引用，由「回复 昵称：摘要」前缀文本拆出。
   *  有值时前端把气泡渲染成 APP 样式：上方引用块 + 下方正文 */
  quote?: { name: string; text: string } | null
  /** 真引用（2026-0-20 协议实证）：被引用消息的 IM messageId。
   *  由 message.extension.replyMessageId 解析而来（历史/推送两条路径都有）。
   *  有值时正文是纯文本，前端按本字段在本地消息列表找原消息渲染引用块；
   *  找不到原消息时降级为普通文本。与 quote 字段等价，优先用本字段 */
  replyToMessageId?: string
  time: number
  /** ⚠️ 已废弃，不要再用它判断「对方是否已读」！
   *  IM 原始字段，真实语义是「**当前登录账号**有没有读过这条消息」（0=未读、2=已读），
   *  与发送方无关——2026-10-20 双向交叉验证：同一批消息发送方视角=2、
   *  从未打开的接收方视角=0。对自己发出的消息恒为 2，用它显示会永远「已读」。
   *  判「对方是否已读我发的消息」请用下面的 peerUnread */
  readStatus?: number
  /** 对方是否已读我发出的这条消息。
   *  ⚠️ 后端给的是 message.unreadCount，它**不是实时回执**，而是这条消息
   *  **发出那一刻**的快照（v1.0.54 用它当判据，当晚被实测证伪：买家连回 5 条
   *  之后数据纹丝不动）。所以它只能当初始值：
   *  - 0   → 对方已读
   *  - >0  → 对方未读（只是"发的时候未读"，之后可能已读但这里不会变）
   *  - undefined / null → 暂无数据（推送副本与本端乐观副本都没有），不显示标记
   *  真正的实时开关是 WS 事件 peer_read（IM 长连接推来的已读回执），
   *  由 ChatNew.tsx 的 handleWsPeerRead 落到本地、在渲染层强制覆盖本字段。 */
  peerUnread?: number | null
  /** 发送是否失败（仅本地发送的消息可能为 true） */
  failed?: boolean
  /** 发送失败原因（如被安全拦截的明文文案），用于点击感叹号查看 */
  failReason?: string
  /** 推送来源标记：'app_channel' = App 通道实时推送。它的 messageId 与
   * IM 网关历史接口是两套 ID 体系，前端合并时据此做内容级去重 */
  pushSource?: string
}

// ==================== 接口方法 ====================

/** 账号列表分页响应 */
export interface ChatAccountsPage {
  data: ChatAccount[]
  total: number
  hasMore: boolean
}

/** 获取账号列表（分页） */
export const getChatAccounts = async (page = 1, pageSize = 20): Promise<ChatAccountsPage> => {
  const res = await get<{ success: boolean; data: ChatAccount[]; total: number; hasMore: boolean }>(
    `${PREFIX}/accounts?page=${page}&page_size=${pageSize}`,
  )
  return { data: res.data || [], total: res.total || 0, hasMore: res.hasMore ?? false }
}

/** 获取当前用户可访问且后端 IM 已连接的账号 ID，用于刷新后自动恢复实时消息订阅 */
export const getConnectedChatAccountIds = async (): Promise<string[]> => {
  const res = await get<{
    success: boolean
    message?: string
    data?: { account_ids: string[] }
  }>(`${PREFIX}/connected-accounts`)
  if (!res.success) throw new Error(res.message || '获取已连接账号失败')
  return res.data?.account_ids || []
}

/** 连接指定账号的IM */
export const connectAccount = (accountId: string) => {
  return post<{ success: boolean; message: string }>(`${PREFIX}/connect/${accountId}`)
}

/** 断开指定账号的IM */
export const disconnectAccount = (accountId: string) => {
  return post<{ success: boolean; message: string }>(`${PREFIX}/disconnect/${accountId}`)
}

/** 获取会话列表 */
export const getConversations = async (
  accountId: string,
  cursor?: number,
  limit: number = 20,
): Promise<{ conversations: Conversation[]; hasMore: boolean; nextCursor: number | null; rateLimited?: boolean; retryAfter?: number }> => {
  const params = new URLSearchParams()
  if (cursor !== undefined && cursor !== null) params.append('cursor', String(cursor))
  params.append('limit', String(limit))

  const res = await get<{
    success: boolean
    message?: string
    data: { conversations: Conversation[]; hasMore: boolean; nextCursor: number | null; rateLimited?: boolean; retryAfter?: number }
  }>(`${PREFIX}/conversations/${accountId}?${params.toString()}`)
  if (!res.success) throw new Error(res.message || '获取会话列表失败')
  const data = res.data || { conversations: [], hasMore: false, nextCursor: null }
  // 后端 hasMore 可能返回数字 0/1，强制转为布尔值避免 React 渲染出 "0"
  data.hasMore = !!data.hasMore
  return data
}

/** 从会话列表删除会话（仅本系统隐藏；之后对方再来消息会自动重新出现） */
export const hideConversation = async (accountId: string, cid: string): Promise<void> => {
  const res = await post<{ success: boolean; message?: string }>(
    `${PREFIX}/conversations/${accountId}/${cid}/hide`,
  )
  if (!res.success) throw new Error(res.message || '删除会话失败')
}

/** 发送文本消息
 *
 * replyToMessageId: 引用（回复）某条消息时传被引用消息的 IM messageId。
 * 协议实证（2026-010-20）：引用关系由 message.extension.replyMessageId 承载，
 * 正文仍是纯文本，对方 APP 会渲染成引用卡片。
 */
export const sendTextMessage = async (
  accountId: string,
  cid: string,
  toUserId: string,
  text: string,
  replyToMessageId = '',
): Promise<{ success: boolean; message: string; data?: { messageId: string } }> => {
  return post<{ success: boolean; message: string; data?: { messageId: string } }>(`${PREFIX}/send-message/${accountId}`, {
    cid,
    toUserId,
    text,
    replyToMessageId,
  })
}

/** 转发消息：把一条已有消息（文本/图片）原样发到另一个会话 */
export const forwardMessage = async (
  accountId: string,
  payload: {
    toCid: string
    toUserId: string
    msgType: 'text' | 'image'
    text?: string
    imageUrl?: string
    width?: number
    height?: number
  },
): Promise<{ success: boolean; message: string; data?: { messageId: string } }> => {
  return post<{ success: boolean; message: string; data?: { messageId: string } }>(
    `${PREFIX}/forward-message/${accountId}`,
    payload,
  )
}

/** 发送图片消息（上传图片文件，后端转存闲鱼CDN后发送） */
export const sendImageMessage = async (
  accountId: string,
  cid: string,
  toUserId: string,
  file: File,
): Promise<{ success: boolean; message: string; data?: { messageId: string; imageUrl: string } }> => {
  const formData = new FormData()
  formData.append('cid', cid)
  formData.append('toUserId', toUserId)
  formData.append('image', file)
  return post<{ success: boolean; message: string; data?: { messageId: string; imageUrl: string } }>(
    `${PREFIX}/send-image/${accountId}`,
    formData,
  )
}

/**
 * 发送语音消息
 * duration 为录音时长（毫秒），后端会尽量用 ffmpeg 转成 mp3 再上传闲鱼CDN
 */
export const sendAudioMessage = async (
  accountId: string,
  cid: string,
  toUserId: string,
  blob: Blob,
  durationMs: number,
): Promise<{ success: boolean; message: string; data?: { messageId: string; audioUrl: string; duration: number } }> => {
  const formData = new FormData()
  formData.append('cid', cid)
  formData.append('toUserId', toUserId)
  formData.append('duration', String(Math.round(durationMs)))
  // 给一个带扩展名的文件名，便于后端识别格式
  const ext = (blob.type || '').includes('mp4') ? 'mp4' : (blob.type || '').includes('ogg') ? 'ogg' : 'webm'
  formData.append('audio', blob, `voice.${ext}`)
  return post<{ success: boolean; message: string; data?: { messageId: string; audioUrl: string; duration: number } }>(
    `${PREFIX}/send-audio/${accountId}`,
    formData,
  )
}

/**
 * 语音转文字（ASR）
 * 二选一：传 blob（本地录音）或 audioUrl（对方发来的语音消息直链）
 */
export const transcribeAudio = async (
  accountId: string,
  blob: Blob,
): Promise<{ success: boolean; message: string; data?: { text: string; model: string } }> => {
  const formData = new FormData()
  const ext = (blob.type || '').includes('wav') ? 'wav' : (blob.type || '').includes('mp4') ? 'm4a' : 'webm'
  formData.append('audio', blob, `voice.${ext}`)
  return post<{ success: boolean; message: string; data?: { text: string; model: string } }>(
    `${PREFIX}/transcribe-audio/${accountId}`,
    formData,
  )
}

/** 把对方语音消息的直链转成文字 */
export const transcribeAudioUrl = async (
  accountId: string,
  audioUrl: string,
): Promise<{ success: boolean; message: string; data?: { text: string; model: string } }> => {
  const formData = new FormData()
  formData.append('audioUrl', audioUrl)
  return post<{ success: boolean; message: string; data?: { text: string; model: string } }>(
    `${PREFIX}/transcribe-audio/${accountId}`,
    formData,
  )
}

/** 用户信息查询结果 */
export interface UserInfoResult {
  avatar: string
  nick: string
}

/** 批量查询对方用户信息（头像+昵称，需要传会话ID用于调API） */
export const queryUserInfos = async (
  accountId: string,
  queries: { userId: string; cid: string }[],
): Promise<Record<string, UserInfoResult>> => {
  const res = await post<{ success: boolean; data: Record<string, UserInfoResult> }>(
    `${PREFIX}/avatars/${accountId}`,
    { queries },
  )
  return res.data || {}
}

export const recallMessage = (
  accountId: string,
  messageId: string,
  messageTime: number,
): Promise<{ success: boolean; message: string }> => {
  return post(`${PREFIX}/recall-message/${accountId}`, { messageId, messageTime })
}

export const getOfficialBlacklistStatus = async (
  accountId: string,
  cid: string,
): Promise<boolean> => {
  const res = await get<{ success: boolean; message?: string; data?: { blocked: boolean } }>(
    `${PREFIX}/official-blacklist/${accountId}/${encodeURIComponent(cid)}`,
  )
  if (!res.success) throw new Error(res.message || '查询黑名单状态失败')
  return !!res.data?.blocked
}

export const changeOfficialBlacklist = async (
  accountId: string,
  cid: string,
  action: 'add' | 'remove',
): Promise<{ success: boolean; message: string; data?: { blocked: boolean } }> => {
  const res = await post<{ success: boolean; message: string; data?: { blocked: boolean } }>(
    `${PREFIX}/official-blacklist/${accountId}/${encodeURIComponent(cid)}/${action}`,
  )
  if (!res.success) throw new Error(res.message || '操作失败')
  return res
}

/** 会话关联商品的详情（价格等，IM 会话数据里没有价格，需额外查） */
export interface ChatItemInfo {
  itemId: string
  title: string
  /** 现价（itemDO.soldPrice） */
  price: string
  /** 原价（itemDO.originalPrice） */
  originalPrice: string
  /** 运费（itemDO.transportFee） */
  transportFee: string
  /** 商品状态文案，如「在线」 */
  status: string
  /** 商品描述全文 */
  desc: string
  /** 商品图列表（major=true 的主图排在最前） */
  images: { url: string; major: boolean }[]
  /** 浏览量 */
  browseCnt: string
  /** 想要人数 */
  collectCnt: string
  /** 库存 */
  quantity: string
  /** 卖家ID */
  sellerId: string
  /** 卖家昵称 */
  sellerNick: string
}

/**
 * 查询会话关联商品的详情（价格/原价/运费/状态）
 * 失败时返回 null（商品条仍可用会话自带的图/标题展示，只是没有价格）
 */
export const getItemInfo = async (
  accountId: string,
  itemId: string,
): Promise<ChatItemInfo | null> => {
  try {
    const res = await get<{ success: boolean; message?: string; data: ChatItemInfo }>(
      `${PREFIX}/item-info/${encodeURIComponent(accountId)}/${encodeURIComponent(itemId)}`,
    )
    if (!res.success) return null
    return res.data || null
  } catch {
    return null
  }
}

/**
 * 获取对方的闲鱼主页信息（会话窗口的"买家名片"）
 *
 * 失败时返回 null（闲鱼未返回数据不算错误，前端静默不展示即可）
 */
export const getXianyuUserProfile = async (
  accountId: string,
  userId: string,
): Promise<XianyuUserProfile | null> => {
  try {
    const res = await get<{ success: boolean; data: XianyuUserProfile }>(
      `${PREFIX}/user-profile/${encodeURIComponent(accountId)}/${encodeURIComponent(userId)}`,
    )
    if (!res.success) return null
    return res.data || null
  } catch {
    return null
  }
}

export const getAccountProfile = async (
  accountId: string,
  cid: string,
): Promise<UserInfoResult> => {
  const res = await get<{ success: boolean; data: UserInfoResult }>(
    `${PREFIX}/account-profile/${accountId}?cid=${encodeURIComponent(cid)}`,
  )
  return res.data || { avatar: '', nick: '' }
}

/** 获取聊天记录 */
export const getMessages = async (
  accountId: string,
  cid: string,
  cursor?: number,
  limit: number = 20,
): Promise<{ messages: ChatMessage[]; hasMore: boolean; nextCursor: number | null }> => {
  const params = new URLSearchParams()
  if (cursor !== undefined && cursor !== null) params.append('cursor', String(cursor))
  params.append('limit', String(limit))

  const res = await get<{
    success: boolean
    message?: string
    data: { messages: ChatMessage[]; hasMore: boolean; nextCursor: number | null }
  }>(`${PREFIX}/messages/${accountId}/${cid}?${params.toString()}`)
  if (!res.success) throw new Error(res.message || '获取聊天记录失败')
  return res.data || { messages: [], hasMore: false, nextCursor: null }
}

/** 已读回执持久化记录（v1.0.56 起落库，刷新/重开会话后恢复「已读」标记） */
export interface PeerReadRecord {
  message_ids: string[]
  read_at: number
}

export const getPeerReads = async (
  accountId: string,
  cid: string,
): Promise<PeerReadRecord> => {
  const res = await get<{
    success: boolean
    message?: string
    data: PeerReadRecord
  }>(`${PREFIX}/peer-read/${accountId}/${cid}`)
  if (!res.success) throw new Error(res.message || '读取已读回执失败')
  return res.data || { message_ids: [], read_at: 0 }
}

export const getCustomerOrders = async (
  accountId: string,
  buyerId: string,
  chatId?: string,
): Promise<CustomerOrder[]> => {
  const params = new URLSearchParams()
  if (chatId) params.append('chat_id', chatId)
  const res = await get<{ success: boolean; message?: string; data: CustomerOrder[] }>(
    `${PREFIX}/customer-orders/${accountId}/${buyerId}?${params.toString()}`,
  )
  if (!res.success) throw new Error(res.message || '获取客户订单失败')
  return res.data || []
}

/** 会话列表的订单标记：该买家进行中订单数 + 最新一单状态（已取消/关闭/退款不计入） */
export interface CustomerOrderMark {
  count: number
  status: string
}

export const getCustomerOrderMarks = async (
  accountId: string,
  buyerIds: string[],
): Promise<Record<string, CustomerOrderMark>> => {
  if (!buyerIds.length) return {}
  const params = new URLSearchParams({ buyer_ids: buyerIds.slice(0, 200).join(',') })
  const res = await get<{ success: boolean; message?: string; data: Record<string, CustomerOrderMark> }>(
    `${PREFIX}/customer-order-marks/${accountId}?${params.toString()}`,
  )
  if (!res.success) throw new Error(res.message || '获取订单标记失败')
  return res.data || {}
}

export const getQuickPhrases = async (): Promise<QuickPhrase[]> => {
  const res = await get<{ success: boolean; data: QuickPhrase[] }>(`${PREFIX}/quick-phrases`)
  return res.data || []
}

export const createQuickPhrase = async (
  phrase: Omit<QuickPhrase, 'id'>,
): Promise<{ success: boolean; data: QuickPhrase; message: string }> => {
  const res = await post<{ success: boolean; data: QuickPhrase; message: string }>(`${PREFIX}/quick-phrases`, phrase)
  if (!res.success) throw new Error(res.message || '添加快捷短语失败')
  return res
}

export const updateQuickPhrase = async (
  id: number,
  phrase: Omit<QuickPhrase, 'id'>,
): Promise<{ success: boolean; data: QuickPhrase; message: string }> => {
  const res = await put<{ success: boolean; data: QuickPhrase; message: string }>(`${PREFIX}/quick-phrases/${id}`, phrase)
  if (!res.success) throw new Error(res.message || '更新快捷短语失败')
  return res
}

export const deleteQuickPhrase = async (id: number): Promise<{ success: boolean; message: string }> => {
  const res = await del<{ success: boolean; message: string }>(`${PREFIX}/quick-phrases/${id}`)
  if (!res.success) throw new Error(res.message || '删除快捷短语失败')
  return res
}

// ==================== 聊天图片复制 ====================

/**
 * 把任意浏览器可解码的图片 blob 转成 PNG
 *
 * 剪贴板对 image/png 的兼容性最好，微信/QQ 都能直接粘贴，
 * 所以统一先转 PNG 再写入，避免 webp/heic 等格式粘不出来。
 */
const convertImageBlobToPng = (blob: Blob): Promise<Blob> =>
  new Promise((resolve, reject) => {
    const objectUrl = URL.createObjectURL(blob)
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = img.naturalWidth || img.width
      canvas.height = img.naturalHeight || img.height
      const ctx = canvas.getContext('2d')
      if (!ctx) {
        URL.revokeObjectURL(objectUrl)
        reject(new Error('浏览器不支持图片转换'))
        return
      }
      ctx.drawImage(img, 0, 0)
      canvas.toBlob(
        (out) => {
          URL.revokeObjectURL(objectUrl)
          if (out) resolve(out)
          else reject(new Error('图片转换失败'))
        },
        'image/png',
      )
    }
    img.onerror = () => {
      URL.revokeObjectURL(objectUrl)
      reject(new Error('图片加载失败'))
    }
    img.src = objectUrl
  })

/**
 * 写入剪贴板
 *
 * 依赖 ClipboardItem，需要安全上下文（https 或 localhost）。
 * 不满足时抛出可理解的提示，由调用方提示用户改用浏览器原生菜单。
 */
const writeImageToClipboard = async (pngBlob: Blob): Promise<void> => {
  if (!navigator.clipboard || typeof ClipboardItem === 'undefined') {
    throw new Error('当前环境不支持直接复制图片，请用浏览器右键菜单自带的复制图片')
  }
  await navigator.clipboard.write([new ClipboardItem({ 'image/png': pngBlob })])
}

/**
 * 复制聊天图片到系统剪贴板（可粘贴到微信/QQ）
 *
 * 背景：闲鱼 CDN 图片不带 CORS 头，浏览器直接 fetch 会被同源策略拦掉，
 *       所以统一走后端 /chat-new/proxy-image 拿同源数据再复制。
 *
 * @param imageUrl 图片地址（闲鱼 CDN 地址，或本地 blob:/data: 临时地址）
 * @throws 失败时抛出可直接展示给用户的文案
 */
export const copyImageToClipboard = async (
  imageUrl: string,
): Promise<{ success: boolean; message: string }> => {
  // 本地临时地址（刚发送、还没拿到 CDN 地址的预览图）可直接读取，无需代理
  const isLocal = imageUrl.startsWith('blob:') || imageUrl.startsWith('data:')
  const target = isLocal
    ? imageUrl
    : `${PREFIX}/proxy-image?url=${encodeURIComponent(imageUrl)}`

  const token = localStorage.getItem('auth_token') || ''
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`

  const resp = await fetch(target, { headers })
  // 后端失败时返回的是业务 JSON（HTTP 仍为 200），需要单独识别
  const contentType = resp.headers.get('Content-Type') || ''
  if (!contentType.startsWith('image/')) {
    const data = await resp.json().catch(() => null)
    throw new Error(data?.message || '图片获取失败')
  }
  const blob = await resp.blob()
  if (!blob.size) throw new Error('图片内容为空')

  const pngBlob = await convertImageBlobToPng(blob)
  await writeImageToClipboard(pngBlob)
  return { success: true, message: '图片已复制，可直接粘贴到微信/QQ' }
}

// ==================== WebSocket 实时推送 ====================

/** WebSocket 推送消息（新消息事件） */
export interface WsPushMessage {
  event: 'new_message' | 'connected' | 'pong' | 'reconnected' | 'typing' | 'peer_read'
  cid?: string
  account_id?: string
  message?: ChatMessage
  /** event==='typing' 时有效：true=对方正在输入，false=停止输入 */
  typing?: boolean
  /** event==='peer_read' 时有效：被对方读掉的消息 ID 列表（IM messageId） */
  message_ids?: string[]
  /** event==='peer_read' 时有效：回执时间戳（毫秒） */
  read_at?: number
}

/**
 * 获取在线聊天(新) WebSocket 基础地址
 *
 * 使用当前页面同源地址，通过代理转发到backend-web：
 * - 开发环境：Vite proxy (ws:true) 代理到 localhost:8089
 * - 生产环境：Nginx 反代到 backend-web:8089
 */
function getChatNewWsBaseUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  return `${protocol}//${host}`
}

/**
 * 创建在线聊天(新) WebSocket 连接
 *
 * 连接后会实时接收 IM 推送的新消息
 * 通过查询参数携带登录令牌进行鉴权（浏览器 WebSocket 无法设置请求头）
 */
export function createChatNewWebSocket(accountId: string): WebSocket {
  const wsBase = getChatNewWsBaseUrl()
  const token = localStorage.getItem('auth_token') || ''
  const url = `${wsBase}/api/v1/chat-new/ws/${encodeURIComponent(accountId)}?token=${encodeURIComponent(token)}`
  return new WebSocket(url)
}

export interface MessageSearchHit {
  accountId: string
  /** 账号完整昵称（如「樱花飞舞4321」，后端从 xy_accounts 带出） */
  accountName?: string
  cid: string
  messageId: string
  senderName: string
  isSelf: boolean
  managedSender?: boolean
  type: string
  text: string
  time: number
}

/** 聊天记录全文搜索（本地归档库，零闲鱼请求） */
export const searchChatMessages = async (
  q: string,
  accountId?: string,
  limit = 30,
): Promise<MessageSearchHit[]> => {
  const params = new URLSearchParams({ q, limit: String(limit) })
  if (accountId) params.append('account_id', accountId)
  const res = await get<{
    success: boolean
    message?: string
    data: { messages: MessageSearchHit[]; keyword: string }
  }>(`${PREFIX}/search-messages?${params.toString()}`)
  if (!res.success) throw new Error(res.message || '搜索失败')
  return res.data?.messages ?? []
}
