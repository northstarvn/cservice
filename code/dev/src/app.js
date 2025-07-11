// src/App.js
import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import { AuthProvider } from './context/auth_context';
import { AppProvider } from './context/app_context';
import { ChatProvider } from './context/chat_context';
import Navbar from './components/navbar_component';
import Home from './pages/home_page';
import Chat from './pages/chat_component';
import Booking from './pages/booking_component';
import Tracking from './pages/tracking_page';
import Profile from './pages/profile_page';
import Planning from './pages/planning_page';
import LoginPopup from './components/login_popup';
import BookingConfirmationPopup from './components/booking_confirmation_popup';
import TrackingResultPopup from './components/tracking_result_popup';
import NotificationContainer from './components/notification_container';
import LoadingSpinner from './components/loading_spinner';
import { useApp } from './context/app_context';
import { useAuth } from './context/auth_context';
import './styles/App.css';

const AppContent = () => {
  const { 
    loading, 
    showLoginPopup, 
    showBookingConfirmation, 
    showTrackingResult,
    theme,
    language 
  } = useApp();
  const { loading: authLoading } = useAuth();

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