/**
 * 在线聊天 WebSocket Hook
 *
 * 管理与后端的多账号 WebSocket 连接，接收 IM 实时推送消息并更新到前端状态。
 * 每个已连接账号维护一条独立的 WebSocket，支持自动重连和心跳保活。
 */
import { useEffect, useRef, useCallback, useMemo } from 'react'
import { createChatNewWebSocket, type WsPushMessage, type ChatMessage } from '@/api/chatNew'

/** 心跳间隔（毫秒） */
const HEARTBEAT_INTERVAL = 20000
/** 重连延迟（毫秒） */
const RECONNECT_DELAY = 3000
/** 连续离线时的重试间隔上限（毫秒） */
const MAX_RECONNECT_DELAY = 30000

/** 单个账号的 WebSocket 连接状态 */
interface WsConnection {
  ws: WebSocket
  heartbeat: ReturnType<typeof setInterval> | null
  reconnect: ReturnType<typeof setTimeout> | null
  closed: boolean
}

interface UseChatNewWsOptions {
  /** 所有已连接的账号ID列表，每个都会建立独立的 WebSocket 连接 */
  accountIds: string[]
  /** 收到新消息时的回调（包含来源账号ID） */
  onNewMessage: (accountId: string, cid: string, msg: ChatMessage) => void
  /** WebSocket 连接成功时的回调（包含首次建连和重连） */
  onConnected?: (accountId: string, isReconnect: boolean) => void
  /** WebSocket 断连时的回调 */
  onDisconnect?: (accountId: string) => void
  /** 后端 IM 断线自动重连成功时的回调（断线期间的消息不补推，前端需自行补拉） */
  onReconnected?: (accountId: string) => void
  /** 「对方正在输入」状态变化（typing=false 表示停止输入） */
  onTyping?: (accountId: string, cid: string, typing: boolean) => void
  /** 「对方已读」回执：IM 长连接推来被读消息的 ID 列表，readAt 为回执时间戳（毫秒） */
  onPeerRead?: (accountId: string, cid: string, messageIds: string[], readAt: number) => void
  /** 处于风控冷却、不应订阅实时通道的账号 ID 列表（后端已不会为其建连，连了也是 4404 噪声） */
  blockedAccountIds?: string[]
}

/**
 * 在线聊天(新) 多账号 WebSocket 连接 Hook
 *
 * 根据 accountIds 列表自动管理每个账号的 WebSocket 连接。
 * 新增账号自动建连，移除的账号自动断开，已有账号保持不变。
 */
export function useChatNewWs({
  accountIds,
  onNewMessage,
  onConnected,
  onDisconnect,
  onReconnected,
  onTyping,
  onPeerRead,
  blockedAccountIds,
}: UseChatNewWsOptions) {
  const connectionsRef = useRef<Map<string, WsConnection>>(new Map())
  const onNewMessageRef = useRef(onNewMessage)
  const onConnectedRef = useRef(onConnected)
  const onDisconnectRef = useRef(onDisconnect)
  const onReconnectedRef = useRef(onReconnected)
  const onTypingRef = useRef(onTyping)
  const onPeerReadRef = useRef(onPeerRead)

  // 保持回调引用最新
  useEffect(() => { onNewMessageRef.current = onNewMessage }, [onNewMessage])
  useEffect(() => { onConnectedRef.current = onConnected }, [onConnected])
  useEffect(() => { onDisconnectRef.current = onDisconnect }, [onDisconnect])
  useEffect(() => { onReconnectedRef.current = onReconnected }, [onReconnected])
  useEffect(() => { onTypingRef.current = onTyping }, [onTyping])
  useEffect(() => { onPeerReadRef.current = onPeerRead }, [onPeerRead])

  /** 清理单个账号的连接 */
  const cleanupAccount = useCallback((aid: string) => {
    const conn = connectionsRef.current.get(aid)
    if (!conn) return
    conn.closed = true
    if (conn.heartbeat) clearInterval(conn.heartbeat)
    if (conn.reconnect) clearTimeout(conn.reconnect)
    conn.ws.onopen = null
    conn.ws.onmessage = null
    conn.ws.onerror = null
    conn.ws.onclose = null
    if (conn.ws.readyState === WebSocket.OPEN || conn.ws.readyState === WebSocket.CONNECTING) {
      conn.ws.close()
    }
    connectionsRef.current.delete(aid)
  }, [])

  /** 为指定账号建立 WebSocket 连接 */
  const connectAccount = useCallback((aid: string, isReconnect = false, retryAttempt = 0) => {
    cleanupAccount(aid)

    const ws = createChatNewWebSocket(aid)
    const conn: WsConnection = { ws, heartbeat: null, reconnect: null, closed: false }
    connectionsRef.current.set(aid, conn)

    ws.onopen = () => {
      conn.heartbeat = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, HEARTBEAT_INTERVAL)
    }

    ws.onmessage = (event) => {
      try {
        const data: WsPushMessage = JSON.parse(event.data)
        if (data.event === 'new_message' && data.cid && data.message) {
          onNewMessageRef.current(aid, data.cid, data.message)
        } else if (data.event === 'connected') {
          // 只有 IM 订阅确认成功才重置退避；握手成功后仍可能收到 4404。
          retryAttempt = 0
          onConnectedRef.current?.(aid, isReconnect)
        } else if (data.event === 'typing' && data.cid) {
          // 对方正在输入（1/0 交替推送，前端自行做超时隐藏）
          onTypingRef.current?.(aid, data.cid, !!data.typing)
        } else if (data.event === 'peer_read' && data.cid) {
          // 真实已读回执：闲鱼把对方读掉的消息 ID 推给发送方，这是
          // APP 气泡下「已读/未读」的唯一数据源（历史接口里给不了实时值）。
          onPeerReadRef.current?.(
            aid,
            data.cid,
            data.message_ids ?? [],
            data.read_at ?? 0,
          )
        } else if (data.event === 'reconnected') {
          // 后端 IM 长连接掉过线又自动连回来了。掉线期间到达的消息服务端不补推，
          // 这里主动补拉一次，否则这些消息要等下次刷新页面才可见。
          onReconnectedRef.current?.(aid)
        }
      } catch {
        // 解析失败，忽略
      }
    }

    ws.onerror = () => {}

    ws.onclose = (event) => {
      if (conn.heartbeat) {
        clearInterval(conn.heartbeat)
        conn.heartbeat = null
      }
      if (!conn.closed && onDisconnectRef.current) {
        onDisconnectRef.current(aid)
      }
      // 鉴权失败（4401 未认证 / 4403 无权限）为终止性关闭，不再重连，避免请求风暴
      const isAuthFailure = event.code === 4401 || event.code === 4403
      if (isAuthFailure) {
        conn.closed = true
        return
      }
      // 临时离线保留订阅并退避重连；主动断开或卸载时由 cleanupAccount 取消。
      if (!conn.closed) {
        const retryDelay = Math.min(RECONNECT_DELAY * 2 ** retryAttempt, MAX_RECONNECT_DELAY)
        conn.reconnect = setTimeout(() => {
          if (!conn.closed) connectAccount(aid, true, Math.min(retryAttempt + 1, 4))
        }, retryDelay)
      }
    }
  }, [cleanupAccount])

  // 风控冷却中的账号不订阅实时通道：后端已不会为其建立 IM 连接，前端再连只会产生
  // 无意义的 4404 重连噪声；“被挑战即停手”也要求避免任何多余动作。
  const blockedSet = useMemo(() => new Set(blockedAccountIds ?? []), [blockedAccountIds])
  const effectiveAccountIds = useMemo(
    () => accountIds.filter((id) => !blockedSet.has(id)),
    [accountIds, blockedSet],
  )

  // 根据 effectiveAccountIds 变化增量管理连接（只增减差异，不断开已有连接）
  const effectiveKey = effectiveAccountIds.join(',')
  useEffect(() => {
    const currentIds = new Set(effectiveAccountIds)
    const existingIds = new Set(connectionsRef.current.keys())

    // 新增的账号 → 建连
    for (const aid of currentIds) {
      if (!existingIds.has(aid)) {
        connectAccount(aid)
      }
    }
    // 移除的账号 → 断开
    for (const aid of existingIds) {
      if (!currentIds.has(aid)) {
        cleanupAccount(aid)
      }
    }
  }, [effectiveKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // 组件卸载时清理所有连接
  useEffect(() => {
    return () => {
      for (const aid of Array.from(connectionsRef.current.keys())) {
        cleanupAccount(aid)
      }
    }
  }, [cleanupAccount])
}
