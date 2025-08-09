// utils/api.js
// Change the API_BASE_URL configuration at the top of the file
const API_BASE_URL = window.location.hostname === 'localhost' 
  ? 'http://localhost:8000'  // Changed from 'http://localhost:8080/api' to match FastAPI
  : '/api';

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

const handleResponse = async (response) => {
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new ApiError(
      errorData.detail || errorData.message || 'An error occurred',
      response.status,
      errorData
    );
  }
  return response.json();
};

// Update the makeRequest function to properly format the Authorization header
const makeRequest = async (endpoint, options = {}) => {
  const url = `${API_BASE_URL}${endpoint}`;
  const token = localStorage.getItem('auth_token');
  
  const config = {
    headers: {
      'Content-Type': 'application/json',
      ...(token && { Authorization: `Bearer ${token}` }), // Ensure Bearer prefix
      ...options.headers,
    },
    ...options,
  };

  if (config.body && typeof config.body === 'object') {
    config.body = JSON.stringify(config.body);
  }

  try {
    const response = await fetch(url, config);
    return await handleResponse(response);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    throw new ApiError('Network error occurred', 0, { originalError: error });
  }
};

// Add this to the authApi object in api.js

export const authApi = {
  register: async (userData) => {
    console.log('Sending registration request:', userData);
    
    const response = await makeRequest('/users/register', {
      method: 'POST',
      body: {
        username: userData.username,
        password: userData.password,
        email: userData.email,
        full_name: userData.fullName
      },
    });
    
    return response;
  },

  login: async (credentials) => {
    console.log('Sending login request:', credentials);
    
    const response = await makeRequest('/users/login', {
      method: 'POST',
      body: credentials,
    });
    
    if (response.access_token) {
      localStorage.setItem('auth_token', response.access_token);
      localStorage.setItem('user_id', credentials.username);
    }
    
    return response;
  },

  getCurrentUser: async () => {
    return makeRequest('/users/me');
  },

  logout: async () => {
    try {
      localStorage.removeItem('auth_token');
      localStorage.removeItem('user_id');
      localStorage.removeItem('session_id');
    } catch (error) {
      console.error('Logout error:', error);
    }
  },

  validateToken: async () => {
    return makeRequest('/users/me');
  },
};

// Booking API - Update endpoints to match FastAPI
export const bookingApi = {
  submitBooking: async (userId, serviceType, date, time) => {
    return makeRequest('/bookings/', {  // Changed from '/booking/submit'
      method: 'POST',
      body: { user_id: userId, service_type: serviceType, date, time },
    });
  },

  getBookings: async (userId) => {
    return makeRequest(`/bookings/user/${userId}`);  // Changed from '/booking/user/'
  },

  cancelBooking: async (bookingId) => {
    return makeRequest(`/bookings/${bookingId}`, {  // Changed from '/booking/cancel/'
      method: 'DELETE',
    });
  },

  getAvailableSlots: async (serviceType, date) => {
    return makeRequest(`/bookings/slots?service_type=${serviceType}&date=${date}`);  // Changed from '/booking/slots'
  },
};

// Chat API
export const chatApi = {
  initChat: async (userId, language = 'en', contextData = {}) => {
    return makeRequest('/chat/init', {
      method: 'POST',
      body: { user_id: userId, language, context_data: contextData },
    });
  },

  sendMessage: async (sessionId, message, language = 'en') => {
    return makeRequest('/chat/message', {
      method: 'POST',
      body: { session_id: sessionId, message, language },
    });
  },

  getChatHistory: async (sessionId) => {
    return makeRequest(`/chat/history/${sessionId}`);
  },

  analyzeSentiment: async (message, language = 'en') => {
    return makeRequest('/chat/sentiment', {
      method: 'POST',
      body: { message, language },
    });
  },

  startVoiceRecognition: async (sessionId, language = 'en') => {
    return makeRequest('/chat/voice/start', {
      method: 'POST',
      body: { session_id: sessionId, language },
    });
  },
};

// Tracking API
export const trackingApi = {
  trackDelivery: async (trackingId, userId) => {
    return makeRequest('/tracking/track', {
      method: 'POST',
      body: { tracking_id: trackingId, user_id: userId },
    });
  },

  getDeliveryHistory: async (userId) => {
    return makeRequest(`/tracking/history/${userId}`);
  },

  updateDeliveryStatus: async (trackingId, status) => {
    return makeRequest('/tracking/update', {
      method: 'PUT',
      body: { tracking_id: trackingId, status },
    });
  },
};

// Profile API
export const profileApi = {
  getProfile: async (userId) => {
    return makeRequest(`/profile/${userId}`);
  },

  updateProfile: async (userId, profileData) => {
    return makeRequest(`/profile/${userId}`, {
      method: 'PUT',
      body: profileData,
    });
  },

  saveProfile: async (userId, profileData) => {
    return makeRequest('/profile/save', {
      method: 'POST',
      body: { user_id: userId, ...profileData },
    });
  },
};

// Planning API
export const planningApi = {
  submitPlan: async (userId, projectName, requirements) => {
    return makeRequest('/planning/submit', {
      method: 'POST',
      body: { user_id: userId, project_name: projectName, requirements },
    });
  },

  aiSuggestRequirements: async (projectName, userInput, contextData = {}) => {
    return makeRequest('/planning/ai-suggest', {
      method: 'POST',
      body: { project_name: projectName, user_input: userInput, context_data: contextData },
    });
  },

  getProjects: async (userId) => {
    return makeRequest(`/planning/projects/${userId}`);
  },

  updateProject: async (projectId, updateData) => {
    return makeRequest(`/planning/project/${projectId}`, {
      method: 'PUT',
      body: updateData,
    });
  },
};

// Analytics API
export const analyticsApi = {
  trackUserBehavior: async (userId, eventType, eventData) => {
    return makeRequest('/analytics/track', {
      method: 'POST',
      body: { user_id: userId, event_type: eventType, event_data: eventData },
    });
  },

  getUserAnalytics: async (userId) => {
    return makeRequest(`/analytics/user/${userId}`);
  },

  getSystemAnalytics: async () => {
    return makeRequest('/analytics/system');
  },
};

// Translation API
export const translationApi = {
  loadTranslations: async (language) => {
    return makeRequest(`/translations/${language}`);
  },

  switchLanguage: async (language) => {
    return makeRequest('/translations/switch', {
      method: 'POST',
      body: { language },
    });
  },
};

// AI Model API
export const aiModelApi = {
  trainModel: async (dataset, modelConfig) => {
    return makeRequest('/ai/train', {
      method: 'POST',
      body: { dataset, model_config: modelConfig },
    });
  },

  getModelStatus: async (modelId) => {
    return makeRequest(`/ai/model/${modelId}/status`);
  },

  updateModelConfig: async (modelId, config) => {
    return makeRequest(`/ai/model/${modelId}/config`, {
      method: 'PUT',
      body: config,
    });
  },
};

// Combined API export
export const api = {
  auth: authApi,
  chat: chatApi,
  booking: bookingApi,
  tracking: trackingApi,
  profile: profileApi,
  planning: planningApi,
  analytics: analyticsApi,
  translation: translationApi,
  aiModel: aiModelApi,
};

// Utility functions
export const apiUtils = {
  isAuthenticated: () => {
    return !!localStorage.getItem('auth_token');
  },

  getCurrentUserId: () => {
    return localStorage.getItem('user_id');
  },

  getCurrentSessionId: () => {
    return localStorage.getItem('session_id');
  },

  setSessionId: (sessionId) => {
    localStorage.setItem('session_id', sessionId);
  },

  clearAuth: () => {
    localStorage.removeItem('auth_token');
    localStorage.removeItem('user_id');
    localStorage.removeItem('session_id');
  },

  handleApiError: (error) => {
    console.error('API Error:', error);
    
    if (error.status === 401) {
      apiUtils.clearAuth();
      window.location.href = '/login';
    }
    
    return {
      message: error.message || 'An unexpected error occurred',
      status: error.status || 0,
      data: error.data || {},
    };
  },

  retry: async (fn, retries = 3, delay = 1000) => {
    try {
      return await fn();
    } catch (error) {
      if (retries > 0 && error.status >= 500) {
        await new Promise(resolve => setTimeout(resolve, delay));
        return apiUtils.retry(fn, retries - 1, delay * 2);
      }
      throw error;
    }
  },
};

export default api;