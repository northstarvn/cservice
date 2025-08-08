// src/context/AppContext.js
import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
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
  const [bookingConfirmationData, setBookingConfirmationData] = useState(null);
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

  const switchLanguage = (newLanguage) => {
    setLanguage(newLanguage);
    loadTranslations(newLanguage);
    trackUserBehavior('language_switch', { from: language, to: newLanguage });
    
    if (user) {
      console.log('Updating user language preference to:', newLanguage);
    }
  };

  const loadTranslations = async (lang) => {
    try {
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

  // Wrap trackUserBehavior in useCallback to prevent infinite re-renders
  const trackUserBehavior = useCallback((eventType, eventData = {}) => {
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

    console.log('Analytics Event:', event);
  }, [user?.id, language]);

  // Track page views
  useEffect(() => {
    trackUserBehavior('page_view', { page: window.location.pathname });
  }, [trackUserBehavior]);

  const submitBooking = async (bookingDetails) => {
    try {
      setLoading(true);
      
      // Mock API call
      await new Promise(resolve => setTimeout(resolve, 1500));
      
      const bookingId = 'booking_' + Date.now();
      const booking = {
        booking_id: bookingId,
        bookingId: bookingId, // For compatibility
        user_id: user?.id,
        ...bookingDetails,
        serviceType: bookingDetails.service_type, // For compatibility
        status: 'confirmed',
        created_at: new Date().toISOString()
      };
      
      // Store in localStorage
      const existingBookings = JSON.parse(localStorage.getItem('userBookings') || '[]');
      existingBookings.push(booking);
      localStorage.setItem('userBookings', JSON.stringify(existingBookings));
      
      setBookingData(booking);
      trackUserBehavior('booking_submitted', booking);
      
      return { success: true, data: booking };
    } catch (error) {
      console.error('Booking error:', error);
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

  const closePopup = useCallback(() => {
    setShowLoginPopup(false);
    setShowBookingConfirmation(false);
    setShowTrackingResult(false);
    setBookingConfirmationData(null);
    setTrackingData(null);
  }, []);

  const addNotification = useCallback((message, type = 'info') => {
    const notification = {
      id: 'notif_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9),
      message,
      type,
      timestamp: new Date().toISOString()
    };
    
    setNotifications(prev => [...prev, notification]);
    
    setTimeout(() => {
      removeNotification(notification.id);
    }, 5000);
  }, []);

  const removeNotification = useCallback((id) => {
    setNotifications(prev => prev.filter(notif => notif.id !== id));
  }, []);

  const openLoginPopup = useCallback(() => {
    setShowLoginPopup(true);
    trackUserBehavior('login_popup_opened');
  }, [trackUserBehavior]);

  // Add the missing function
  const openBookingConfirmation = useCallback((bookingData = null) => {
    setBookingConfirmationData(bookingData);
    setShowBookingConfirmation(true);
  }, []);

  // Add tracking result functions
  const openTrackingResult = useCallback((data = null) => {
    setTrackingData(data);
    setShowTrackingResult(true);
  }, []);

  const closeTrackingResult = useCallback(() => {
    setShowTrackingResult(false);
    setTrackingData(null);
  }, []);

  // Generate AI suggestions for project planning
  const generateAISuggestions = async (projectName, userInput) => {
    try {
      setLoading(true);
      
      // Replace mock with real API call
      const response = await fetch(`${import.meta.env.VITE_API_URL || 'http://localhost:8000'}/planning/ai-suggest`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('auth_token')}`,
        },
        body: JSON.stringify({ 
          project_name: projectName, 
          user_input: userInput,
          context_data: {}
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      
      trackUserBehavior('ai_suggestions_generated', { projectName, userInput });
      
      return { success: true, suggestions: data.suggestions };
    } catch (error) {
      console.error('Error generating AI suggestions:', error);
      
      // Fallback to mock suggestions if API fails
      const fallbackSuggestions = [
        'Consider using React for the frontend framework',
        'Implement responsive design for mobile compatibility',
        'Add user authentication and authorization',
        'Include data validation and error handling',
        'Set up automated testing (unit and integration)',
        'Plan for scalability and performance optimization'
      ];
      
      return { success: true, suggestions: fallbackSuggestions };
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
    bookingConfirmationData,
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
    openBookingConfirmation,
    openTrackingResult,
    closeTrackingResult,
    generateAISuggestions,
    
    // Aliases for backward compatibility
    isLoading: loading,
    setIsLoading: setLoading,
    trackingResult: trackingData
  };

  return (
    <AppContext.Provider value={value}>
      {children}
    </AppContext.Provider>
  );
};