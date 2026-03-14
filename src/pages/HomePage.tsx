import { useState, useEffect } from 'react';

type AppId = 'chat' | 'memory' | 'settings' | 'worldbook';

interface HomePageProps {
  onOpenApp: (app: AppId) => void;
}

function useClock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

export default function HomePage({ onOpenApp }: HomePageProps) {
  const now = useClock();

  const timeStr = now.toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', hour12: false });
  const dateStr = now.toLocaleDateString('zh-TW', { month: 'long', day: 'numeric', weekday: 'long' });

  const dockApps: { id: AppId; icon: string; label: string }[] = [
    { id: 'chat',    icon: '💬', label: '聊天' },
    { id: 'memory',  icon: '🧠', label: '記憶室' },
    { id: 'settings',icon: '⚙️', label: '設定' },
    { id: 'worldbook',icon: '📖', label: '世界書' },
  ];

  return (
    <div
      className="relative w-full h-full flex flex-col select-none"
      style={{ background: 'linear-gradient(160deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%)' }}
    >
      {/* 時鐘區 */}
      <div className="flex-1 flex flex-col items-center justify-center gap-1 pb-8">
        <div className="text-white font-light" style={{ fontSize: 80, letterSpacing: -2, lineHeight: 1 }}>
          {timeStr}
        </div>
        <div className="text-white/60 text-lg font-light">{dateStr}</div>
      </div>

      {/* Dock */}
      <div className="pb-8 px-6" style={{ paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 24px)' }}>
        <div
          className="flex items-center justify-around rounded-3xl px-3 py-3"
          style={{ background: 'rgba(255,255,255,0.12)', backdropFilter: 'blur(20px)', border: '1px solid rgba(255,255,255,0.15)' }}
        >
          {dockApps.map((app) => (
            <button
              key={app.id}
              onClick={() => onOpenApp(app.id)}
              className="flex flex-col items-center gap-1 active:scale-90 transition-transform"
            >
              <div
                className="w-14 h-14 rounded-2xl flex items-center justify-center text-2xl"
                style={{ background: 'rgba(255,255,255,0.15)', backdropFilter: 'blur(10px)' }}
              >
                {app.icon}
              </div>
              <span className="text-white/70 text-xs">{app.label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
