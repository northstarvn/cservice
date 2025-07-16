// src/context/AppContext.js
import React, { createContext, useContext, useState, useEffect } from 'react';
import { useAuth } from './AuthContext';
import { i18n } from '../utils/I18n';  // Import the class

export const AppContext = createContext();

export const useApp = () => {
  const context = useContext(AppContext);
  if (!context) {
    console.error('useApp must be used within an AppProvider');
    throw new Error('useApp must be used within an AppProvider');
  }
  return context;
};

export const AppProvider = ({ children }) => {
  const { user } = useAuth();
  const [language, setLanguage] = useState('en');
  const [theme, setTheme] = useState('light');
  const [isMobile, setIsMobile] = useState(false);
  const [showLoginPopup, setShowLoginPopup] = useState(false);
  const [showBookingConfirmation, setShowBookingConfirmation] = useState(false);
  const [showTrackingResult, setShowTrackingResult] = useState(false);
  const [trackingData, setTrackingData] = useState(null);
  const [bookingData, setBookingData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [notifications, setNotifications] = useState([]);
  const [analytics, setAnalytics] = useState({
    pageViews: 0,
    userInteractions: 0,
    chatMessages: 0
  });

  const [translations, setTranslations] = useState({});
  // Detect mobile device
  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth <= 768);
    };
    
    checkMobile();
    window.addEventListener('resize', checkMobile);
    
    return () => window.removeEventListener('resize', checkMobile);
  }, []);

  // Initialize user language preference
  useEffect(() => {
    if (user?.preferredLanguage) {
      setLanguage(user.preferredLanguage);
    }
  }, [user]);

  // Track page views
  useEffect(() => {
    trackUserBehavior('page_view', { page: window.location.pathname });
  }, []);

  const switchLanguage = (newLanguage) => {
    setLanguage(newLanguage);
    // Load translations for the new language
    loadTranslations(newLanguage);
    trackUserBehavior('language_switch', { from: language, to: newLanguage });
    
    // Update user profile if logged in
    if (user) {
      console.log('Updating user language preference to:', newLanguage);
    }
  };

  const loadTranslations = async (lang) => {
    try {
      // This would typically load from an API or import language files
      const mockTranslations = {
        en: { welcome: 'Welcome', loading: 'Loading...' },
        es: { welcome: 'Bienvenido', loading: 'Cargando...' }
      };
      setTranslations(mockTranslations[lang] || mockTranslations.en);
    } catch (error) {
      console.error('Error loading translations:', error);
    }
  };

  const toggleTheme = () => {
    const newTheme = theme === 'light' ? 'dark' : 'light';
    setTheme(newTheme);
    trackUserBehavior('theme_toggle', { theme: newTheme });
  };

  const trackUserBehavior = (eventType, eventData = {}) => {
    const event = {
      id: 'event_' + Date.now(),
      userId: user?.id || 'anonymous',
      eventType,
      eventData,
      timestamp: new Date().toISOString(),
      page: window.location.pathname,
      userAgent: navigator.userAgent,
      language
    };

    // Update analytics
    setAnalytics(prev => ({
      ...prev,
      userInteractions: prev.userInteractions + 1,
      ...(eventType === 'page_view' && { pageViews: prev.pageViews + 1 }),
      ...(eventType === 'chat_message' && { chatMessages: prev.chatMessages + 1 })
    }));

    // In real app, send to analytics service
    console.log('Analytics Event:', event);
  };

  const submitBooking = async (bookingDetails) => {
    try {
      setLoading(true);
      
      // Mock API call
      await new Promise(resolve => setTimeout(resolve, 1500));
      
      const booking = {
        id: 'booking_' + Date.now(),
        userId: user?.id,
        ...bookingDetails,
        status: 'confirmed',
        createdAt: new Date().toISOString()
      };
      
      setBookingData(booking);
      setShowBookingConfirmation(true);
      
      trackUserBehavior('booking_submitted', booking);
      
      addNotification('Booking confirmed successfully!', 'success');
      
      return { success: true, booking };
    } catch (error) {
      console.error('Booking error:', error);
      addNotification('Booking failed. Please try again.', 'error');
      return { success: false, error: error.message };
    } finally {
      setLoading(false);
    }
  };

  const trackDelivery = async (trackingId) => {
    try {
      setLoading(true);
      
      // Mock API call
      await new Promise(resolve => setTimeout(resolve, 1000));
      
      // Mock tracking data
      const trackingInfo = {
        trackingId,
        status: 'In Transit',
        lastUpdated: new Date().toISOString(),
        estimatedDelivery: '2025-07-10',
        location: 'Distribution Center',
        history: [
          { status: 'Order Placed', date: '2025-07-08', location: 'Origin' },
          { status: 'In Transit', date: '2025-07-09', location: 'Distribution Center' }
        ]
      };
      
      setTrackingData(trackingInfo);
      setShowTrackingResult(true);
      
      trackUserBehavior('delivery_tracked', { trackingId });
      
      return { success: true, data: trackingInfo };
    } catch (error) {
      console.error('Tracking error:', error);
      addNotification('Unable to track delivery. Please check your tracking ID.', 'error');
      return { success: false, error: error.message };
    } finally {
      setLoading(false);
    }
  };

  const addNotification = (message, type = 'info') => {
    const notification = {
      id: 'notif_' + Date.now(),
      message,
      type,
      timestamp: new Date().toISOString()
    };
    
    setNotifications(prev => [...prev, notification]);
    
    // Auto-remove after 5 seconds
    setTimeout(() => {
      removeNotification(notification.id);
    }, 5000);
  };

  const removeNotification = (id) => {
    setNotifications(prev => prev.filter(notif => notif.id !== id));
  };

  const closePopup = () => {
    setShowLoginPopup(false);
    setShowBookingConfirmation(false);
    setShowTrackingResult(false);
  };

  const openLoginPopup = () => {
    setShowLoginPopup(true);
    trackUserBehavior('login_popup_opened');
  };

  // Generate AI suggestions for project planning
  const generateAISuggestions = async (projectName, userInput) => {
    try {
      setLoading(true);
      
      // Mock AI suggestions
      await new Promise(resolve => setTimeout(resolve, 1000));
      
      const suggestions = [
        'Consider using React for the frontend framework',
        'Implement responsive design for mobile compatibility',
        'Add user authentication and authorization',
        'Include data validation and error handling',
        'Set up automated testing (unit and integration)',
        'Plan for scalability and performance optimization'
      ];
      
      trackUserBehavior('ai_suggestions_generated', { projectName, userInput });
      
      return { success: true, suggestions };
    } catch (error) {
      console.error('AI suggestions error:', error);
      return { success: false, error: error.message };
    } finally {
      setLoading(false);
    }
  };

  const value = {
    language,
    theme,
    isMobile,
    loading,
    notifications,
    analytics,
    showLoginPopup,
    showBookingConfirmation,
    showTrackingResult,
    trackingData,
    bookingData,
    translations,
    switchLanguage,
    toggleTheme,
    trackUserBehavior,
    submitBooking,
    trackDelivery,
    addNotification,
    removeNotification,
    closePopup,
    openLoginPopup,
    generateAISuggestions
  };

  return (
    <AppContext.Provider value={value}>
      {children}
    </AppContext.Provider>
  );
};