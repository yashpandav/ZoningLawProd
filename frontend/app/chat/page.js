import { Suspense } from 'react';
import ChatPage from './ChatPage';

export const metadata = {
  title: 'Parcel Chat — Toronto Zoning',
  description: 'Full-page zoning assistant for a selected parcel',
};

function Loading() {
  return (
    <div style={{ background: '#09090B', height: '100vh', width: '100vw', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ width: 24, height: 24, borderRadius: '50%', border: '2px solid rgba(255,255,255,0.08)', borderTopColor: '#E8A95C', animation: 'spin 0.8s linear infinite' }} />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <ChatPage />
    </Suspense>
  );
}
