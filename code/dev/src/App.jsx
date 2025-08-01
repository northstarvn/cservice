import React from 'react';
import ErrorBoundary from './components/ErrorBoundary';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import { AuthProvider } from './context/AuthContext';
import { AppProvider } from './context/AppContext';
import { ChatProvider } from './context/ChatContext';
import Navbar from './components/Navbar';
import Home from './pages/Home';
import Chat from './pages/Chat';
import Booking from './pages/Booking';
import MyBookings from './pages/MyBookings';
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
import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { ROUTES } from './routes';

export function LoginRedirect() {
  const { openLoginPopup } = useApp();
  const navigate = useNavigate();

  useEffect(() => {
    openLoginPopup();
  }, [openLoginPopup]);

  return null;
}

const AppContent = () => {
  const { 
    loading, 
    showLoginPopup, 
    showBookingConfirmation, 
    showTrackingResult,
    bookingConfirmationData,
    theme,
    language,
    closePopup
  } = useApp();
  const { loading: authLoading } = useAuth();

  if (!loading && !authLoading && (!theme || !language)) {
    console.error('Missing required context providers');
    return <div>Application configuration error</div>;
  }

  if (authLoading || loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-blue-600 to-purple-700">
        <LoadingSpinner size="large" color="white" />
      </div>
    );
  }

  return (
    <div className={`min-h-screen ${theme === 'dark' ? 'dark' : ''}`}>
      <div className="bg-gray-50 dark:bg-gray-900 min-h-screen">
        <Navbar />
        <main className="pt-16">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/booking" element={<Booking />} />
            <Route path="/bookings" element={<MyBookings />} />
            <Route path="/tracking" element={<Tracking />} />
            <Route path="/profile" element={<Profile />} />
            <Route path="/planning" element={<Planning />} />
            <Route path="/login" element={<LoginRedirect />} />
          </Routes>
        </main>

        {/* Popups */}
        {showLoginPopup && <LoginPopup isOpen={showLoginPopup} onClose={closePopup} />}
        {showBookingConfirmation && (
          <BookingConfirmationPopup 
            isOpen={showBookingConfirmation} 
            onClose={closePopup}
            bookingDetails={bookingConfirmationData}
          />
        )}
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
    <ErrorBoundary>
      <AuthProvider>
        <AppProvider>
          <Router>
            <div className="App">
              <Routes>
                <Route path="/bookings" element={<MyBookings />} />
                {/* other routes */}
              </Routes>
            </div>
          </Router>
        </AppProvider>
      </AuthProvider>
    </ErrorBoundary>
  );
}

export default App;

