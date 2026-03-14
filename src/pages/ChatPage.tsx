import { useState, useCallback, useEffect, useRef } from 'react'
import ChatSidebar from '../components/ChatSidebar'
import ChatWindow from '../components/ChatWindow'
import ModelRoutingSettings from '../components/ModelRoutingSettings'
import SidePanel from '../components/SidePanel'
import {
  loadChats,
  saveChats,
  createNewChat,
  deriveChatTitle,
} from '../session'
import type { StoredChat, DisplayMessage } from '../types'

interface ChatPageProps {
  onBack: () => void
}

function initState(): { chats: StoredChat[]; activeChatId: string } {
  const loaded = loadChats()
  if (loaded.length > 0) return { chats: loaded, activeChatId: loaded[0].id }
  const first = createNewChat()
  return { chats: [first], activeChatId: first.id }
}

export default function ChatPage({ onBack }: ChatPageProps) {
  const init = useRef(initState())

  const [chats, setChats] = useState<StoredChat[]>(init.current.chats)
  const [activeChatId, setActiveChatId] = useState<string>(init.current.activeChatId)

  const handleSelectChat = useCallback((chatId: string) => setActiveChatId(chatId), [])

  const handleNewChat = useCallback(() => {
    const chat = createNewChat()
    setChats((prev) => [chat, ...prev])
    setActiveChatId(chat.id)
  }, [])

  const handleImportChats = useCallback((newChats: StoredChat[]) => {
    setChats((prev) => {
      const existingIds = new Set(prev.map((c) => c.id))
      const fresh = newChats.filter((c) => !existingIds.has(c.id))
      if (fresh.length === 0) return prev
      return [...fresh, ...prev].slice(0, 50)
    })
    if (newChats.length > 0) setActiveChatId(newChats[0].id)
  }, [])

  const handleDeleteChat = useCallback((chatId: string) => {
    setChats((prev) => {
      const next = prev.filter((c) => c.id !== chatId)
      return next
    })
    setActiveChatId((prev) => {
      if (prev !== chatId) return prev
      const next = chats.filter((c) => c.id !== chatId)
      return next[0]?.id ?? ''
    })
  }, [chats])

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)
  const [routingOpen, setRoutingOpen] = useState(false)

  const closeSidebar = () => setSidebarOpen(false)
  const closePanel   = () => setPanelOpen(false)

  useEffect(() => {
    saveChats(chats)
  }, [chats])

  const activeChat = chats.find((c) => c.id === activeChatId) ?? null

  const handleUpdateChat = useCallback(
    (
      chatId: string,
      messages: DisplayMessage[],
      sessionId?: string,
      checkpointData?: { index: number; summary: string },
    ) => {
      setChats((prev) => {
        const now = Date.now()
        const target = prev.find((c) => c.id === chatId)
        if (!target) return prev
        const sessionRenewed = sessionId != null && sessionId !== target.sessionId
        const updated: StoredChat = {
          ...target,
          messages,
          title: deriveChatTitle(messages),
          sessionId: sessionId ?? target.sessionId,
          updatedAt: now,
          lastActiveAt: now,
          ...(sessionRenewed && {
            checkpointIndex: undefined,
            checkpointSummary: undefined,
            checkpointCreatedAt: undefined,
          }),
          ...(checkpointData && {
            checkpointIndex: checkpointData.index,
            checkpointSummary: checkpointData.summary,
            checkpointCreatedAt: now,
          }),
        }
        return [updated, ...prev.filter((c) => c.id !== chatId)]
      })
    },
    [],
  )

  return (
    <div className="absolute inset-0 z-50" style={{ background: 'var(--bg-base)' }}>
      <style>{CHAT_PAGE_STYLES}</style>

      {(sidebarOpen || panelOpen) && (
        <div
          className="overlay-mask"
          onClick={() => { closeSidebar(); closePanel() }}
        />
      )}

      <div className="app-shell">
        <div className={`sidebar-wrap${sidebarOpen ? ' is-open' : ''}`}>
          <ChatSidebar
            chats={chats}
            activeChatId={activeChatId}
            onClose={closeSidebar}
            onSelectChat={(id) => { handleSelectChat(id); closeSidebar() }}
            onNewChat={handleNewChat}
            onDeleteChat={handleDeleteChat}
            onBack={onBack}
          />
        </div>

        <ChatWindow
          onOpenSidebar={() => setSidebarOpen(true)}
          onOpenPanel={() => setPanelOpen(true)}
          activeChat={activeChat}
          onUpdateChat={handleUpdateChat}
        />

        <div className={`panel-wrap${panelOpen ? ' is-open' : ''}`}>
          <SidePanel
            activeChat={activeChat}
            onClose={closePanel}
            onOpenModelRouting={() => { closePanel(); setRoutingOpen(true) }}
            onImportChats={handleImportChats}
          />
        </div>
      </div>

      <ModelRoutingSettings
        open={routingOpen}
        onClose={() => setRoutingOpen(false)}
      />
    </div>
  )
}

const CHAT_PAGE_STYLES = `
.app-shell {
  display: flex;
  height: 100dvh;
  overflow: hidden;
  background: var(--bg-base);
}

.overlay-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  z-index: 40;
}

.sidebar-wrap {
  flex: 0 0 var(--sidebar-w);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-subtle);
}

.panel-wrap {
  flex: 0 0 var(--panel-w);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-surface);
  border-left: 1px solid var(--border-subtle);
}

@media (max-width: 767px) {
  .sidebar-wrap {
    position: fixed;
    top: 0; left: 0; bottom: 0;
    width: min(82vw, 320px);
    z-index: 50;
    transform: translateX(-100%);
    transition: transform 0.28s var(--ease-out);
    box-shadow: 4px 0 24px rgba(0,0,0,0.4);
  }
  .sidebar-wrap.is-open { transform: translateX(0); }

  .panel-wrap {
    position: fixed;
    top: 0; right: 0; bottom: 0;
    width: min(82vw, 320px);
    z-index: 50;
    transform: translateX(100%);
    transition: transform 0.28s var(--ease-out);
    box-shadow: -4px 0 24px rgba(0,0,0,0.4);
  }
  .panel-wrap.is-open { transform: translateX(0); }
}
`
