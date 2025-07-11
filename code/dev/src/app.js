// src/App.js
import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import { AuthProvider } from './context/AuthContext';
import { AppProvider } from './context/AppContext';
import { ChatProvider } from './context/ChatContext';
import Navbar from './components/Navbar';
import Home from './pages/Home';
import Chat from './pages/Chat';
import Booking from './pages/Booking';
import Tracking from './pages/Tracking';
import Profile from './pages/Profile';
import Planning from './pages/Planning';
import LoginPopup from './components/LoginPopup';
import BookingConfirmationPopup from './components/BookingConfirmationPopup';
import TrackingResultPopup from './components/TrackingResultPopup';
import NotificationContainer from './components/NotificationContainer';
import LoadingSpinner from './components/LoadingSpinner';
import { useApp } from './context/AppContext';
import { useAuth } from './context/AuthContext';
import './styles/App.css';

const appContext = useApp();
const authContext = useAuth();
const AppContent = () => {
  const { 
    loading, 
    showLoginPopup, 
    showBookingConfirmation, 
    showTrackingResult,
    theme,
    language 
  } = useApp();
  const { loading: authLoading } = authContext;

  if (!appContext || !authContext) {
    console.error('Missing required context providers');
    return <div>Application configuration error</div>;
  }

  if (authLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-blue-600 to-purple-700">
        <LoadingSpinner size="large" color="white" />
      </div>
    );
  }

  return (
    <div className={`min-h-screen ${theme === 'dark' ? 'dark' : ''}`}>
      <div className="bg-gray-50 dark:bg-gray-900 min-h-screen">
        <Router>
          <Navbar />
          <main className="pt-16">
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/chat" element={<Chat />} />
              <Route path="/booking" element={<Booking />} />
              <Route path="/tracking" element={<Tracking />} />
              <Route path="/profile" element={<Profile />} />
              <Route path="/planning" element={<Planning />} />
            </Routes>
          </main>
        </Router>

        {/* Popups */}
        {showLoginPopup && <LoginPopup />}
        {showBookingConfirmation && <BookingConfirmationPopup />}
        {showTrackingResult && <TrackingResultPopup />}

        {/* Notifications */}
        <NotificationContainer />

        {/* Global Loading Overlay */}
        {loading && (
          <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow-xl">
              <LoadingSpinner size="large" />
              <p className="mt-4 text-gray-700 dark:text-gray-300">Processing...</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

function App() {
  return (
    <AuthProvider>
      <AppProvider>
        <ChatProvider>
          <AppContent />
        </ChatProvider>
      </AppProvider>
    </AuthProvider>
  );
}

export default App;