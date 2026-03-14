import { useState } from 'react';
import HomePage from './pages/HomePage';
import ChatPage from './pages/ChatPage';
import WorldbookPage from './pages/WorldbookPage';
import './index.css';

type AppId = 'chat' | 'memory' | 'settings' | 'worldbook';

// Placeholder overlay — will be replaced with real pages later
function PlaceholderPage({ title, onBack }: { title: string; onBack: () => void }) {
  return (
    <div className="absolute inset-0 z-50 flex flex-col" style={{ background: '#1c1c1e' }}>
      <div className="flex items-center gap-3 px-4 pt-14 pb-4">
        <button
          onClick={onBack}
          className="w-8 h-8 rounded-full flex items-center justify-center text-white"
          style={{ background: 'rgba(255,255,255,0.15)' }}
        >
          <span style={{ transform: 'translateY(-1px)', display: 'block' }}>‹</span>
        </button>
        <span className="text-white text-lg font-medium">{title}</span>
      </div>
      <div className="flex-1 flex items-center justify-center">
        <p className="text-white/40 text-sm">Coming soon</p>
      </div>
    </div>
  );
}

export default function App() {
  const [activeApp, setActiveApp] = useState<AppId | null>(null);

  const appTitles: Record<AppId, string> = {
    chat: '聊天',
    memory: '記憶室',
    settings: '設定',
    worldbook: '世界書',
  };

  return (
    <div className="relative w-full h-full overflow-hidden" style={{ background: '#000' }}>
      <HomePage onOpenApp={setActiveApp} />

      {activeApp === 'chat' && (
        <ChatPage onBack={() => setActiveApp(null)} />
      )}

      {activeApp === 'worldbook' && (
        <WorldbookPage onBack={() => setActiveApp(null)} />
      )}

      {activeApp && activeApp !== 'chat' && activeApp !== 'worldbook' && (
        <PlaceholderPage
          title={appTitles[activeApp]}
          onBack={() => setActiveApp(null)}
        />
      )}
    </div>
  );
}
